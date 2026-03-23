"""Shake build backend — a self-executing backend using verifying traces.

Implements the "Shake" build system from "Build Systems à la Carte":
- Suspending scheduler: build dependencies on-demand recursively
- Verifying traces: content-hash-based change detection for minimal rebuilds
- Early cutoff: if rebuilt output is byte-identical, skip rebuilding dependents

No external build tool required — drives compilation directly from Python.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import compiletools.filesystem_utils
from compiletools.build_backend import BuildBackend, register_backend
from compiletools.build_graph import BuildGraph

TRACE_VERSION = 1


@dataclass
class TraceEntry:
    """Record of a successful build action for verifying traces."""

    output_hash: str
    input_hashes: dict[str, str]
    command_hash: str


class TraceStore:
    """Persistent store for build traces, backed by JSON on disk."""

    def __init__(self, path: str):
        self._path = path
        self._traces: dict[str, TraceEntry] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != TRACE_VERSION:
                return
            for output, entry_dict in data.get("traces", {}).items():
                self._traces[output] = TraceEntry(
                    output_hash=entry_dict["output_hash"],
                    input_hashes=entry_dict["input_hashes"],
                    command_hash=entry_dict["command_hash"],
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            self._traces = {}

    def get(self, output: str) -> TraceEntry | None:
        return self._traces.get(output)

    def put(self, output: str, entry: TraceEntry) -> None:
        self._traces[output] = entry

    def save(self) -> None:
        data = {
            "version": TRACE_VERSION,
            "traces": {output: asdict(entry) for output, entry in self._traces.items()},
        }
        with compiletools.filesystem_utils.atomic_output_file(self._path, mode="w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    @staticmethod
    def hash_command(cmd: list[str]) -> str:
        return hashlib.sha1(json.dumps(cmd, sort_keys=False).encode()).hexdigest()


def _compute_file_hash(path: str) -> str:
    """Compute git blob hash for a file (works for any file, generated or source)."""
    with open(path, "rb") as f:
        content = f.read()
    blob_data = f"blob {len(content)}\0".encode() + content
    return hashlib.sha1(blob_data).hexdigest()


def _hash_file(path: str) -> str:
    """Hash a file, using the global registry for tracked files, fallback for generated."""
    try:
        from compiletools.global_hash_registry import get_file_hash

        return get_file_hash(path)
    except (FileNotFoundError, Exception):
        return _compute_file_hash(path)


@register_backend
class ShakeBackend(BuildBackend):
    """Self-executing backend using Shake-style verifying traces."""

    def __init__(self, args, hunter):
        super().__init__(args, hunter)
        self._graph: BuildGraph | None = None

    @staticmethod
    def name() -> str:
        return "shake"

    @staticmethod
    def build_filename() -> str:
        return ".ct-traces.json"

    def generate(self, graph: BuildGraph, output=None) -> None:
        self._graph = graph
        if output is not None:
            self._write_summary(graph, output)

    def _write_summary(self, graph: BuildGraph, f) -> None:
        f.write("# Shake build graph summary\n\n")
        for rule in graph.rules:
            if rule.rule_type == "phony":
                f.write(f"phony {rule.output}: {' '.join(rule.inputs)}\n")
            elif rule.command:
                f.write(f"{rule.rule_type} {rule.output}:\n")
                f.write(f"  inputs: {' '.join(rule.inputs)}\n")
                f.write(f"  command: {' '.join(rule.command)}\n")
        f.write("\n")

    def execute(self, target: str = "build") -> None:
        if target == "runtests":
            self._run_tests()
            return

        if self._graph is None:
            raise RuntimeError("generate() must be called before execute()")

        trace_path = os.path.join(self.args.objdir, self.build_filename())
        traces = TraceStore(trace_path)
        done: set[str] = set()

        parallel = getattr(self.args, "parallel", 1)
        max_workers = parallel if parallel and parallel > 0 else 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            self._build(target, self._graph, traces, done, executor)

        traces.save()

    def _build(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        done: set[str],
        executor: ThreadPoolExecutor,
    ) -> bool:
        """Suspending scheduler with verifying traces and early cutoff.

        Returns True if the target's output changed (dependents should rebuild).
        """
        if target in done:
            return False

        rule = graph.get_rule(target)
        if rule is None:
            # Leaf node (source/header file) — no rule to run
            done.add(target)
            return False

        if rule.rule_type == "phony":
            any_rebuilt = False
            for inp in rule.inputs:
                if self._build(inp, graph, traces, done, executor):
                    any_rebuilt = True
            done.add(target)
            return any_rebuilt

        # Ensure order-only deps (directories) exist
        for dep in rule.order_only_deps:
            os.makedirs(dep, exist_ok=True)

        # SUSPEND: recursively build all inputs
        any_input_rebuilt = False
        for inp in rule.inputs:
            if self._build(inp, graph, traces, done, executor):
                any_input_rebuilt = True

        # VERIFY TRACE
        if not any_input_rebuilt:
            trace = traces.get(target)
            if trace is not None and self._verify(rule, trace):
                done.add(target)
                return False  # up to date

        # EXECUTE
        old_hash = _compute_file_hash(target) if os.path.exists(target) else None

        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 1:
            print(" ".join(rule.command), file=sys.stderr)

        # Flatten multi-word elements (e.g. CXXFLAGS stored as single string)
        flat_cmd = []
        for arg in rule.command:
            parts = shlex.split(arg)
            flat_cmd.extend(parts)

        result = subprocess.run(flat_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout, end="", file=sys.stdout)
            print(result.stderr, end="", file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, rule.command, result.stdout, result.stderr)

        new_hash = _compute_file_hash(target)

        # RECORD TRACE
        traces.put(
            target,
            TraceEntry(
                output_hash=new_hash,
                input_hashes={inp: _hash_file(inp) for inp in rule.inputs},
                command_hash=TraceStore.hash_command(rule.command),
            ),
        )

        # EARLY CUTOFF
        done.add(target)
        return old_hash != new_hash

    def _verify(self, rule, trace: TraceEntry) -> bool:
        """Check if a trace is still valid (all inputs unchanged, same command)."""
        if TraceStore.hash_command(rule.command) != trace.command_hash:
            return False

        if set(rule.inputs) != set(trace.input_hashes.keys()):
            return False

        for inp, stored_hash in trace.input_hashes.items():
            try:
                current_hash = _hash_file(inp)
            except (FileNotFoundError, OSError):
                return False
            if current_hash != stored_hash:
                return False

        return True
