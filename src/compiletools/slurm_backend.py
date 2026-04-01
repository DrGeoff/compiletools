"""Slurm build backend — distributes compile rules across a Slurm HPC cluster.

Like the shake backend, this is self-executing (no external build file).  It
reuses shake's verifying-trace and content-addressable short-circuit logic by
inheriting from ShakeBackend, but replaces the ThreadPoolExecutor compile phase
with batch Slurm job submission:

1. Identify compile rules that need rebuilding (trace verify).
2. Submit all of them to Slurm as job arrays (``sbatch --array``).
3. Poll ``sacct`` until every task reaches a terminal state.
4. Record traces for successfully compiled outputs.
5. Run link/library rules locally (they are few, fast, and serial-dependent).
6. Save traces.

Link rules also use shake's content-addressable short-circuit so unchanged
executables are never re-linked.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time

from compiletools.build_backend import register_backend
from compiletools.build_graph import BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.shake_backend import (
    ShakeBackend,
    TraceEntry,
    TraceStore,
    _atomic_copy,
    hash_command,
)


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
            help="Slurm partition (queue) for compile jobs. "
            "Omit to use the site default partition.",
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
                traces.put(
                    rule.output,
                    TraceEntry(
                        output_hash=get_file_hash(rule.output),
                        input_hashes={
                            p: get_file_hash(p) for p in rule.inputs if os.path.isfile(p)
                        },
                        command_hash=hash_command(rule.command),
                    ),
                )

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
                flat = [tok for arg in rule.command for tok in shlex.split(arg)]
                fc.write(shlex.join(flat) + "\n")
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
                "-j", array_job_id,
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
                    f"Timed out after {polls} sacct polls waiting for Slurm arrays: "
                    + ", ".join(sorted(pending))
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
        import shlex as _shlex
        import sys

        if rule.command is None:
            return  # No-op rules (e.g. copy with no command)

        if rule.rule_type in ("link", "static_library", "shared_library"):
            ca = self._ca_target(rule)  # type: ignore[attr-defined]
            if os.path.exists(ca):
                _atomic_copy(ca, rule.output)
                return
            # Ensure output directory exists
            out_dir = os.path.dirname(rule.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            flat_cmd: list[str] = []
            for arg in rule.command:
                flat_cmd.extend(_shlex.split(arg))
            # Build to CA target so the result is content-addressable
            ca_cmd = [ca if a == rule.output else a for a in flat_cmd]
            ca_dir = os.path.dirname(ca)
            if ca_dir:
                os.makedirs(ca_dir, exist_ok=True)
            result = subprocess.run(ca_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(result.stdout, end="", file=sys.stdout)
                print(result.stderr, end="", file=sys.stderr)
                raise subprocess.CalledProcessError(
                    result.returncode, rule.command, result.stdout, result.stderr
                )
            _atomic_copy(ca, rule.output)
        else:
            flat_cmd = []
            for arg in rule.command:
                flat_cmd.extend(_shlex.split(arg))
            result = subprocess.run(flat_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(result.stdout, end="", file=sys.stdout)
                print(result.stderr, end="", file=sys.stderr)
                raise subprocess.CalledProcessError(
                    result.returncode, rule.command, result.stdout, result.stderr
                )

        traces.put(
            rule.output,
            TraceEntry(
                output_hash=get_file_hash(rule.output),
                input_hashes={p: get_file_hash(p) for p in rule.inputs if os.path.isfile(p)},
                command_hash=hash_command(rule.command),
            ),
        )

    def _execute_build(self, target: str) -> None:
        # SlurmBackend is self-executing via execute(); this path is never used.
        raise NotImplementedError  # pragma: no cover
