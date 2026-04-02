"""Verifying-traces build backends: Shake (local threads) and Slurm (HPC cluster).

Both backends implement the Shake rebuild strategy from "Build Systems à la Carte"
(Mokhov, Mitchell, Jones 2018), specifically:
- Suspending scheduler: build dependencies on-demand recursively
- Verifying traces: content-hash-based change detection for minimal rebuilds
- Early cutoff: if rebuilt output is byte-identical, skip rebuilding dependents

Content-addressable short-circuit: compile rules produce output filenames that
encode source hash, dependency hash, and macro state hash.  If such an output
already exists on disk it is correct by construction, so verifying traces
degenerates to a single os.path.exists() call — skipping all hashing, trace
lookup, and input comparison for no-op rebuilds.

ShakeBackend drives compilation directly from Python using asyncio coroutines
with a semaphore to limit subprocess concurrency.

SlurmBackend replaces the async compile phase with batch Slurm job
submission, distributing compile rules across an HPC cluster:

1. Identify compile rules that need rebuilding (trace verify).
2. Submit all of them to Slurm as job arrays (``sbatch --array``).
3. Poll ``sacct`` until every task reaches a terminal state.
4. Record traces for successfully compiled outputs.
5. Run link/library rules locally (they are few, fast, and serial-dependent).
6. Save traces.

The dependency graph is static (pre-computed by Hunter), not dynamic as in the
original Shake (which uses monadic tasks for dynamic dependency discovery).
This is sufficient because compiletools resolves all dependencies at a higher
level before the backend executes.

No external build tool required for either backend — both drive compilation
directly from Python.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import ClassVar

import compiletools.filesystem_utils
from compiletools.build_backend import (
    BuildBackend,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.locking import FileLock, atomic_compile

logger = logging.getLogger(__name__)

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


def _flatten_command(command: list[str]) -> list[str]:
    """Flatten multi-word command elements (e.g. CXXFLAGS as one string) into tokens."""
    return [tok for arg in command for tok in shlex.split(arg)]


def _run_subprocess(cmd: list[str], original_cmd: list[str]) -> subprocess.CompletedProcess:
    """Run *cmd*; on failure stream stdout/stderr and raise CalledProcessError."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, end="", file=sys.stdout)
        print(result.stderr, end="", file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, original_cmd, result.stdout, result.stderr)
    return result


