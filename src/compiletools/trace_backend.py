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

Assumes a shared network filesystem (GPFS, Lustre, NFS, etc.) visible to
both the submission node and all compute nodes — source files, object
directories, and the working directory must be accessible at the same
paths on every node.  The implementation accounts for metadata-visibility
lag common on network filesystems (fsync before close, polling for output
files after job completion).

The dependency graph is static (pre-computed by Hunter), not dynamic as in the
original Shake (which uses monadic tasks for dynamic dependency discovery).
This is sufficient because compiletools resolves all dependencies at a higher
level before the backend executes.

No external build tool required for either backend — both drive compilation
directly from Python.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import glob
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import ClassVar

import compiletools.apptools
import compiletools.filesystem_utils
import compiletools.wrappedos
from compiletools.build_backend import (
    BuildBackend,
    _compiler_identity,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.locking import FileLock, atomic_compile, atomic_link

logger = logging.getLogger(__name__)

TRACE_VERSION = 1

# LD_LIBRARY_PATH is included because non-system-installed compilers (Spack, Lmod,
# environment modules, custom installs) almost always need it to find their shared
# libs on the compute node. Other HPC vars (MODULEPATH, LMOD_*, SPACK_ROOT, etc.)
# are deliberately excluded — sites using those toolchains can extend this via
# --slurm-export.
_DEFAULT_SLURM_EXPORT = "PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH"


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
        except json.JSONDecodeError as e:
            logger.warning("trace store %s is corrupt (%s); discarding", self._path, e)
            self._traces = {}
            return
        if not isinstance(data, dict) or data.get("version") != TRACE_VERSION:
            return
        for output, entry_dict in data.get("traces", {}).items():
            try:
                self._traces[output] = TraceEntry(**entry_dict)
            except (KeyError, TypeError) as e:
                logger.warning("dropping corrupt trace entry for %s: %s", output, e)

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


def hash_command(cmd: list[str], compiler_identity: str | None = None) -> str:
    """Compute a stable hash of a shell command list.

    *compiler_identity* folds in the resolved binary's realpath + size + mtime
    for the tool that runs the command, so an in-place compiler upgrade
    invalidates traces even when the argv is byte-identical.
    """
    payload = [compiler_identity, cmd] if compiler_identity is not None else cmd
    return hashlib.sha1(json.dumps(payload, sort_keys=False).encode()).hexdigest()


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
    """Return command as-is. Producers in build_backend.py pre-split flag
    strings (CXXFLAGS/CFLAGS/LDFLAGS) before constructing rule.command, so
    no re-splitting is needed here. A second shlex.split would corrupt args
    like ``-DGREETING=Hello World`` whose value contains a literal space.
    Kept as a no-op for back-compat with existing call sites and tests.
    """
    return list(command)


def _parse_slurm_elapsed(elapsed_str: str) -> float:
    """Parse sacct Elapsed field (HH:MM:SS or D-HH:MM:SS) to seconds."""
    days = 0
    if "-" in elapsed_str:
        day_part, elapsed_str = elapsed_str.split("-", 1)
        days = int(day_part)
    parts = elapsed_str.split(":")
    if len(parts) == 3:
        return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return days * 86400 + int(parts[0]) * 60 + float(parts[1])
    return float(elapsed_str)


def _make_trace_entry(rule: BuildRule, context, output_hash: str | None = None) -> TraceEntry:
    """Build a TraceEntry for a successfully executed rule.

    Pass *output_hash* when already computed (avoids a redundant disk read).
    """
    assert rule.command is not None, "only call _make_trace_entry after a rule executes"
    input_hashes = {}
    for p in rule.inputs:
        if os.path.isfile(p):
            input_hashes[compiletools.wrappedos.realpath(p)] = get_file_hash(p, context)
        else:
            logger.debug("_make_trace_entry: skipping non-file input %s for %s", p, rule.output)
    identity = _compiler_identity(rule.command[0]) if rule.command else None
    return TraceEntry(
        output_hash=output_hash if output_hash is not None else get_file_hash(rule.output, context),
        input_hashes=input_hashes,
        command_hash=hash_command(rule.command, compiler_identity=identity),
    )


@register_backend
class ShakeBackend(BuildBackend):
    """Self-executing backend using Shake-style verifying traces."""

    def __init__(self, args, hunter, *, context=None):
        super().__init__(args, hunter, context=context)
        self._graph: BuildGraph | None = None

    @staticmethod
    def name() -> str:
        return "shake"

    @staticmethod
    def tool_command() -> str | None:
        # Self-executing — runs each rule directly via subprocess.
        return None

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

        try:
            asyncio.run(self._build_async(target, self._graph, traces, memo, sem))
        finally:
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
            old_hash = get_file_hash(target, self.context) if os.path.exists(target) else None

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

        new_hash = get_file_hash(target, self.context)
        traces.put(target, _make_trace_entry(rule, self.context, output_hash=new_hash))

        # EARLY CUTOFF
        return old_hash != new_hash

    def _execute_rule(self, rule: BuildRule, target: str, flat_cmd: list[str], cmd: list[str]) -> None:
        """Run the subprocess for a single build rule (called from a thread)."""
        start = time.monotonic()
        if rule.rule_type == "compile":
            try:
                o_idx = flat_cmd.index("-o")
            except ValueError as e:
                raise AssertionError(f"compile rule missing -o flag: {flat_cmd}") from e
            cmd_without_output = flat_cmd[:o_idx] + flat_cmd[o_idx + 2 :]
            lock_impl = FileLock(target, self.args).lock
            atomic_compile(lock_impl, target, cmd_without_output)
        elif _is_build_artifact(rule):
            ca = self._ca_target(rule)
            ca_cmd = [ca if a == target else a for a in flat_cmd]
            lock_impl = FileLock(ca, self.args).lock
            atomic_link(lock_impl, ca, ca_cmd)
            _atomic_copy(ca, target)
        else:
            lock_impl = FileLock(target, self.args).lock
            atomic_link(lock_impl, target, flat_cmd)

        # Record per-rule timing
        elapsed = time.monotonic() - start
        timer = self._timer
        if timer:
            source = rule.inputs[0] if rule.inputs else ""
            timer.record_rule(
                rule_type=rule.rule_type,
                target=target,
                source=source,
                elapsed_s=elapsed,
                start_s=start,
                end_s=start + elapsed,
            )

    def _verify(self, rule, trace: TraceEntry) -> bool:
        """Check if a trace is still valid (output exists, inputs unchanged, same command)."""
        assert rule.command is not None, "_verify only applies to rules with commands"
        try:
            if get_file_hash(rule.output, self.context) != trace.output_hash:
                return False
        except (FileNotFoundError, OSError):
            return False

        identity = _compiler_identity(rule.command[0]) if rule.command else None
        if hash_command(rule.command, compiler_identity=identity) != trace.command_hash:
            return False

        canonical_inputs = {compiletools.wrappedos.realpath(p) for p in rule.inputs}
        if canonical_inputs != set(trace.input_hashes.keys()):
            return False

        for inp, stored_hash in trace.input_hashes.items():
            try:
                current_hash = get_file_hash(inp, self.context)
            except (FileNotFoundError, OSError):
                return False
            if current_hash != stored_hash:
                return False

        return True


_DEFAULT_MEM_TIERS_STR = "1:1G,2:2G,4:4G,8:8G,16:16G"


def _parse_mem_str(mem_str: str) -> int:
    """Parse a Slurm memory string (e.g. '4G', '512M') to megabytes."""
    s = mem_str.strip().upper()
    if not s:
        raise ValueError("empty memory value")
    if s.endswith("G"):
        return int(s[:-1]) * 1024
    if s.endswith("M"):
        return int(s[:-1])
    return int(s)


def _slurm_mem_arg(value: str) -> str:
    try:
        if _parse_mem_str(value) <= 0:
            raise ValueError("memory must be positive")
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid Slurm memory '{value}': {e} (expected '<int>G', '<int>M', or '<int>')"
        ) from e
    return value


