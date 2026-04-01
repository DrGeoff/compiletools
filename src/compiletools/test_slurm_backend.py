"""Unit tests for the Slurm build backend (no compiler or Slurm required)."""

from __future__ import annotations

import contextlib
import os
import subprocess
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, call, patch

import pytest

import compiletools.slurm_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class, is_backend_available
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.shake_backend import TraceEntry, TraceStore, hash_command
from compiletools.slurm_backend import SlurmBackend
from compiletools.testhelper import TempDirContextNoChange, make_backend_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def SlurmBackendTestContext(graph, **arg_overrides):
    """Context manager yielding (backend, tmpdir) with a SlurmBackend wired to *graph*."""
    slurm_defaults = dict(
        slurm_partition=None,
        slurm_time="00:10:00",
        slurm_mem="2G",
        slurm_cpus=1,
        slurm_account=None,
        slurm_max_jobs=500,
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
# Slurm job submission
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
                patch.object(backend, "_wait_for_jobs"),
            ):
                backend.execute("build")

        # sbatch must have been called exactly once for the compile rule
        mock_sbatch.assert_called_once()
        cmd = mock_sbatch.call_args[0][0]
        assert cmd[0] == "sbatch"
        assert "--parsable" in cmd

    def test_sbatch_wrap_contains_compile_command(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_jobs"),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        wrap_idx = cmd.index("--wrap")
        wrap_value = cmd[wrap_idx + 1]
        assert "g++" in wrap_value
        assert "foo.o" in wrap_value

    def test_partition_added_when_specified(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_partition="gpu") as (backend, tmpdir):
            with (
                patch("subprocess.check_output", return_value="7\n") as mock_sbatch,
                patch.object(backend, "_wait_for_jobs"),
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
                patch.object(backend, "_wait_for_jobs"),
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
                patch.object(backend, "_wait_for_jobs"),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        assert "--account" in cmd
        assert "myproject" in cmd

    def test_multiple_compile_rules_all_submitted(self, tmp_path):
        graph = BuildGraph()
        r1 = make_compile_rule(output=str(tmp_path / "a.o"), src="a.cpp")
        r2 = make_compile_rule(output=str(tmp_path / "b.o"), src="b.cpp")
        graph.add_rule(r1)
        graph.add_rule(r2)
        graph.add_rule(make_phony_rule("build", [r1.output, r2.output]))

        call_count = 0

        def fake_check_output(cmd, **kw):
            nonlocal call_count
            call_count += 1
            return f"{call_count}\n"

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with (
                patch("subprocess.check_output", side_effect=fake_check_output),
                patch.object(backend, "_wait_for_jobs"),
            ):
                backend.execute("build")

        assert call_count == 2


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestCAShortCircuit:
    def test_existing_output_skips_sbatch(self, tmp_path):
        """Compile rules whose output already exists are skipped (CA short-circuit)."""
        out = str(tmp_path / "foo.o")
        graph = BuildGraph()
        rule = make_compile_rule(output=out)
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # Create the output file to simulate a prior build
        open(out, "w").close()

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch("subprocess.check_output") as mock_sbatch:
                backend.execute("build")

        mock_sbatch.assert_not_called()


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

        def fake_sacct(cmd, **kw):
            # Return FAILED state for job 99
            return _sacct_output(("99", "FAILED"))

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            with patch("subprocess.check_output", side_effect=[
                "99\n",              # sbatch returns job ID
                fake_sacct(None),    # sacct query
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
                _sacct_output(("88", "CANCELLED")),
            ]):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")


# ---------------------------------------------------------------------------
# _query_states parsing
# ---------------------------------------------------------------------------


class TestQueryStates:
    def _make_backend(self, tmp_path):
        args = SimpleNamespace(
            objdir=str(tmp_path),
            slurm_partition=None,
            slurm_time="00:10:00",
            slurm_mem="2G",
            slurm_cpus=1,
            slurm_account=None,
            slurm_max_jobs=500,
            slurm_poll_interval=0.0,
        )
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = args
        return b

    def test_parses_completed(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = _sacct_output(("123", "COMPLETED"), ("456", "RUNNING"))
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_states({"123", "456"})
        assert states["123"] == "COMPLETED"
        assert states["456"] == "RUNNING"

    def test_skips_batch_substeps(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = "123|COMPLETED\n123.batch|COMPLETED\n123.extern|COMPLETED\n"
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_states({"123"})
        assert list(states.keys()) == ["123"]

    def test_strips_state_reason(self, tmp_path):
        b = self._make_backend(tmp_path)
        sacct_out = "77|CANCELLED by 1001\n"
        with patch("subprocess.check_output", return_value=sacct_out):
            states = b._query_states({"77"})
        assert states["77"] == "CANCELLED"

    def test_handles_empty_output(self, tmp_path):
        b = self._make_backend(tmp_path)
        with patch("subprocess.check_output", return_value=""):
            states = b._query_states({"999"})
        assert states == {}


# ---------------------------------------------------------------------------
# Local link execution
# ---------------------------------------------------------------------------


class TestLocalLink:
    def test_link_rule_runs_locally_not_via_sbatch(self, tmp_path):
        obj = str(tmp_path / "foo.o")
        exe = str(tmp_path / "foo")
        open(obj, "w").close()  # pretend compile happened

        compile_rule = make_compile_rule(output=obj)
        link_rule = make_link_rule(output=exe, inputs=[obj])
        graph = BuildGraph()
        graph.add_rule(compile_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [exe]))

        sbatch_calls = []
        local_calls = []

        def fake_check_output(cmd, **kw):
            sbatch_calls.append(cmd)
            return "5\n"

        def fake_check_call(cmd, **kw):
            # Create the output file to simulate a successful link
            for arg in cmd:
                if arg == exe:
                    # The link cmd is redirected to a CA target, find it
                    pass
            local_calls.append(cmd)
            # Create exe so traces can hash it — find -o TARGET in cmd
            try:
                idx = cmd.index("-o")
                out_file = cmd[idx + 1]
                open(out_file, "w").close()
            except (ValueError, IndexError):
                pass

        with SlurmBackendTestContext(graph) as (backend, tmpdir):
            # compile output already exists → no sbatch
            with (
                patch("subprocess.check_output", side_effect=fake_check_output),
                patch("subprocess.run") as mock_run,
            ):
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

        # sbatch not called (compile output existed)
        assert not sbatch_calls
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
            slurm_time="00:10:00",
            slurm_mem="2G",
            slurm_cpus=1,
            slurm_account=None,
            slurm_max_jobs=500,
            slurm_poll_interval=0.0,
        )
        backend = SlurmBackend.__new__(SlurmBackend)
        backend.args = args
        backend._graph = None

        with pytest.raises(RuntimeError, match="generate\\(\\) must be called"):
            backend.execute("build")
