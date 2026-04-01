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

ShakeBackend drives compilation directly from Python using a ThreadPoolExecutor.

SlurmBackend replaces the ThreadPoolExecutor compile phase with batch Slurm job
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
import time
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
    except Exception:
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
    return TraceEntry(
        output_hash=output_hash if output_hash is not None else get_file_hash(rule.output),
        input_hashes={p: get_file_hash(p) for p in rule.inputs if os.path.isfile(p)},
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
                    sequential_results.append(self._build(inp, graph, traces, done, lock, executor, max_workers))
                else:
                    futures.append(executor.submit(self._build, inp, graph, traces, done, lock, executor, max_workers))
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
                executor.submit(self._build, inp, graph, traces, done, lock, executor, max_workers)
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

        assert rule.command is not None, "only rules with commands reach EXECUTE"
        cmd = rule.command  # bind locally so Pyright narrows to list[str]
        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 1:
            print(" ".join(cmd), file=sys.stderr)

        flat_cmd = _flatten_command(cmd)

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
                    atomic_compile(lock_impl, target, cmd_without_output)
                except subprocess.CalledProcessError as e:
                    print(e.stdout or "", end="", file=sys.stdout)
                    print(e.stderr or "", end="", file=sys.stderr)
                    raise
            else:
                # File locking disabled — still use temp+rename for atomicity
                # but without cross-process locking
                self._atomic_compile_no_lock(target, cmd_without_output)
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
                _run_subprocess(ca_cmd, cmd)
            _atomic_copy(ca, target)
        else:
            # Non-CA rules (shouldn't normally reach here)
            with FileLock(target, self.args):
                _run_subprocess(flat_cmd, cmd)

        with lock:
            done.add(target)

        # CA outputs don't need trace recording or early cutoff —
        # existence implies correctness.
        if _is_build_artifact(rule):
            return True  # New output → dependents must rebuild

        new_hash = get_file_hash(target)

        # RECORD TRACE (only for non-content-addressable rules)
        with lock:
            traces.put(target, _make_trace_entry(rule, output_hash=new_hash))

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
            default="2G",
            help="Memory limit per compile job (e.g. 2G, 512M). Default: 2G",
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
    # Core execute() — overrides ShakeBackend's ThreadPoolExecutor engine

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

        # Phase 2: submit compile rules as job arrays (one sbatch call per array chunk).
        # Chunking is controlled by --slurm-max-array; each chunk becomes one job array
        # so the scheduler sees all N tasks at once and backfills them efficiently.
        # Each chunk gets a unique chunk_id so the command/output files don't collide
        # when multiple chunks are submitted before the first chunk's tasks start reading.
        max_array = self.args.slurm_max_array
        # index_map[array_job_id] = list of rules corresponding to task indices 0, 1, …
        index_map: dict[str, list[BuildRule]] = {}
        for chunk_start in range(0, len(to_submit), max_array):
            chunk = to_submit[chunk_start : chunk_start + max_array]
            array_job_id = self._sbatch_array(chunk, chunk_id=chunk_start)
            index_map[array_job_id] = chunk

        # Phase 3: wait for all arrays to finish
        if index_map:
            self._wait_for_arrays(index_map)

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
    # Slurm helpers

    def _sbatch_array(self, rules: list[BuildRule], chunk_id: int = 0) -> str:
        """Submit *rules* as a single Slurm job array; return the array job ID.

        Each array task (index 0 … N-1) reads its compile command from a
        commands file written to the objdir and executes it.

        *chunk_id* is used to give each chunk a unique commands/outputs filename so
        multiple chunks submitted before the first chunk's tasks start reading do not
        overwrite each other's files.
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
        wrap = (
            f'CMD=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(cmds_file)}); '
            f'OUT=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" {shlex.quote(outs_file)}); '
            f'bash -c "$CMD" || {{ rm -f "$OUT"; exit 1; }}'
        )

        cmd = [
            "sbatch",
            "--parsable",
            "--export=ALL",
            f"--array=0-{n - 1}",
            "--job-name=ct-compile",
            f"--time={self.args.slurm_time}",
            f"--mem={self.args.slurm_mem}",
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

    def _wait_for_arrays(self, index_map: dict[str, list[BuildRule]]) -> None:
        """Poll sacct until every task in every array reaches a terminal state.

        *index_map* maps array_job_id → ordered list of rules (index == task index).
        Raises RuntimeError listing every failed task after all arrays finish, or if
        sacct stops reporting tasks before all are terminal (e.g. accounting purge).
        """
        poll_interval = self.args.slurm_poll_interval
        # Cap polling to avoid hanging forever if sacct loses track of jobs.
        # At the default 2s interval this gives ~2 hours; callers with longer
        # wall-time limits should increase --slurm-poll-interval accordingly.
        max_polls = max(1, int(3600 / max(poll_interval, 0.1)))
        polls = 0

        pending: set[str] = set(index_map)
        failed: list[str] = []

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

                # terminal_tasks: only array tasks (contain "_"), not the array root entry
                terminal_tasks = {
                    jid: st
                    for jid, st in states.items()
                    if st in self._TERMINAL_STATES and "_" in jid  # skip array root
                }
                if len(terminal_tasks) < len(rules):
                    still_pending.add(array_job_id)
                    continue

                # All tasks terminal — check for failures
                for jid, st in terminal_tasks.items():
                    if st != self._SUCCESS_STATE:
                        # jid is "<array_job_id>_<index>"; strip the known prefix
                        # to get the index robustly even if array_job_id has underscores.
                        try:
                            idx = int(jid[len(array_job_id) + 1 :])
                            rule_output = rules[idx].output
                            if os.path.exists(rule_output):
                                os.remove(rule_output)  # remove corrupt/partial artifact
                        except (ValueError, IndexError, OSError):
                            rule_output = "?"
                        failed.append(f"Job {jid} ({rule_output}): {st}")

            pending = still_pending
            if pending:
                time.sleep(poll_interval)

        if failed:
            raise RuntimeError("Slurm compile jobs failed:\n" + "\n".join(failed))

    # ------------------------------------------------------------------
    # Local execution for link/library rules

    def _run_local(self, rule: BuildRule, traces: TraceStore) -> None:
        """Run a link/library/copy rule locally, with CA short-circuit."""
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
            _run_subprocess(ca_cmd, rule.command)
            _atomic_copy(ca, rule.output)
        else:
            _run_subprocess(flat_cmd, rule.command)

        traces.put(rule.output, _make_trace_entry(rule))

    def _execute_build(self, target: str) -> None:
        # SlurmBackend is self-executing via execute(); this path is never used.
        raise NotImplementedError  # pragma: no cover