def _slurm_time_arg(value: str) -> str:
    """Validate Slurm wall-clock time format (HH:MM:SS or D-HH:MM:SS)."""
    s = value.strip()
    if not s:
        raise argparse.ArgumentTypeError("invalid Slurm time: empty")
    rest = s
    if "-" in rest:
        day_str, rest = rest.split("-", 1)
        try:
            if int(day_str) < 0:
                raise ValueError("days must be non-negative")
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': bad days field") from e
    parts = rest.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': expected HH:MM:SS or D-HH:MM:SS")
    try:
        for p in parts:
            if int(p) < 0:
                raise ValueError("time fields must be non-negative")
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': {e}") from e
    return value


def _slurm_mem_tiers_arg(value: str) -> list[tuple[int, str]]:
    """Parse '<threshold>:<mem>,<threshold>:<mem>,...' into a sorted tier list."""
    if not value or not value.strip():
        raise argparse.ArgumentTypeError("invalid --slurm-mem-tiers: empty")
    tiers: list[tuple[int, str]] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers entry '{entry}': expected '<threshold>:<mem>'")
        thr_str, mem_str = entry.split(":", 1)
        try:
            threshold = int(thr_str.strip())
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers threshold '{thr_str}': {e}") from e
        mem = mem_str.strip()
        try:
            _parse_mem_str(mem)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers memory '{mem}': {e}") from e
        tiers.append((threshold, mem))
    if not tiers:
        raise argparse.ArgumentTypeError("invalid --slurm-mem-tiers: no entries")
    tiers.sort(key=lambda t: t[0])
    return tiers