def _make_trace_entry(rule: BuildRule, output_hash: str | None = None) -> TraceEntry:
    """Build a TraceEntry for a successfully executed rule.

    Pass *output_hash* when already computed (avoids a redundant disk read).
    """
    assert rule.command is not None, "only call _make_trace_entry after a rule executes"
    input_hashes = {}
    for p in rule.inputs:
        if os.path.isfile(p):
            input_hashes[p] = get_file_hash(p)
        else:
            logger.debug("_make_trace_entry: skipping non-file input %s for %s", p, rule.output)
    return TraceEntry(
        output_hash=output_hash if output_hash is not None else get_file_hash(rule.output),
        input_hashes=input_hashes,
        command_hash=hash_command(rule.command),
    )


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
        assert rule.command is not None, "_ca_target only applies to link/library rules"
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

        parallel = getattr(self.args, "parallel", 1)
        max_workers = parallel if parallel and parallel > 0 else 1
        sem = asyncio.Semaphore(max_workers)
        memo: dict[str, asyncio.Task[bool]] = {}

        asyncio.run(self._build_async(target, self._graph, traces, memo, sem))

        traces.save()

    async def _build_async(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        memo: dict[str, asyncio.Task[bool]],
        sem: asyncio.Semaphore,
    ) -> bool:
        """Async suspending scheduler with verifying traces and early cutoff.

        Uses asyncio.gather for fan-out (no deadlock risk) and a semaphore
        to limit subprocess concurrency.  Memoization via the memo dict
        ensures each target is built at most once (diamond deps await the
        same task).

        Returns True if the target's output changed (dependents should rebuild).
        """
        if target not in memo:
            memo[target] = asyncio.ensure_future(self._do_build(target, graph, traces, memo, sem))
        return await memo[target]

    async def _do_build(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        memo: dict[str, asyncio.Task[bool]],
        sem: asyncio.Semaphore,
    ) -> bool:
        """Build a single target, recursing into dependencies via gather."""
        rule = graph.get_rule(target)
        if rule is None:
            return False  # Leaf node (source/header file)

        if rule.rule_type == "phony":
            results = await asyncio.gather(*(self._build_async(inp, graph, traces, memo, sem) for inp in rule.inputs))
            return any(results)

        # Ensure order-only deps (directories) exist
        for dep in rule.order_only_deps:
            os.makedirs(dep, exist_ok=True)

        # CONTENT-ADDRESSABLE SHORT-CIRCUIT
        if _is_build_artifact(rule):
            if rule.rule_type == "compile":
                if os.path.exists(target):
                    return False
            else:
                ca = self._ca_target(rule)
                if os.path.exists(ca):
                    _atomic_copy(ca, target)
                    return False

        # SUSPEND: build all inputs concurrently via gather.
        results = await asyncio.gather(*(self._build_async(inp, graph, traces, memo, sem) for inp in rule.inputs))
        any_input_rebuilt = any(results)

        # VERIFY TRACE (non-CA rules only)
        if not _is_build_artifact(rule) and not any_input_rebuilt:
            trace = traces.get(target)
            if trace is not None and self._verify(rule, trace):
                return False  # up to date

        # EXECUTE (semaphore limits subprocess concurrency)
        old_hash = None
        if not _is_build_artifact(rule):
            old_hash = get_file_hash(target) if os.path.exists(target) else None

        assert rule.command is not None, "only rules with commands reach EXECUTE"
        cmd = rule.command
        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 1:
            print(" ".join(cmd), file=sys.stderr)

        flat_cmd = _flatten_command(cmd)

        loop = asyncio.get_running_loop()
        async with sem:
            await loop.run_in_executor(None, self._execute_rule, rule, target, flat_cmd, cmd)

        # CA outputs don't need trace recording or early cutoff
        if _is_build_artifact(rule):
            return True  # New output -> dependents must rebuild

        new_hash = get_file_hash(target)
        traces.put(target, _make_trace_entry(rule, output_hash=new_hash))

        # EARLY CUTOFF
        return old_hash != new_hash

    def _execute_rule(self, rule: BuildRule, target: str, flat_cmd: list[str], cmd: list[str]) -> None:
        """Run the subprocess for a single build rule (called from a thread)."""
        if rule.rule_type == "compile":
            cmd_without_output = flat_cmd[:-2]  # remove [-o, target]
            file_lock = FileLock(target, self.args)
            lock_impl = file_lock.lock
            if lock_impl is not None:
                try:
                    atomic_compile(lock_impl, target, cmd_without_output)
                except subprocess.CalledProcessError as e:
                    print(e.stdout or "", end="", file=sys.stdout)
                    print(e.stderr or "", end="", file=sys.stderr)
                    raise
            else:
                self._atomic_compile_no_lock(target, cmd_without_output)
        elif _is_build_artifact(rule):
            ca = self._ca_target(rule)
            ca_cmd = [ca if a == target else a for a in flat_cmd]
            with FileLock(ca, self.args):
                if os.path.exists(ca) and os.path.getsize(ca) == 0:
                    os.unlink(ca)
                _run_subprocess(ca_cmd, cmd)
            _atomic_copy(ca, target)
        else:
            with FileLock(target, self.args):
                _run_subprocess(flat_cmd, cmd)

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
            with contextlib.suppress(OSError):
                os.unlink(tempfile_path)

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


