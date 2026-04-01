"""Unit tests for the Slurm build backend (no compiler or Slurm required)."""

from __future__ import annotations

import contextlib
import os
import subprocess
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, call, patch

import pytest

import compiletools.trace_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class, is_backend_available
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.trace_backend import SlurmBackend, TraceEntry, TraceStore, hash_command
from compiletools.testhelper import TempDirContextNoChange, make_backend_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def SlurmBackendTestContext(graph, **arg_overrides):
    """Context manager yielding (backend, tmpdir) with a SlurmBackend wired to *graph*."""
    slurm_defaults = dict(
        slurm_partition=None,
        slurm_time="00:30:00",
        slurm_mem="2G",
        slurm_cpus=1,
        slurm_account=None,
        slurm_max_array=1000,
        slurm_poll_interval=0.0,  # no sleep in tests
    )
    slurm_defaults.update(arg_overrides)
    with TempDirContextNoChange() as tmpdir:
        args = make_backend_args(tmpdir, **slurm_defaults)
        os.makedirs(args.objdir, exist_ok=True)
        backend = SlurmBackend.__new__(SlurmBackend)
        backend.args = args
        backend._graph = graph
        backend.namer = MagicMock()
        yield backend, tmpdir


def make_compile_rule(output="obj/foo.o", src="src/foo.cpp"):
    return BuildRule(
        output=output,
        inputs=[src],
        command=["g++", "-c", src, "-o", output],
        rule_type="compile",
    )


def make_link_rule(output="bin/foo", inputs=("obj/foo.o",)):
    return BuildRule(
        output=output,
        inputs=list(inputs),
        command=["g++", "-o", output] + list(inputs),
        rule_type="link",
    )


def make_phony_rule(name="build", inputs=()):
    return BuildRule(output=name, inputs=list(inputs), command=None, rule_type="phony")


def _sacct_output(*rows):
    """Build a mock sacct --parsable2 output string."""
    return "\n".join(f"{jid}|{state}" for jid, state in rows) + "\n"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_as_slurm(self):
        assert get_backend_class("slurm") is SlurmBackend

    def test_name(self):
        assert SlurmBackend.name() == "slurm"

    def test_build_filename(self):
        assert SlurmBackend.build_filename() == ".ct-slurm-traces.json"

    def test_in_available_backends(self):
        assert "slurm" in available_backends()

    def test_separate_trace_file_from_shake(self):
        assert SlurmBackend.build_filename() != ".ct-traces.json"


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_when_sbatch_on_path(self):
        with patch("shutil.which", side_effect=lambda t: "/usr/bin/sbatch" if t == "sbatch" else None):
            assert is_backend_available("slurm") is True

    def test_unavailable_when_sbatch_missing(self):
        with patch("shutil.which", return_value=None):
            assert is_backend_available("slurm") is False


# ---------------------------------------------------------------------------
# Slurm job array submission
# ---------------------------------------------------------------------------