@register_backend
class SlurmBackend(ShakeBackend):
    """Self-executing backend that distributes compile rules via Slurm."""

    @staticmethod
    def name() -> str:
        return "slurm"

    @staticmethod
    def tool_command() -> str:
        # Slurm jobs are submitted via sbatch.
        return "sbatch"

    @staticmethod
    def build_filename() -> str:
        return ".ct-slurm-traces.json"

    @staticmethod
    def add_arguments(cap) -> None:
        if compiletools.apptools._parser_has_option(cap, "--slurm-partition"):
            return
        cap.add(
            "--slurm-partition",
            default=None,
            help="Slurm partition (queue) for compile jobs. Omit to use the site default partition.",
        )
        cap.add(
            "--slurm-time",
            default="00:30:00",
            type=_slurm_time_arg,
            help="Wall-clock time limit per compile job (HH:MM:SS or D-HH:MM:SS). Default: 00:30:00",
        )
        cap.add(
            "--slurm-mem",
            default="16G",
            type=_slurm_mem_arg,
            help="Memory ceiling per compile job (e.g. 16G, 8G, 512M). Default: 16G",
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
        cap.add(
            "--slurm-job-name",
            default="ct-compile",
            help="Name applied to submitted Slurm jobs (visible in squeue/sacct). "
            "Default: ct-compile. Useful for distinguishing concurrent ct-cake invocations.",
        )
        cap.add(
            "--slurm-mem-tiers",
            default=_DEFAULT_MEM_TIERS_STR,
            type=_slurm_mem_tiers_arg,
            help="Memory tier mapping as 'threshold:mem,threshold:mem,...' where threshold is "
            "the maximum quoted-include count for that tier. Rules whose include_weight exceeds "
            "the largest threshold use --slurm-mem. Default: " + _DEFAULT_MEM_TIERS_STR,
        )
        cap.add(
            "--slurm-sacct-failure-threshold",
            default=10,
            type=int,
            help="Consecutive sacct failures tolerated before _wait_for_arrays raises. Default: 10",
        )
        cap.add(
            "--slurm-output-wait-timeout",
            default=30.0,
            type=float,
            help="Seconds to wait for compiled outputs to become visible on the submitter "
            "after sacct reports COMPLETED (network filesystem metadata lag). Default: 30.0",
        )
        cap.add(
            "--slurm-export",
            default=_DEFAULT_SLURM_EXPORT,
            help="Value passed to sbatch --export=. Default propagates a curated allowlist "
            f"({_DEFAULT_SLURM_EXPORT}) instead of the submitter's full environment. "
            "Use 'ALL' to restore legacy behavior, 'NONE' for a fully isolated environment, "
            "or extend the default for Lmod/Spack sites (e.g. "
            "'PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH,MODULEPATH,LMOD_CMD'). "
            "See README.ct-backends for guidance.",
        )
        cap.add(
            "--slurm-rule-retry-cap",
            default=3,
            type=int,
            help="Maximum OOM retries per rule before that rule is abandoned. Default: 3",
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

        # Per-invocation prefix for cmds/outs/log files.  Prevents collisions
        # between concurrent ct-cake processes sharing the same objdir, and
        # bounds cleanup to files this invocation actually produced.
        self._invocation_prefix = f"{os.getpid()}-{int(time.monotonic_ns())}"
        self._created_aux_files: list[str] = []
        self._tracked_jobs: dict[str, str] = {}  # job_id -> "pending"|"terminal"

        # Index from job_id -> chunk_id, so log lookups don't have to glob.
        self._chunk_id_for_job: dict[str, int] = {}

        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _on_signal(signum, frame):  # pragma: no cover - exercised via thread test
            self._scancel_pending()
            # Restore default handler and re-raise so normal interrupt semantics apply
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        # Only install handlers on the main thread; otherwise signal.signal raises.
        installed_handlers = False
        try:
            signal.signal(signal.SIGINT, _on_signal)
            signal.signal(signal.SIGTERM, _on_signal)
            installed_handlers = True
        except (ValueError, OSError):
            pass

        try:
            self._execute_impl(graph, traces)
        finally:
            try:
                self._scancel_pending()
            finally:
                try:
                    self._cleanup_invocation_files()
                finally:
                    try:
                        traces.save()
                    finally:
                        if installed_handlers:
                            with contextlib.suppress(Exception):
                                signal.signal(signal.SIGINT, prev_sigint)
                                signal.signal(signal.SIGTERM, prev_sigterm)

    def _execute_impl(self, graph: BuildGraph, traces: TraceStore) -> None:
        # Ensure output directories exist (order-only deps on compile rules)
        for rule in graph.rules_by_type("mkdir"):
            if rule.command:
                subprocess.check_call(rule.command)
            else:
                os.makedirs(rule.output, exist_ok=True)

        # Phase 1: identify compile rules that need rebuilding.
        to_submit = [
            rule
            for rule in graph.rules_by_type("compile")
            if not os.path.exists(rule.output)
            or not (
                (trace := traces.get(rule.output))  # type: ignore[attr-defined]
                and self._verify(rule, trace)  # type: ignore[attr-defined]
            )
        ]

        max_array = self.args.slurm_max_array
        index_map: dict[str, list[BuildRule]] = {}

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
                self._tracked_jobs[array_job_id] = "pending"
                self._chunk_id_for_job[array_job_id] = chunk_id
                chunk_id += len(chunk)

        all_failures: list[SlurmBackend._TaskFailure] = []
        retry_cap = getattr(self.args, "slurm_rule_retry_cap", 3)
        per_rule_retries: dict[str, int] = collections.defaultdict(int)

        try:
            if index_map:
                failures = self._wait_for_arrays(index_map)
                mem_cap_mb = self._parse_mem(self.args.slurm_mem)

                oom_rules = [f.rule for f in failures if f.state == self._OOM_STATE]
                non_oom = [f for f in failures if f.state != self._OOM_STATE]
                all_failures.extend(non_oom)

                rule_mem: dict[str, str] = {r.output: self._estimate_memory(r) for r in oom_rules}
                while oom_rules:
                    capped: list[BuildRule] = []
                    retryable: list[tuple[BuildRule, str]] = []
                    abandoned: list[BuildRule] = []
                    for r in oom_rules:
                        if per_rule_retries[r.output] >= retry_cap:
                            abandoned.append(r)
                            continue
                        doubled = self._double_mem(rule_mem[r.output])
                        if self._parse_mem(doubled) > mem_cap_mb:
                            capped.append(r)
                        else:
                            rule_mem[r.output] = doubled
                            retryable.append((r, doubled))
                    all_failures.extend(
                        SlurmBackend._TaskFailure(rule=r, state=self._OOM_STATE, job_id="retry") for r in capped
                    )
                    all_failures.extend(
                        SlurmBackend._TaskFailure(rule=r, state=self._OOM_STATE, job_id=f"retry-cap-{retry_cap}")
                        for r in abandoned
                    )
                    if not retryable:
                        break

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
                            self._tracked_jobs[array_job_id] = "pending"
                            self._chunk_id_for_job[array_job_id] = chunk_id
                            chunk_id += len(chunk)

                    for r, _ in retryable:
                        per_rule_retries[r.output] += 1

                    retry_failures = self._wait_for_arrays(retry_map)
                    oom_rules = [f.rule for f in retry_failures if f.state == self._OOM_STATE]
                    non_oom = [f for f in retry_failures if f.state != self._OOM_STATE]
                    all_failures.extend(non_oom)
        finally:
            # Always collect timing for any array we actually waited on, even
            # if a later step raises.
            with contextlib.suppress(Exception):
                self._collect_timing(index_map)

        if all_failures:
            lines = [f"Job {f.job_id} ({f.rule.output}): {f.state}" for f in all_failures]
            diag = self._read_slurm_logs_for_failures(all_failures)
            msg = "Slurm compile jobs failed:\n" + "\n".join(lines)
            if diag:
                msg += "\n\n" + diag
            raise RuntimeError(msg)

        # Phase 4: record traces for successfully built compile rules.
        # Done before _wait_for_output_files so partial progress survives a
        # missing-output raise.
        for rule in to_submit:
            if os.path.exists(rule.output):
                traces.put(rule.output, _make_trace_entry(rule, self.context))

        has_link_rules = any(r.rule_type not in ("phony", "mkdir", "compile", "clean") for r in graph.rules)
        if to_submit and has_link_rules:
            timeout = getattr(self.args, "slurm_output_wait_timeout", 30.0)
            try:
                self._wait_for_output_files(to_submit, timeout=timeout)
            except RuntimeError as e:
                # Save traces for completed compiles before re-raising so the
                # next invocation doesn't re-submit them.  Slurm logs are
                # preserved so the user can diagnose the missing output.
                self._save_traces_for_completed(to_submit, traces)
                log_paths = self._invocation_log_paths()
                if log_paths:
                    raise RuntimeError(
                        f"{e}\n\nSlurm logs preserved for diagnosis ({len(log_paths)} file(s)):\n"
                        + "\n".join(f"  {p}" for p in log_paths[:10])
                        + ("" if len(log_paths) <= 10 else f"\n  ... and {len(log_paths) - 10} more")
                    ) from e
                raise

        self._cleanup_slurm_logs()

        # Phase 5: run link/library/other non-compile rules locally in graph order
        timer = self._timer
        for rule in graph.rules:
            if rule.rule_type in ("phony", "mkdir", "compile", "clean"):
                continue
            start = time.monotonic()
            self._run_local(rule, traces)
            if timer:
                elapsed = time.monotonic() - start
                source = rule.inputs[0] if rule.inputs else ""
                timer.record_rule(
                    rule_type=rule.rule_type,
                    target=rule.output,
                    source=source,
                    elapsed_s=elapsed,
                )

        self._record_link_signatures(graph)

    # ------------------------------------------------------------------
    # Memory estimation

    # Default tier thresholds: (max_quoted_includes, slurm_mem_string).
    #
    # Derived from profiling C++20 builds on an HPC cluster (gcc-12, -O3, with a large C++
    # framework).  Quoted #include count correlates strongly with peak RSS
    # (r=0.85) because each quoted include transitively pulls in framework
    # headers and triggers template instantiation.  Unity-build patterns
    # contribute naturally to this count.  Override via --slurm-mem-tiers.
    _MEMORY_TIERS: ClassVar[list[tuple[int, str]]] = [
        (1, "1G"),
        (2, "2G"),
        (4, "4G"),
        (8, "8G"),
        (16, "16G"),
    ]

    def _estimate_memory(self, rule: BuildRule) -> str:
        """Estimate Slurm memory from the source file's quoted-include count.

        rule.include_weight is ``len(FileAnalyzer.quoted_headers)`` for the
        source file, computed in BuildBackend._create_compile_rule() at zero
        cost (analyze_file results are cached from the header dep walk).

        Uses --slurm-mem-tiers if configured, otherwise the class default.
        Rules whose include_weight exceeds the largest threshold use
        ``--slurm-mem`` (the per-job ceiling).
        """
        tiers = getattr(self.args, "slurm_mem_tiers", None) or self._MEMORY_TIERS
        for threshold, mem in tiers:
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
        real_objdir = compiletools.wrappedos.realpath(self.args.objdir)

        # Per-invocation prefix prevents collisions with peer ct-cake processes
        # sharing the same objdir on a network filesystem.
        prefix = getattr(self, "_invocation_prefix", f"{os.getpid()}-{int(time.monotonic_ns())}")
        cmds_file = os.path.join(real_objdir, f".ct-slurm-cmds-{prefix}-{chunk_id}.txt")
        outs_file = os.path.join(real_objdir, f".ct-slurm-outs-{prefix}-{chunk_id}.txt")
        with open(cmds_file, "w") as fc, open(outs_file, "w") as fo:
            for rule in rules:
                assert rule.command is not None, "compile rules always have a command"
                fc.write(shlex.join(_flatten_command(rule.command)) + "\n")
                fo.write(rule.output + "\n")
            fc.flush()
            fo.flush()
            os.fsync(fc.fileno())
            os.fsync(fo.fileno())

        # Track for end-of-build cleanup.
        if hasattr(self, "_created_aux_files"):
            self._created_aux_files.append(cmds_file)
            self._created_aux_files.append(outs_file)

        # eval "$CMD" runs the line read from cmds_file as a shell command.
        # The line was produced by shlex.join, so each token is single-quoted
        # and metacharacters like $, backticks, parentheses are literal — eval
        # parses the quoting once and produces argv without re-expansion.
        wrap = (
            f'CMD=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(cmds_file)}); '
            f'OUT=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(outs_file)}); '
            f'[ -n "$CMD" ] || {{ echo "ct-compile: empty command (index $SLURM_ARRAY_TASK_ID)" >&2; exit 1; }}; '
            f'eval "$CMD" || {{ rm -f "$OUT"; exit 1; }}'
        )

        effective_mem = mem if mem is not None else self.args.slurm_mem
        slurm_log = os.path.join(real_objdir, f"slurm-ct-{prefix}-{chunk_id}-%a.out")
        export_value = getattr(self.args, "slurm_export", _DEFAULT_SLURM_EXPORT)
        cmd = [
            "sbatch",
            "--parsable",
            f"--export={export_value}",
            f"--chdir={os.getcwd()}",
            f"--array=0-{n - 1}",
            f"--job-name={getattr(self.args, 'slurm_job_name', 'ct-compile')}",
            f"--time={self.args.slurm_time}",
            f"--mem={effective_mem}",
            f"--cpus-per-task={self.args.slurm_cpus}",
            f"--output={slurm_log}",
            f"--error={slurm_log}",
        ]
        if self.args.slurm_partition:
            cmd += ["--partition", self.args.slurm_partition]
        if self.args.slurm_account:
            cmd += ["--account", self.args.slurm_account]
        cmd += ["--wrap", wrap]
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE).strip()
        except subprocess.CalledProcessError as e:
            stderr_text = (e.stderr or "").strip()
            raise RuntimeError(f"sbatch failed (exit {e.returncode}): {stderr_text or '<no stderr>'}") from e
        except FileNotFoundError as e:
            raise RuntimeError(f"sbatch not found on PATH: {e}") from e

    def _collect_timing(self, index_map: dict[str, list[BuildRule]]) -> None:
        """Collect per-job timing from Slurm accounting via sacct."""
        timer = self._timer
        if not timer or not index_map:
            return
        for array_job_id, rules in index_map.items():
            try:
                out = subprocess.check_output(
                    [
                        "sacct",
                        "-j",
                        array_job_id,
                        "--format=JobID,Elapsed,State",
                        "--noheader",
                        "--parsable2",
                    ],
                    text=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.debug("sacct unavailable for job %s: %s", array_job_id, e)
                continue
            for line in out.splitlines():
                parts = line.strip().split("|")
                if len(parts) < 3:
                    continue
                jid = parts[0]
                if "." in jid:
                    continue  # skip sub-steps
                elapsed_str = parts[1]
                state = parts[2].split()[0]
                if state != "COMPLETED":
                    continue
                # Parse task index from job ID (format: "array_job_id_index")
                jid_parts = jid.rsplit("_", 1)
                if len(jid_parts) != 2:
                    continue
                try:
                    task_idx = int(jid_parts[1])
                except ValueError:
                    continue
                if task_idx >= len(rules):
                    continue
                rule = rules[task_idx]
                elapsed_s = _parse_slurm_elapsed(elapsed_str)
                source = rule.inputs[0] if rule.inputs else ""
                timer.record_rule(
                    rule_type=rule.rule_type,
                    target=rule.output,
                    source=source,
                    elapsed_s=elapsed_s,
                )

    def _invocation_log_paths(self) -> list[str]:
        """Slurm log paths produced by THIS invocation (no cross-invocation glob)."""
        prefix = getattr(self, "_invocation_prefix", None)
        if not prefix:
            return []
        return sorted(glob.glob(os.path.join(self.args.objdir, f"slurm-ct-{prefix}-*.out")))

    def _cleanup_slurm_logs(self) -> None:
        """Remove THIS invocation's slurm log files when verbosity is low."""
        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 2:
            return
        for f in self._invocation_log_paths():
            with contextlib.suppress(OSError):
                os.remove(f)

    def _cleanup_invocation_files(self) -> None:
        """Remove cmds/outs files this invocation created. Best effort."""
        for f in getattr(self, "_created_aux_files", []):
            with contextlib.suppress(OSError):
                os.remove(f)
        self._created_aux_files = []

    def _scancel_pending(self) -> None:
        """Cancel any tracked Slurm jobs not yet known to be terminal. Never raises."""
        pending = [jid for jid, status in getattr(self, "_tracked_jobs", {}).items() if status != "terminal"]
        if not pending:
            return
        try:
            result = subprocess.run(
                ["scancel", *pending],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "scancel returned %s for jobs %s: %s",
                    result.returncode,
                    pending,
                    (result.stderr or "").strip(),
                )
            else:
                logger.info("scancel cancelled pending Slurm jobs: %s", pending)
        except FileNotFoundError:
            logger.warning("scancel not found on PATH; %d Slurm job(s) may still be pending: %s", len(pending), pending)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("scancel failed for jobs %s: %s", pending, e)
        finally:
            for jid in pending:
                self._tracked_jobs[jid] = "terminal"

    def _save_traces_for_completed(self, rules: list[BuildRule], traces: TraceStore) -> None:
        """Record trace entries for rules whose output exists on disk, then save."""
        for rule in rules:
            if os.path.exists(rule.output):
                traces.put(rule.output, _make_trace_entry(rule, self.context))
        traces.save()

    def _read_slurm_logs_for_failures(self, failures: list[SlurmBackend._TaskFailure]) -> str:
        """Read slurm log content for failed tasks and return formatted diagnostics.

        Log files for a chunk are named ``slurm-ct-<prefix>-<chunk_id>-<array_index>.out``.
        Looks up the exact chunk_id for the failure's array job to avoid matching
        retry chunks that share the same array_index.
        """
        diagnostics: list[str] = []
        prefix = getattr(self, "_invocation_prefix", None)
        for f in failures:
            parts = f.job_id.rsplit("_", 1)
            if len(parts) != 2:
                continue
            array_job_id, task_idx = parts
            chunk_id = getattr(self, "_chunk_id_for_job", {}).get(array_job_id)
            if chunk_id is None or prefix is None:
                # No chunk index available (e.g. synthetic 'retry'/'retry-cap' job_ids).
                continue
            log_path = os.path.join(self.args.objdir, f"slurm-ct-{prefix}-{chunk_id}-{task_idx}.out")
            try:
                with open(log_path) as fh:
                    content = fh.read().strip()
                if content:
                    diagnostics.append(f"--- {f.rule.output} (job {f.job_id}) ---\n{content}")
            except OSError:
                pass
        return "\n".join(diagnostics)

    def _query_array_task_states(self, array_job_id: str) -> dict[str, str]:
        """Return ``{task_id: state}`` for every task in *array_job_id* via sacct.

        Task IDs are returned as ``"<array_job_id>_<index>"``.
        Sub-steps (.batch, .extern) are skipped.

        Returns an empty dict on transient sacct failure (slurmdbd hiccup,
        sacct missing); the polling loop's failure counter handles persistent
        failure.
        """
        try:
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
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as e:
            stderr_text = (e.stderr or "").strip()
            logger.warning(
                "sacct failed for job %s (exit %s): %s",
                array_job_id,
                e.returncode,
                stderr_text or "<no stderr>",
            )
            return {}
        except FileNotFoundError as e:
            logger.warning("sacct not found on PATH: %s", e)
            return {}

        result: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 2:
                continue
            jid = parts[0]
            if "." in jid:
                continue
            result[jid] = parts[1].split()[0]
        return result

    _TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"})
    _SUCCESS_STATE = "COMPLETED"
    _OOM_STATE = "OUT_OF_MEMORY"

    @staticmethod
    def _parse_mem(mem_str: str) -> int:
        """Parse a Slurm memory string (e.g. '4G', '512M') to megabytes."""
        return _parse_mem_str(mem_str)

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
        Raises RuntimeError if sacct polling times out, or if sacct fails
        consecutively more than --slurm-sacct-failure-threshold times.
        """
        poll_interval = self.args.slurm_poll_interval
        max_polls = max(1, int(1800 / max(poll_interval, 0.1)))
        polls = 0
        failure_threshold = getattr(self.args, "slurm_sacct_failure_threshold", 10)
        consecutive_failures = 0

        pending: set[str] = set(index_map)
        failures: list[SlurmBackend._TaskFailure] = []

        while pending:
            if polls >= max_polls:
                raise RuntimeError(
                    f"Timed out after {polls} sacct polls waiting for Slurm arrays: " + ", ".join(sorted(pending))
                )
            polls += 1

            still_pending: set[str] = set()
            any_response = False
            for array_job_id in pending:
                rules = index_map[array_job_id]
                states = self._query_array_task_states(array_job_id)
                if states:
                    any_response = True

                terminal_tasks = {jid: st for jid, st in states.items() if st in self._TERMINAL_STATES and "_" in jid}
                if len(terminal_tasks) < len(rules):
                    still_pending.add(array_job_id)
                    continue

                # All tasks terminal — mark the parent job terminal so scancel skips it.
                if hasattr(self, "_tracked_jobs"):
                    self._tracked_jobs[array_job_id] = "terminal"

                for jid, st in terminal_tasks.items():
                    if st != self._SUCCESS_STATE:
                        try:
                            idx = int(jid[len(array_job_id) + 1 :])
                            rule = rules[idx]
                            if os.path.exists(rule.output):
                                os.remove(rule.output)
                        except (ValueError, IndexError, OSError):
                            rule = rules[0]
                        failures.append(SlurmBackend._TaskFailure(rule=rule, state=st, job_id=jid))

            if any_response:
                consecutive_failures = 0
            elif pending:
                consecutive_failures += 1
                if consecutive_failures >= failure_threshold:
                    raise RuntimeError(
                        f"sacct returned no usable data for {consecutive_failures} consecutive polls "
                        f"(threshold={failure_threshold}); pending arrays: " + ", ".join(sorted(pending))
                    )

            pending = still_pending
            if pending:
                time.sleep(poll_interval)

        return failures

    def _wait_for_output_files(self, rules: list[BuildRule], timeout: float = 30.0) -> None:
        """Wait for compiled output files to become visible on this node.

        On network filesystems, sacct may report a job as COMPLETED before
        the output file metadata has propagated to the submission node.
        This polls briefly so that subsequent link steps don't fail with
        missing .o files.
        """
        missing = [r for r in rules if not os.path.exists(r.output)]
        if not missing:
            return

        deadline = time.monotonic() + timeout
        interval = 0.1
        while missing and time.monotonic() < deadline:
            time.sleep(interval)
            interval = min(interval * 2, 2.0)
            missing = [r for r in missing if not os.path.exists(r.output)]

        if missing:
            names = ", ".join(os.path.basename(r.output) for r in missing[:5])
            msg = (
                f"Slurm reported jobs COMPLETED but {len(missing)} output file(s) "
                f"are still missing after {timeout:.0f}s "
                f"(network filesystem metadata lag, or sacct false-positive): {names}"
            )
            print(f"ct-slurm: {msg}", file=sys.stderr)
            raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Local execution for link/library rules

    def _run_local(self, rule: BuildRule, traces: TraceStore) -> None:
        """Run a link/library/copy rule locally, with CA short-circuit.

        Non-build-artifact rules (e.g. copy) consult the trace store first so
        the rule is not re-executed on every build when its inputs are unchanged.
        """
        if rule.command is None:
            return

        flat_cmd = _flatten_command(rule.command)

        if rule.rule_type in ("link", "static_library", "shared_library"):
            ca = self._ca_target(rule)
            if os.path.exists(ca):
                _atomic_copy(ca, rule.output)
                return
            out_dir = os.path.dirname(rule.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            ca_cmd = [ca if a == rule.output else a for a in flat_cmd]
            ca_dir = os.path.dirname(ca)
            if ca_dir:
                os.makedirs(ca_dir, exist_ok=True)
            lock_impl = FileLock(ca, self.args).lock
            atomic_link(lock_impl, ca, ca_cmd)
            _atomic_copy(ca, rule.output)
        else:
            # Non-build-artifact (copy etc.): verify trace before re-executing.
            trace = traces.get(rule.output)
            if trace is not None and self._verify(rule, trace):
                return
            lock_impl = FileLock(rule.output, self.args).lock
            atomic_link(lock_impl, rule.output, flat_cmd)

        traces.put(rule.output, _make_trace_entry(rule, self.context))

    def _execute_build(self, target: str) -> None:
        # SlurmBackend is self-executing via execute(); this path is never used.
        raise NotImplementedError  # pragma: no cover