@register_backend
class SlurmBackend(ShakeBackend):
    """Self-executing backend that distributes compile rules via Slurm."""

    @staticmethod
    def name() -> str:
        return "slurm"

    @staticmethod
    def build_filename() -> str:
        return ".ct-slurm-traces.json"

    @staticmethod
    def add_arguments(cap) -> None:
        cap.add(
            "--slurm-partition",
            default=None,
            help="Slurm partition (queue) for compile jobs. Omit to use the site default partition.",
        )
        cap.add(
            "--slurm-time",
            default="00:30:00",
            help="Wall-clock time limit per compile job (HH:MM:SS). Default: 00:30:00",
        )
        cap.add(
            "--slurm-mem",
            default="16G",
            help="Memory ceiling per compile job (e.g. 16G, 8G). Default: 16G",
        )
        cap.add(
            "--slurm-cpus",
            default=1,
            type=int,
            help="CPUs allocated per compile job. Default: 1",
        )
        cap.add(
            "--slurm-account",
            default=None,
            help="Slurm account/project to charge for compile jobs.",
        )
        cap.add(
            "--slurm-max-array",
            default=1000,
            type=int,
            help="Maximum job-array size per sbatch call. Larger projects are split into "
            "multiple arrays. Default: 1000",
        )
        cap.add(
            "--slurm-poll-interval",
            default=2.0,
            type=float,
            help="Seconds between sacct polls when waiting for compile jobs. Default: 2.0",
        )

    # ------------------------------------------------------------------
    # Core execute() — overrides ShakeBackend's async engine

    def execute(self, target: str = "build") -> None:
        if target == "runtests":
            self._run_tests()
            return

        if self._graph is None:
            raise RuntimeError("generate() must be called before execute()")

        graph = self._graph
        trace_path = os.path.join(self.args.objdir, self.build_filename())
        traces = TraceStore(trace_path)

        # Ensure output directories exist (order-only deps on compile rules)
        for rule in graph.rules_by_type("mkdir"):
            if rule.command:
                subprocess.check_call(rule.command)
            else:
                os.makedirs(rule.output, exist_ok=True)

        # Phase 1: identify compile rules that need rebuilding.
        #   A file is skipped only if it exists AND has a valid trace (output hash matches).
        #   Files with no trace were produced by a crashed build (Phase 4 trace-recording
        #   never ran) and cannot be trusted even if they appear valid on disk.
        #   Failed compile jobs also delete their output immediately via the sbatch wrap
        #   script, so a corrupt artifact from an OOM-kill is normally cleaned up before
        #   the next run.
        to_submit = [
            rule
            for rule in graph.rules_by_type("compile")
            if not os.path.exists(rule.output)
            or not (
                (trace := traces.get(rule.output))  # type: ignore[attr-defined]
                and self._verify(rule, trace)  # type: ignore[attr-defined]
            )
        ]

        # Phase 2: submit compile rules as job arrays grouped by memory tier.
        # Rules are partitioned by estimated memory requirement so each Slurm
        # job array requests only the memory its tasks actually need.  Within
        # each tier, chunking by --slurm-max-array still applies.
        max_array = self.args.slurm_max_array
        # index_map[array_job_id] = list of rules corresponding to task indices 0, 1, …
        index_map: dict[str, list[BuildRule]] = {}

        # Group rules by memory tier (preserving submission order within each tier).
        tiers: dict[str, list[BuildRule]] = collections.defaultdict(list)
        for rule in to_submit:
            mem = self._estimate_memory(rule)
            tiers[mem].append(rule)

        chunk_id = 0
        for mem, tier_rules in tiers.items():
            for chunk_start in range(0, len(tier_rules), max_array):
                chunk = tier_rules[chunk_start : chunk_start + max_array]
                array_job_id = self._sbatch_array(chunk, chunk_id=chunk_id, mem=mem)
                index_map[array_job_id] = chunk
                chunk_id += len(chunk)

        # Phase 3: wait for all arrays to finish, with OOM retry.
        # On OUT_OF_MEMORY, resubmit failed rules with doubled memory
        # (capped at --slurm-mem).  Non-OOM failures are collected and
        # reported after all retries are exhausted.
        all_failures: list[SlurmBackend._TaskFailure] = []
        if index_map:
            failures = self._wait_for_arrays(index_map)
            mem_cap_mb = self._parse_mem(self.args.slurm_mem)

            # Separate OOM from other failures
            oom_rules = [f.rule for f in failures if f.state == self._OOM_STATE]
            non_oom = [f for f in failures if f.state != self._OOM_STATE]
            all_failures.extend(non_oom)

            # Retry OOM failures: double each rule's last memory allocation,
            # capped at --slurm-mem.  Rules are grouped by their doubled memory
            # tier so each Slurm array requests only what its tasks need.
            # Track last memory per rule (starts at original estimate).
            rule_mem: dict[str, str] = {r.output: self._estimate_memory(r) for r in oom_rules}
            while oom_rules:
                # Double each rule's memory; separate those that exceed the cap.
                capped: list[BuildRule] = []
                retryable: list[tuple[BuildRule, str]] = []
                for r in oom_rules:
                    doubled = self._double_mem(rule_mem[r.output])
                    if self._parse_mem(doubled) > mem_cap_mb:
                        capped.append(r)
                    else:
                        rule_mem[r.output] = doubled
                        retryable.append((r, doubled))
                all_failures.extend(
                    SlurmBackend._TaskFailure(rule=r, state=self._OOM_STATE, job_id="retry") for r in capped
                )
                if not retryable:
                    break

                # Group retryable rules by memory tier for efficient array submission.
                retry_tiers: dict[str, list[BuildRule]] = collections.defaultdict(list)
                for r, mem in retryable:
                    retry_tiers[mem].append(r)

                logger.info(
                    "Retrying %d OOM compile job(s) across %d memory tier(s) (cap: %s)",
                    len(retryable),
                    len(retry_tiers),
                    self.args.slurm_mem,
                )
                retry_map: dict[str, list[BuildRule]] = {}
                for mem, tier_rules in retry_tiers.items():
                    for retry_start in range(0, len(tier_rules), max_array):
                        chunk = tier_rules[retry_start : retry_start + max_array]
                        array_job_id = self._sbatch_array(chunk, chunk_id=chunk_id, mem=mem)
                        retry_map[array_job_id] = chunk
                        chunk_id += len(chunk)

                retry_failures = self._wait_for_arrays(retry_map)
                oom_rules = [f.rule for f in retry_failures if f.state == self._OOM_STATE]
                non_oom = [f for f in retry_failures if f.state != self._OOM_STATE]
                all_failures.extend(non_oom)

        if all_failures:
            lines = [f"Job {f.job_id} ({f.rule.output}): {f.state}" for f in all_failures]
            raise RuntimeError("Slurm compile jobs failed:\n" + "\n".join(lines))

        # Phase 4: record traces for successfully built compile rules
        for rule in to_submit:
            if os.path.exists(rule.output):
                traces.put(rule.output, _make_trace_entry(rule))

        # Phase 5: run link/library/other non-compile rules locally in graph order
        for rule in graph.rules:
            if rule.rule_type in ("phony", "mkdir", "compile", "clean"):
                continue
            self._run_local(rule, traces)

        traces.save()
        self._record_link_signatures(graph)

    # ------------------------------------------------------------------
    # Memory estimation

    # Tier thresholds: (max_quoted_includes, slurm_mem_string)
    #
    # Derived from profiling C++20 builds on an HPC cluster (gcc-12, -O3, with a large C++
    # framework).  The number of quoted #include "..." directives in the
    # source file (FileAnalyzer.quoted_headers) correlates strongly with
    # peak RSS (r=0.85) because each quoted include transitively pulls in
    # framework headers and triggers template instantiation.  Unity-build
    # #include "*.C" patterns contribute naturally to this count.
    _MEMORY_TIERS: ClassVar[list[tuple[int, str]]] = [
        (1, "2G"),
        (2, "4G"),
    ]

    def _estimate_memory(self, rule: BuildRule) -> str:
        """Estimate Slurm memory from the source file's quoted-include count.

        rule.include_weight is ``len(FileAnalyzer.quoted_headers)`` for the
        source file, computed in BuildBackend._create_compile_rule() at zero
        cost (analyze_file results are cached from the header dep walk).

        The max tier uses ``--slurm-mem`` (default 16G) so projects can raise
        or lower the ceiling via ct.conf without modifying compiletools source.
        """
        for threshold, mem in self._MEMORY_TIERS:
            if rule.include_weight <= threshold:
                return mem
        return self.args.slurm_mem

    # ------------------------------------------------------------------
    # Slurm helpers

    def _sbatch_array(self, rules: list[BuildRule], chunk_id: int = 0, mem: str | None = None) -> str:
        """Submit *rules* as a single Slurm job array; return the array job ID.

        Each array task (index 0 … N-1) reads its compile command from a
        commands file written to the objdir and executes it.

        *chunk_id* is used to give each chunk a unique commands/outputs filename so
        multiple chunks submitted before the first chunk's tasks start reading do not
        overwrite each other's files.

        *mem* overrides ``--slurm-mem`` for this array (used for per-tier sizing).
        """
        n = len(rules)
        # Write one shell-quoted compile command per line (1-based for sed).
        # Use chunk_id in the filename so concurrent chunks don't collide.
        cmds_file = os.path.join(self.args.objdir, f".ct-slurm-cmds-{chunk_id}.txt")
        outs_file = os.path.join(self.args.objdir, f".ct-slurm-outs-{chunk_id}.txt")
        with open(cmds_file, "w") as fc, open(outs_file, "w") as fo:
            for rule in rules:
                assert rule.command is not None, "compile rules always have a command"
                fc.write(shlex.join(_flatten_command(rule.command)) + "\n")
                fo.write(rule.output + "\n")

        # Each array task reads its compile command and output path, then executes
        # the command. On failure the partial/empty output file is removed immediately
        # so that the CA short-circuit never treats a corrupt artifact as valid.
        # Note: bash -c "$CMD" performs shell expansion.  This is safe because
        # compiletools generates compile commands from compiler paths, flags, and
        # file paths — none contain shell metacharacters ($, backticks).
        wrap = (
            f'CMD=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(cmds_file)}); '
            f'OUT=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(outs_file)}); '
            f'bash -c "$CMD" || {{ rm -f "$OUT"; exit 1; }}'
        )

        effective_mem = mem if mem is not None else self.args.slurm_mem
        cmd = [
            "sbatch",
            "--parsable",
            "--export=ALL",
            f"--array=0-{n - 1}",
            "--job-name=ct-compile",
            f"--time={self.args.slurm_time}",
            f"--mem={effective_mem}",
            f"--cpus-per-task={self.args.slurm_cpus}",
        ]
        if self.args.slurm_partition:
            cmd += ["--partition", self.args.slurm_partition]
        if self.args.slurm_account:
            cmd += ["--account", self.args.slurm_account]
        cmd += ["--wrap", wrap]
        return subprocess.check_output(cmd, text=True).strip()

    def _query_array_task_states(self, array_job_id: str) -> dict[str, str]:
        """Return ``{task_id: state}`` for every task in *array_job_id* via sacct.

        Task IDs are returned as ``"<array_job_id>_<index>"``.
        Sub-steps (.batch, .extern) are skipped.
        """
        out = subprocess.check_output(
            [
                "sacct",
                "-j",
                array_job_id,
                "--format=JobID,State",
                "--noheader",
                "--parsable2",
            ],
            text=True,
        )
        result: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 2:
                continue
            jid = parts[0]
            # Skip sub-steps (.batch, .extern)
            if "." in jid:
                continue
            # State may have a trailing reason, e.g. "FAILED (exit code 1)"
            result[jid] = parts[1].split()[0]
        return result

    _TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"})
    _SUCCESS_STATE = "COMPLETED"
    _OOM_STATE = "OUT_OF_MEMORY"

    @staticmethod
    def _parse_mem(mem_str: str) -> int:
        """Parse a Slurm memory string (e.g. '4G', '512M') to megabytes."""
        s = mem_str.strip().upper()
        if s.endswith("G"):
            return int(s[:-1]) * 1024
        if s.endswith("M"):
            return int(s[:-1])
        return int(s)  # assume megabytes

    @staticmethod
    def _format_mem(mb: int) -> str:
        """Format megabytes as a Slurm memory string (e.g. '4G', '512M')."""
        if mb >= 1024 and mb % 1024 == 0:
            return f"{mb // 1024}G"
        return f"{mb}M"

    @staticmethod
    def _double_mem(mem_str: str) -> str:
        """Double a Slurm memory string (e.g. '4G' -> '8G')."""
        return SlurmBackend._format_mem(SlurmBackend._parse_mem(mem_str) * 2)

    @dataclass
    class _TaskFailure:
        """Structured info about a failed Slurm task."""

        rule: BuildRule
        state: str
        job_id: str

    def _wait_for_arrays(self, index_map: dict[str, list[BuildRule]]) -> list[SlurmBackend._TaskFailure]:
        """Poll sacct until every task in every array reaches a terminal state.

        *index_map* maps array_job_id → ordered list of rules (index == task index).
        Returns a list of _TaskFailure for failed tasks (empty if all succeeded).
        Raises RuntimeError if sacct polling times out.
        """
        poll_interval = self.args.slurm_poll_interval
        max_polls = max(1, int(1800 / max(poll_interval, 0.1)))
        polls = 0

        pending: set[str] = set(index_map)
        failures: list[SlurmBackend._TaskFailure] = []

        while pending:
            if polls >= max_polls:
                raise RuntimeError(
                    f"Timed out after {polls} sacct polls waiting for Slurm arrays: " + ", ".join(sorted(pending))
                )
            polls += 1

            still_pending: set[str] = set()
            for array_job_id in pending:
                rules = index_map[array_job_id]
                states = self._query_array_task_states(array_job_id)

                terminal_tasks = {jid: st for jid, st in states.items() if st in self._TERMINAL_STATES and "_" in jid}
                if len(terminal_tasks) < len(rules):
                    still_pending.add(array_job_id)
                    continue

                for jid, st in terminal_tasks.items():
                    if st != self._SUCCESS_STATE:
                        try:
                            idx = int(jid[len(array_job_id) + 1 :])
                            rule = rules[idx]
                            if os.path.exists(rule.output):
                                os.remove(rule.output)
                        except (ValueError, IndexError, OSError):
                            rule = rules[0]  # fallback
                        failures.append(SlurmBackend._TaskFailure(rule=rule, state=st, job_id=jid))

            pending = still_pending
            if pending:
                time.sleep(poll_interval)

        return failures

    # ------------------------------------------------------------------
    # Local execution for link/library rules

    def _run_local(self, rule: BuildRule, traces: TraceStore) -> None:
        """Run a link/library/copy rule locally, with CA short-circuit.

        Uses FileLock around CA target creation to prevent races when
        concurrent ct-cake processes share the same objdir (e.g. on GPFS).
        """
        if rule.command is None:
            return

        flat_cmd = _flatten_command(rule.command)

        if rule.rule_type in ("link", "static_library", "shared_library"):
            ca = self._ca_target(rule)  # type: ignore[attr-defined]
            if os.path.exists(ca):
                _atomic_copy(ca, rule.output)
                return
            # Ensure output directory exists
            out_dir = os.path.dirname(rule.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            # Build to CA target so the result is content-addressable
            ca_cmd = [ca if a == rule.output else a for a in flat_cmd]
            ca_dir = os.path.dirname(ca)
            if ca_dir:
                os.makedirs(ca_dir, exist_ok=True)
            with FileLock(ca, self.args):
                # FlockLock creates an empty file at the CA path via O_CREAT.
                # ar -r treats an empty file as a corrupt archive and fails
                # with "File format not recognized".  Remove the empty file
                # so ar can create a fresh archive.
                if os.path.exists(ca) and os.path.getsize(ca) == 0:
                    os.unlink(ca)
                _run_subprocess(ca_cmd, rule.command)
            _atomic_copy(ca, rule.output)
        else:
            with FileLock(rule.output, self.args):
                _run_subprocess(flat_cmd, rule.command)

        traces.put(rule.output, _make_trace_entry(rule))

    def _execute_build(self, target: str) -> None:
        # SlurmBackend is self-executing via execute(); this path is never used.
        raise NotImplementedError  # pragma: no cover
