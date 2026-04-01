"""Shake build backend — a self-executing backend using verifying traces.

Implements the Shake rebuild strategy from "Build Systems à la Carte"
(Mokhov, Mitchell, Jones 2018), specifically:
- Suspending scheduler: build dependencies on-demand recursively
- Verifying traces: content-hash-based change detection for minimal rebuilds
- Early cutoff: if rebuilt output is byte-identical, skip rebuilding dependents

Content-addressable short-circuit: compile rules produce output filenames that
encode source hash, dependency hash, and macro state hash.  If such an output
already exists on disk it is correct by construction, so verifying traces
degenerates to a single os.path.exists() call — skipping all hashing, trace
lookup, and input comparison for no-op rebuilds.

The dependency graph is static (pre-computed by Hunter), not dynamic as in the
original Shake (which uses monadic tasks for dynamic dependency discovery).
This is sufficient because compiletools resolves all dependencies at a higher
level before the backend executes.

No external build tool required — drives compilation directly from Python.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass

import compiletools.filesystem_utils
from compiletools.build_backend import (
    BuildBackend,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.locking import FileLock, atomic_compile

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


def hash_command(cmd: list[str]) -> str:
    """Compute a stable hash of a shell command list."""
    return hashlib.sha1(json.dumps(cmd, sort_keys=False).encode()).hexdigest()


def _atomic_copy(src: str, dst: str) -> None:
    """Copy src to dst atomically via temp file + rename."""
    dst_dir = os.path.dirname(dst) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dst_dir)
    try:
        os.close(tmp_fd)
        shutil.copy2(src, tmp_path)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _is_build_artifact(rule) -> bool:
    """Rules whose output names encode all inputs — existence implies correctness."""
    return rule.rule_type in ("compile", "link", "static_library", "shared_library")


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

    def _ca_target(self, rule: BuildRule) -> str:
        """Content-addressable output path for a link/library rule.

        Hashes sorted inputs + command (with output path stripped to avoid
        circularity).  The CA filename lives alongside the human-readable
        output so directory creation is already handled.
        """
        cmd_filtered = [a for a in rule.command if a != rule.output]
        key = json.dumps({"inputs": sorted(rule.inputs), "cmd": cmd_filtered}, sort_keys=True)
        h = hashlib.sha1(key.encode()).hexdigest()[:20]
        base = os.path.basename(rule.output)
        name, ext = os.path.splitext(base)
        return os.path.join(os.path.dirname(rule.output), f"{name}_{h}{ext}")

    def _execute_build(self, target: str) -> None:
        # Not used: ShakeBackend overrides execute() with its own build engine.
        raise NotImplementedError  # pragma: no cover

    def execute(self, target: str = "build") -> None:
        if target == "runtests":
            self._run_tests()
            return

        if self._graph is None:
            raise RuntimeError("generate() must be called before execute()")

        trace_path = os.path.join(self.args.objdir, self.build_filename())
        traces = TraceStore(trace_path)
        done: set[str] = set()
        lock = threading.Lock()

        parallel = getattr(self.args, "parallel", 1)
        max_workers = parallel if parallel and parallel > 0 else 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            self._build(target, self._graph, traces, done, lock, executor, max_workers)

        traces.save()

    def _build(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        done: set[str],
        lock: threading.Lock,
        executor: ThreadPoolExecutor,
        max_workers: int = 1,
    ) -> bool:
        """Suspending scheduler with verifying traces and early cutoff.

        Returns True if the target's output changed (dependents should rebuild).
        """
        with lock:
            if target in done:
                return False

        rule = graph.get_rule(target)
        if rule is None:
            # Leaf node (source/header file) — no rule to run
            with lock:
                done.add(target)
            return False

        if rule.rule_type == "phony":
            # Phony targets aggregate independent builds — parallelise them.
            # Guard against nested phony targets: if an input is itself phony,
            # build it sequentially to avoid ThreadPoolExecutor deadlock (all
            # workers blocking on f.result() for queued-but-unstarted tasks).
            futures: list[Future[bool]] = []
            sequential_results: list[bool] = []
            for inp in rule.inputs:
                inp_rule = graph.get_rule(inp)
                if inp_rule is not None and inp_rule.rule_type == "phony":
                    sequential_results.append(
                        self._build(inp, graph, traces, done, lock, executor, max_workers)
                    )
                else:
                    futures.append(
                        executor.submit(
                            self._build, inp, graph, traces, done, lock, executor, max_workers
                        )
                    )
            any_rebuilt = any(f.result() for f in futures) or any(sequential_results)
            with lock:
                done.add(target)
            return any_rebuilt

        # Ensure order-only deps (directories) exist
        for dep in rule.order_only_deps:
            os.makedirs(dep, exist_ok=True)

        # CONTENT-ADDRESSABLE SHORT-CIRCUIT
        # For compile rules, the object filename encodes all inputs.
        # For link/library rules, we compute a CA target from the rule's
        # inputs + command; if that CA file exists, copy it to the
        # human-readable target and skip.
        if _is_build_artifact(rule):
            if rule.rule_type == "compile":
                # Compile: target IS the CA name
                if os.path.exists(target):
                    with lock:
                        done.add(target)
                    return False
            else:
                # Link/library: CA name differs from graph target
                ca = self._ca_target(rule)
                if os.path.exists(ca):
                    _atomic_copy(ca, target)
                    with lock:
                        done.add(target)
                    return False

        # SUSPEND: build all inputs.  The dependency graph is fully static
        # (pre-computed by Hunter), so all inputs are known upfront and can
        # be started concurrently when workers are available (§6.2 of Mokhov
        # et al. 2018).  Shared deps are safe: the ``done`` set prevents
        # duplicate work.  With max_workers == 1, submitting from inside a
        # worker would deadlock (the worker blocks on f.result() while no
        # other worker can pick up the submitted task), so we fall back to
        # sequential execution.
        if max_workers > 1 and len(rule.inputs) > 1:
            futures = [
                executor.submit(
                    self._build, inp, graph, traces, done, lock, executor, max_workers
                )
                for inp in rule.inputs
            ]
            # Collect *all* results (no short-circuit) so that every input
            # is built before we proceed to the EXECUTE step.
            results = [f.result() for f in futures]
            any_input_rebuilt = any(results)
        else:
            any_input_rebuilt = False
            for inp in rule.inputs:
                if self._build(inp, graph, traces, done, lock, executor, max_workers):
                    any_input_rebuilt = True

        # VERIFY TRACE (non-CA rules only)
        if not _is_build_artifact(rule) and not any_input_rebuilt:
            with lock:
                trace = traces.get(target)
            if trace is not None and self._verify(rule, trace):
                with lock:
                    done.add(target)
                return False  # up to date

        # EXECUTE
        old_hash = None
        if not _is_build_artifact(rule):
            old_hash = get_file_hash(target) if os.path.exists(target) else None

        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 1:
            print(" ".join(rule.command), file=sys.stderr)

        # Flatten multi-word elements (e.g. CXXFLAGS stored as single string)
        flat_cmd = []
        for arg in rule.command:
            parts = shlex.split(arg)
            flat_cmd.extend(parts)

        if rule.rule_type == "compile":
            # Compile rules: use atomic_compile to prevent TOCTOU races.
            # The target file never exists in a partial state (compile to
            # temp file, then atomic rename). Strip the trailing -o <target>
            # from flat_cmd since atomic_compile appends -o <tempfile>.
            cmd_without_output = flat_cmd[:-2]  # remove [-o, target]
            file_lock = FileLock(target, self.args)
            lock_impl = file_lock.lock  # underlying lock (or None if disabled)
            if lock_impl is not None:
                try:
                    result = atomic_compile(lock_impl, target, cmd_without_output)
                except subprocess.CalledProcessError as e:
                    print(e.stdout or "", end="", file=sys.stdout)
                    print(e.stderr or "", end="", file=sys.stderr)
                    raise
            else:
                # File locking disabled — still use temp+rename for atomicity
                # but without cross-process locking
                result = self._atomic_compile_no_lock(target, cmd_without_output)
        elif _is_build_artifact(rule):
            # Link/library rules: build to CA target, then copy to human target.
            ca = self._ca_target(rule)
            ca_cmd = [ca if a == target else a for a in flat_cmd]
            with FileLock(ca, self.args):
                # FlockLock creates an empty file at the CA path via O_CREAT.
                # ar -r treats an empty file as a corrupt archive and fails
                # with "File format not recognized".  Remove the empty file
                # so ar can create a fresh archive.  The flock fd stays valid
                # after unlink on Unix, so the lock is still held.
                if os.path.exists(ca) and os.path.getsize(ca) == 0:
                    os.unlink(ca)
                result = subprocess.run(ca_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(result.stdout, end="", file=sys.stdout)
                    print(result.stderr, end="", file=sys.stderr)
                    raise subprocess.CalledProcessError(result.returncode, rule.command, result.stdout, result.stderr)
            _atomic_copy(ca, target)
        else:
            # Non-CA rules (shouldn't normally reach here)
            with FileLock(target, self.args):
                result = subprocess.run(flat_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(result.stdout, end="", file=sys.stdout)
                    print(result.stderr, end="", file=sys.stderr)
                    raise subprocess.CalledProcessError(result.returncode, rule.command, result.stdout, result.stderr)

        with lock:
            done.add(target)

        # CA outputs don't need trace recording or early cutoff —
        # existence implies correctness.
        if _is_build_artifact(rule):
            return True  # New output → dependents must rebuild

        new_hash = get_file_hash(target)

        # RECORD TRACE (only for non-content-addressable rules)
        with lock:
            traces.put(
                target,
                TraceEntry(
                    output_hash=new_hash,
                    input_hashes={inp: get_file_hash(inp) for inp in rule.inputs},
                    command_hash=hash_command(rule.command),
                ),
            )

        # EARLY CUTOFF
        return old_hash != new_hash

    @staticmethod
    def _atomic_compile_no_lock(target: str, compile_cmd: list[str]) -> subprocess.CompletedProcess:
        """Atomic compile without cross-process locking (temp file + rename only)."""
        pid = os.getpid()
        random_suffix = os.urandom(2).hex()
        tempfile_path = f"{target}.{pid}.{random_suffix}.tmp"

        try:
            cmd = list(compile_cmd) + ["-o", tempfile_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            os.rename(tempfile_path, target)
            return result
        finally:
            if os.path.exists(tempfile_path):
                try:
                    os.unlink(tempfile_path)
                except OSError:
                    pass

    def _verify(self, rule, trace: TraceEntry) -> bool:
        """Check if a trace is still valid (output exists, inputs unchanged, same command)."""
        # Verify output file still exists and matches the recorded hash
        try:
            if get_file_hash(rule.output) != trace.output_hash:
                return False
        except (FileNotFoundError, OSError):
            return False

        if hash_command(rule.command) != trace.command_hash:
            return False

        if set(rule.inputs) != set(trace.input_hashes.keys()):
            return False

        for inp, stored_hash in trace.input_hashes.items():
            try:
                current_hash = get_file_hash(inp)
            except (FileNotFoundError, OSError):
                return False
            if current_hash != stored_hash:
                return False

        return True