class TestSbatchSubmission:
    def test_compile_rule_submitted_to_slurm(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="12345\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        # One sbatch call (the array submission)
        mock_sbatch.assert_called_once()
        cmd = mock_sbatch.call_args[0][0]
        assert cmd[0] == "sbatch"
        assert "--parsable" in cmd
        assert "--array=0-0" in cmd

    def test_sbatch_wrap_reads_from_cmds_file(self, tmp_path):
        """The --wrap script reads the compile command from the commands file."""
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

            cmd = mock_sbatch.call_args[0][0]
            wrap_idx = cmd.index("--wrap")
            wrap_value = cmd[wrap_idx + 1]
            # The wrap runs sed to read the commands file
            assert "sed" in wrap_value
            assert "SLURM_ARRAY_TASK_ID" in wrap_value
            # The commands file written to objdir must contain the compile command
            # (chunk 0 → chunk_id=0 → filename suffix -0)
            cmds_file = os.path.join(backend.args.objdir, ".ct-slurm-cmds-0.txt")
            content = open(cmds_file).read()
            assert "g++" in content
            assert "foo.o" in content

    def test_partition_added_when_specified(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_partition="gpu") as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="7\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        assert "--partition" in cmd
        assert "gpu" in cmd

    def test_partition_omitted_when_none(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="7\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        assert "--partition" not in cmd

    def test_account_added_when_specified(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_account="myproject") as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="9\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        assert "--account" in cmd
        assert "myproject" in cmd

    def test_multiple_compile_rules_in_one_array(self, tmp_path):
        """Multiple compile rules are submitted as a single job array, not N individual jobs."""
        graph = BuildGraph()
        r1 = make_compile_rule(output=str(tmp_path / "a.o"), src="a.cpp")
        r2 = make_compile_rule(output=str(tmp_path / "b.o"), src="b.cpp")
        graph.add_rule(r1)
        graph.add_rule(r2)
        graph.add_rule(make_phony_rule("build", [r1.output, r2.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="55\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        # Both rules in one sbatch call with --array=0-1
        mock_sbatch.assert_called_once()
        cmd = mock_sbatch.call_args[0][0]
        assert "--array=0-1" in cmd

    def test_large_project_uses_multiple_arrays_with_unique_filenames(self, tmp_path):
        """Projects larger than slurm-max-array get split into multiple job arrays.

        Each chunk must write to uniquely-named command/output files so that
        concurrent chunks do not overwrite each other's task lists.
        """
        graph = BuildGraph()
        rules = []
        for i in range(5):
            r = make_compile_rule(output=str(tmp_path / f"rule{i}.o"), src=f"src{i}.cpp")
            graph.add_rule(r)
            rules.append(r)
        graph.add_rule(make_phony_rule("build", [r.output for r in rules]))

        sbatch_ids = iter(["10\n", "20\n", "30\n"])

        with SlurmBackendTestContext(graph, slurm_max_array=2) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", side_effect=sbatch_ids) as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

            # 5 rules / max_array=2 → ceil(5/2) = 3 sbatch calls
            assert mock_sbatch.call_count == 3

            # Each chunk must have used a distinct cmds/outs filename (inside context
            # so tmpdir still exists)
            cmds_files = [f for f in os.listdir(backend.args.objdir) if f.startswith(".ct-slurm-cmds-")]
            assert len(cmds_files) == 3, f"Expected 3 unique cmds files, got: {cmds_files}"


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestCAShortCircuit:
    def test_existing_output_with_valid_trace_skips_sbatch(self, tmp_path):
        """Compile rules whose output exists AND has a valid trace are skipped."""
        src = str(tmp_path / "foo.cpp")
        out = str(tmp_path / "foo.o")
        open(src, "w").write("int x;")
        open(out, "w").write("compiled")

        rule = make_compile_rule(output=out, src=src)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        from compiletools.global_hash_registry import get_file_hash

        # Write a valid trace so the file is trusted
        trace_path = str(tmp_path / ".ct-slurm-traces.json")
        store = TraceStore(trace_path)
        store.put(
            out,
            TraceEntry(
                output_hash=get_file_hash(out),
                input_hashes={src: get_file_hash(src)},
                command_hash=hash_command(rule.command or []),
            ),
        )
        store.save()

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            backend.args.objdir = str(tmp_path)
            with patch("subprocess.check_output") as mock_sbatch:
                backend.execute("build")

        mock_sbatch.assert_not_called()

    def test_existing_output_without_trace_is_resubmitted(self, tmp_path):
        """Compile rules whose output exists but has no trace are resubmitted.

        A file with no trace was produced by a crashed build (Phase 4 trace-recording
        never ran) and cannot be trusted even if it appears valid on disk.
        """
        out = str(tmp_path / "foo.o")
        graph = BuildGraph()
        rule = make_compile_rule(output=out)
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # File exists on disk but no trace was recorded for it
        open(out, "w").close()

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        # Must resubmit despite the file existing — no trace means untrusted
        mock_sbatch.assert_called_once()


# ---------------------------------------------------------------------------
# Trace-based short-circuit
# ---------------------------------------------------------------------------


class TestTraceVerification:
    def test_valid_trace_skips_sbatch(self, tmp_path):
        """Compile rules with a valid trace are skipped without submitting."""
        src = str(tmp_path / "foo.cpp")
        out = str(tmp_path / "foo.o")
        with open(src, "w") as f:
            f.write("int main(){}")
        with open(out, "w") as f:
            f.write("compiled")

        rule = make_compile_rule(output=out, src=src)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        from compiletools.global_hash_registry import get_file_hash

        trace_path = str(tmp_path / ".ct-slurm-traces.json")
        store = TraceStore(trace_path)
        store.put(
            out,
            TraceEntry(
                output_hash=get_file_hash(out),
                input_hashes={src: get_file_hash(src)},
                command_hash=hash_command(rule.command or []),
            ),
        )
        store.save()

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            # Point objdir to tmp_path so the trace file is found
            backend.args.objdir = str(tmp_path)
            with patch("subprocess.check_output") as mock_sbatch:
                backend.execute("build")

        mock_sbatch.assert_not_called()


# ---------------------------------------------------------------------------
# Failed job handling
# ---------------------------------------------------------------------------


class TestJobFailures:
    def test_failed_job_raises_runtime_error(self, tmp_path):
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch("subprocess.check_output", side_effect=[
                "99\n",                                     # sbatch returns array job ID
                _sacct_output(("99_0", "FAILED")),          # sacct for array task
            ]):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

    def test_cancelled_job_raises_runtime_error(self, tmp_path):
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch("subprocess.check_output", side_effect=[
                "88\n",
                _sacct_output(("88_0", "CANCELLED")),
            ]):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

    def test_failed_job_deletes_corrupt_output_file(self, tmp_path):
        """_wait_for_arrays removes the output file when a task fails (monitoring layer)."""
        out = str(tmp_path / "foo.o")
        open(out, "w").close()  # simulate corrupt artifact from prior crash

        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch("subprocess.check_output", side_effect=[
                "99\n",
                _sacct_output(("99_0", "FAILED")),
            ]):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

        # Monitoring layer must delete the corrupt file on failure
        assert not os.path.exists(out)

    def test_wait_for_arrays_times_out_when_sacct_stops_reporting(self, tmp_path):
        """_wait_for_arrays raises RuntimeError if tasks never reach a terminal state.

        Exercises the poll cap added to prevent infinite hangs when sacct loses
        track of jobs (e.g. accounting history purge or cluster issue).
        """
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))

        b = SlurmBackend.__new__(SlurmBackend)
        # poll_interval=3600 → max_polls = max(1, int(3600/3600)) = 1: times out after 1 poll
        b.args = SimpleNamespace(slurm_poll_interval=3600.0)
        index_map = {"77": [rule]}

        with (
            patch("subprocess.check_output", return_value=_sacct_output(("77_0", "RUNNING"))),
            patch("time.sleep"),  # don't actually sleep
        ):
            with pytest.raises(RuntimeError, match="Timed out"):
                b._wait_for_arrays(index_map)


# ---------------------------------------------------------------------------
# _query_array_task_states parsing
# ---------------------------------------------------------------------------


class TestQueryStates:
    def _make_backend(self, tmp_path):
        args = SimpleNamespace(
            objdir=str(tmp_path),
            slurm_partition=None,
            slurm_time="00:30:00",
            slurm_mem="2G",
            slurm_cpus=1,
            slurm_account=None,
            slurm_max_array=1000,
            slurm_poll_interval=0.0,
        )
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = args
        return b

    def test_parses_completed(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = _sacct_output(("123_0", "COMPLETED"), ("123_1", "RUNNING"))
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_array_task_states("123")
        assert states["123_0"] == "COMPLETED"
        assert states["123_1"] == "RUNNING"

    def test_skips_batch_substeps(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = "123|RUNNING\n123_0|COMPLETED\n123_0.batch|COMPLETED\n123_0.extern|COMPLETED\n"
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_array_task_states("123")
        # Only top-level entries (no dots); includes the overall job and array tasks
        assert all("." not in k for k in states)
        assert states["123_0"] == "COMPLETED"

    def test_strips_state_reason(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = "77_0|CANCELLED by 1001\n"
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_array_task_states("77")
        assert states["77_0"] == "CANCELLED"

    def test_handles_empty_output(self, tmp_path):
        b = self._make_backend(tmp_path)
        with patch("subprocess.check_output", return_value=""):
            states = b._query_array_task_states("999")
        assert states == {}


# ---------------------------------------------------------------------------
# Local link execution
# ---------------------------------------------------------------------------


class TestLocalLink:
    def test_link_rule_runs_locally_not_via_sbatch(self, tmp_path):
        src = str(tmp_path / "foo.cpp")
        obj = str(tmp_path / "foo.o")
        exe = str(tmp_path / "foo")
        open(src, "w").write("int main(){}")
        open(obj, "w").write("compiled")  # pretend compile happened

        compile_rule = make_compile_rule(output=obj, src=src)
        link_rule = make_link_rule(output=exe, inputs=[obj])
        graph = BuildGraph()
        graph.add_rule(compile_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [exe]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            # Record a valid trace for the compile output so it is trusted and skipped.
            from compiletools.global_hash_registry import get_file_hash

            store = TraceStore(os.path.join(backend.args.objdir, ".ct-slurm-traces.json"))
            store.put(
                obj,
                TraceEntry(
                    output_hash=get_file_hash(obj),
                    input_hashes={src: get_file_hash(src)},
                    command_hash=hash_command(compile_rule.command or []),
                ),
            )
            store.save()

            with patch("subprocess.run") as mock_run:
                result = subprocess.CompletedProcess([], 0, "", "")

                def fake_run(cmd, **kw):
                    try:
                        idx = cmd.index("-o")
                        open(cmd[idx + 1], "w").close()
                    except (ValueError, IndexError):
                        pass
                    return result

                mock_run.side_effect = fake_run
                backend.execute("build")

        # sbatch not called (compile output had a valid trace)
        mock_run.assert_called()
        # link ran locally via subprocess.run
        mock_run.assert_called()

    def test_runtests_delegates_to_run_tests(self):
        graph = BuildGraph()
        graph.add_rule(make_phony_rule("build"))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch.object(backend, "_run_tests") as mock_run_tests:
                backend.execute("runtests")

        mock_run_tests.assert_called_once()

    def test_generate_before_execute_required(self):
        args = SimpleNamespace(
            objdir="/tmp",
            slurm_partition=None,
            slurm_time="00:30:00",
            slurm_mem="2G",
            slurm_cpus=1,
            slurm_account=None,
            slurm_max_array=1000,
            slurm_poll_interval=0.0,
        )
        backend = SlurmBackend.__new__(SlurmBackend)
        backend.args = args
        backend._graph = None

        with pytest.raises(RuntimeError, match="generate\\(\\) must be called"):
            backend.execute("build")


# ---------------------------------------------------------------------------
# Memory estimation and tiered submission
# ---------------------------------------------------------------------------


def _make_rule_with_weight(output, src, include_weight):
    """Create a compile rule with a given include_weight."""
    return BuildRule(
        output=output,
        inputs=[src],
        command=["g++", "-c", src, "-o", output],
        rule_type="compile",
        include_weight=include_weight,
    )


class TestMemoryEstimation:
    """Test _estimate_memory tier boundaries.

    Tiers are based on include_weight (len(FileAnalyzer.quoted_headers)):
      <= 1 -> 512M,  <= 2 -> 1G,  > 2 -> 4G
    """

    def test_zero_weight_gets_512M(self):
        rule = make_compile_rule()  # include_weight=0
        assert SlurmBackend._estimate_memory(rule) == "512M"

    def test_weight_1_gets_512M(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=1)
        assert SlurmBackend._estimate_memory(rule) == "512M"

    def test_weight_2_gets_1G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=2)
        assert SlurmBackend._estimate_memory(rule) == "1G"

    def test_weight_3_gets_4G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=3)
        assert SlurmBackend._estimate_memory(rule) == "4G"

    def test_weight_10_gets_4G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=10)
        assert SlurmBackend._estimate_memory(rule) == "4G"


class TestTieredSubmission:
    """Test that rules with different include_weight produce separate sbatch calls."""

    def test_mixed_tiers_produce_separate_sbatch_calls(self, tmp_path):
        """Rules with different memory needs get separate sbatch calls."""
        graph = BuildGraph()
        # Small file: weight=0 -> 512M
        r_small = _make_rule_with_weight(str(tmp_path / "small.o"), "small.C", include_weight=0)
        # Large file: weight=5 -> 4G
        r_large = _make_rule_with_weight(str(tmp_path / "large.o"), "large.C", include_weight=5)
        graph.add_rule(r_small)
        graph.add_rule(r_large)
        graph.add_rule(make_phony_rule("build", [r_small.output, r_large.output]))

        sbatch_ids = iter(["100\n", "200\n"])

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", side_effect=sbatch_ids) as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        # Two separate sbatch calls (one per tier)
        assert mock_sbatch.call_count == 2
        mems = []
        for c in mock_sbatch.call_args_list:
            cmd = c[0][0]
            for arg in cmd:
                if arg.startswith("--mem="):
                    mems.append(arg)
        assert "--mem=512M" in mems
        assert "--mem=4G" in mems

    def test_same_tier_uses_single_sbatch_call(self, tmp_path):
        """Rules in the same memory tier are batched into one array."""
        graph = BuildGraph()
        # Both weight <= 1 -> same tier (512M)
        r1 = _make_rule_with_weight(str(tmp_path / "a.o"), "a.C", include_weight=0)
        r2 = _make_rule_with_weight(str(tmp_path / "b.o"), "b.C", include_weight=1)
        graph.add_rule(r1)
        graph.add_rule(r2)
        graph.add_rule(make_phony_rule("build", [r1.output, r2.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="55\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays"),
            ):
                backend.execute("build")

        # Both in same tier (512M) -> one sbatch call
        mock_sbatch.assert_called_once()
        cmd = mock_sbatch.call_args[0][0]
        assert "--array=0-1" in cmd
        assert "--mem=512M" in cmd

    def test_sbatch_array_mem_parameter_overrides_default(self, tmp_path):
        """The mem parameter to _sbatch_array overrides self.args.slurm_mem."""
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_mem="16G") as (backend, tmpdir):
            with patch("subprocess.check_output", return_value="42\n") as mock_sbatch:
                backend._sbatch_array([rule], chunk_id=0, mem="1G")

        cmd = mock_sbatch.call_args[0][0]
        assert "--mem=1G" in cmd
        assert "--mem=16G" not in cmd

    def test_sbatch_array_without_mem_uses_default(self, tmp_path):
        """Without mem parameter, _sbatch_array uses self.args.slurm_mem."""
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_mem="16G") as (backend, tmpdir):
            with patch("subprocess.check_output", return_value="42\n") as mock_sbatch:
                backend._sbatch_array([rule], chunk_id=0)

        cmd = mock_sbatch.call_args[0][0]
        assert "--mem=16G" in cmd
