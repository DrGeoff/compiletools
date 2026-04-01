"""Slurm build backend — distributes compile rules across a Slurm HPC cluster.

Like the shake backend, this is self-executing (no external build file).  It
reuses shake's verifying-trace and content-addressable short-circuit logic by
inheriting from ShakeBackend, but replaces the ThreadPoolExecutor compile phase
with batch Slurm job submission:

1. Identify compile rules that need rebuilding (CA short-circuit + trace verify).
2. Submit all of them to Slurm via ``sbatch --parsable --wrap=...`` in one pass.
3. Poll ``sacct`` until every job reaches a terminal state.
4. Run link/library rules locally (they are few, fast, and serial-dependent).
5. Save traces.

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
            default="00:10:00",
            help="Wall-clock time limit per compile job (HH:MM:SS). Default: 00:10:00",
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
            "--slurm-max-jobs",
            default=500,
            type=int,
            help="Maximum number of Slurm jobs to have in flight at once. Default: 500",
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

        # Phase 1: identify compile rules that need rebuilding
        #   - CA short-circuit: object filename encodes all inputs → existence = correct
        #   - trace verify: inherited _verify() from ShakeBackend
        to_submit = [
            rule
            for rule in graph.rules_by_type("compile")
            if not os.path.exists(rule.output)
            and not (traces.get(rule.output) and self._verify(rule, traces.get(rule.output)))  # type: ignore[attr-defined]
        ]

        # Phase 2: batch-submit all compile rules to Slurm
        job_map: dict[str, BuildRule] = {}  # job_id → rule
        max_jobs = getattr(self.args, "slurm_max_jobs", 500)
        for i, rule in enumerate(to_submit):
            job_id = self._sbatch(rule)
            job_map[job_id] = rule
            # Respect max_jobs by draining when we hit the limit
            if len(job_map) >= max_jobs and i < len(to_submit) - 1:
                self._drain_jobs(job_map, target_count=max_jobs // 2)

        # Phase 3: wait for all remaining compile jobs
        if job_map:
            self._wait_for_jobs(job_map)

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

    def _sbatch(self, rule: BuildRule) -> str:
        """Submit one compile rule to Slurm; return the job ID string."""
        cmd = [
            "sbatch",
            "--parsable",
            "--job-name=ct-compile",
            f"--time={self.args.slurm_time}",
            f"--mem={self.args.slurm_mem}",
            f"--cpus-per-task={self.args.slurm_cpus}",
        ]
        if self.args.slurm_partition:
            cmd += ["--partition", self.args.slurm_partition]
        if self.args.slurm_account:
            cmd += ["--account", self.args.slurm_account]
        assert rule.command is not None, "compile rules always have a command"
        cmd += ["--wrap", shlex.join(rule.command)]
        return subprocess.check_output(cmd, text=True).strip()

    def _query_states(self, job_ids: set[str]) -> dict[str, str]:
        """Return ``{job_id: state}`` for *job_ids* via ``sacct``."""
        out = subprocess.check_output(
            [
                "sacct",
                "-j",
                ",".join(job_ids),
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
            # Skip sub-steps (.batch, .extern) — only track the top-level job
            if "." in jid:
                continue
            # State may have a trailing reason in parens, e.g. "FAILED (exit code 1)"
            result[jid] = parts[1].split()[0]
        return result

    _TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"})
    _SUCCESS_STATE = "COMPLETED"

    def _wait_for_jobs(self, job_map: dict[str, BuildRule]) -> None:
        """Poll ``sacct`` until all jobs in *job_map* reach a terminal state.

        Raises ``RuntimeError`` listing every failed job after all jobs finish.
        """
        self._drain_jobs(job_map, target_count=0)

    def _drain_jobs(self, job_map: dict[str, BuildRule], target_count: int) -> None:
        """Poll until ``len(job_map) <= target_count``, removing finished jobs."""
        failed: list[str] = []
        poll_interval = getattr(self.args, "slurm_poll_interval", 2.0)

        while len(job_map) > target_count:
            states = self._query_states(set(job_map))
            done = {jid for jid, st in states.items() if st in self._TERMINAL_STATES}
            for jid in done:
                if states[jid] != self._SUCCESS_STATE:
                    failed.append(
                        f"Job {jid} ({job_map[jid].output}): {states[jid]}"
                    )
                del job_map[jid]
            if len(job_map) > target_count:
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
