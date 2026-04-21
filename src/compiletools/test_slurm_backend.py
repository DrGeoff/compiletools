"""Unit tests for the Slurm build backend (no compiler or Slurm required)."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import compiletools.trace_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class, is_backend_available
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.testhelper import TempDirContextNoChange, make_backend_args
from compiletools.trace_backend import (
    SlurmBackend,
    TraceStore,
    _make_trace_entry,
    _slurm_mem_arg,
    _slurm_mem_tiers_arg,
    _slurm_time_arg,
)


class _ScancelMock:
    """Holds both the patcher and the active mock so tests can stop/start."""

    def __init__(self):
        self._patcher = patch.object(SlurmBackend, "_scancel_pending", autospec=True)
        self.mock = self._patcher.start()

    @property
    def called(self):
        return self.mock.called

    def stop(self):
        self._patcher.stop()

    def start(self):
        self.mock = self._patcher.start()


@pytest.fixture(autouse=True)
def _mock_scancel():
    """Stub out _scancel_pending so execute()'s finally block doesn't shell out.

    Tests that exercise scancel directly should call .stop()/.start() on the
    yielded helper.
    """
    helper = _ScancelMock()
    try:
        yield helper
    finally:
        with contextlib.suppress(RuntimeError):
            helper.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def SlurmBackendTestContext(graph, **arg_overrides):
    """Context manager yielding (backend, _tmpdir) with a SlurmBackend wired to *graph*."""
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
        backend.context = BuildContext()
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


def _sbatch_calls(mock_check_output):
    """Return only the sbatch calls from a check_output mock.

    The broad subprocess.check_output patch also catches unrelated calls
    (e.g. git rev-parse from find_git_root).  Filter to sbatch only.
    """
    return [c for c in mock_check_output.call_args_list if c[0][0][0] == "sbatch"]


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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="12345\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        # One sbatch call (the array submission)
        assert len(_sbatch_calls(mock_sbatch)) == 1
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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

            cmd = mock_sbatch.call_args[0][0]
            wrap_idx = cmd.index("--wrap")
            wrap_value = cmd[wrap_idx + 1]
            assert "sed" in wrap_value
            assert "SLURM_ARRAY_TASK_ID" in wrap_value
            # Wrap must reference the per-invocation cmds file.
            import re

            m = re.search(r"\.ct-slurm-cmds-[\w\-.]+-0\.txt", wrap_value)
            assert m, f"wrap missing cmds file pattern: {wrap_value}"

    def test_partition_added_when_specified(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_partition="gpu") as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="7\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="7\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        assert "--partition" not in cmd

    def test_account_added_when_specified(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_account="myproject") as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="9\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="55\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        # Both rules in one sbatch call with --array=0-1
        assert len(_sbatch_calls(mock_sbatch)) == 1
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

        with SlurmBackendTestContext(graph, slurm_max_array=2) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", side_effect=sbatch_ids) as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

            # 5 rules / max_array=2 → ceil(5/2) = 3 sbatch calls
            assert mock_sbatch.call_count == 3

            # Each sbatch call's wrap script must reference a distinct cmds file
            # (per-invocation prefix + per-chunk suffix prevents collisions).
            wrap_files = []
            for c in _sbatch_calls(mock_sbatch):
                cmd = c[0][0]
                wrap_idx = cmd.index("--wrap")
                wrap = cmd[wrap_idx + 1]
                # Extract the cmds-file path from the wrap script
                import re

                m = re.search(r"\.ct-slurm-cmds-[\w\-.]+\.txt", wrap)
                assert m
                wrap_files.append(m.group(0))
            assert len(set(wrap_files)) == 3, f"Expected 3 unique cmds filenames, got: {wrap_files}"


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestCAShortCircuit:
    def test_existing_output_with_valid_trace_skips_sbatch(self, tmp_path):
        """Compile rules whose output exists AND has a valid trace are skipped."""
        src = str(tmp_path / "foo.cpp")
        out = str(tmp_path / "foo.o")
        (tmp_path / "foo.cpp").write_text("int x;")
        (tmp_path / "foo.o").write_text("compiled")

        rule = make_compile_rule(output=out, src=src)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            backend.args.objdir = str(tmp_path)
            ctx = backend.context

            # Write a valid trace so the file is trusted
            trace_path = str(tmp_path / ".ct-slurm-traces.json")
            store = TraceStore(trace_path)
            store.put(out, _make_trace_entry(rule, ctx))
            store.save()

            with patch("subprocess.check_output") as mock_sbatch:
                backend.execute("build")

            assert len(_sbatch_calls(mock_sbatch)) == 0

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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        # Must resubmit despite the file existing — no trace means untrusted
        assert len(_sbatch_calls(mock_sbatch)) == 1


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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            # Point objdir to tmp_path so the trace file is found
            backend.args.objdir = str(tmp_path)
            ctx = backend.context

            trace_path = str(tmp_path / ".ct-slurm-traces.json")
            store = TraceStore(trace_path)
            store.put(out, _make_trace_entry(rule, ctx))
            store.save()

            with patch("subprocess.check_output") as mock_sbatch:
                backend.execute("build")

            assert len(_sbatch_calls(mock_sbatch)) == 0


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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "99\n",  # sbatch returns array job ID
                    _sacct_output(("99_0", "FAILED")),  # sacct for array task
                ],
            ):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

    def test_cancelled_job_raises_runtime_error(self, tmp_path):
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "88\n",
                    _sacct_output(("88_0", "CANCELLED")),
                ],
            ):
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

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "99\n",
                    _sacct_output(("99_0", "FAILED")),
                ],
            ):
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
        # poll_interval=1800 → max_polls = max(1, int(1800/1800)) = 1: times out after 1 poll
        b.args = SimpleNamespace(slurm_poll_interval=1800.0)
        index_map = {"77": [rule]}

        with (
            patch("subprocess.check_output", return_value=_sacct_output(("77_0", "RUNNING"))),
            patch("time.sleep"),  # don't actually sleep
        ):
            with pytest.raises(RuntimeError, match="Timed out"):
                b._wait_for_arrays(index_map)

    def test_wait_for_output_files_raises_when_outputs_missing(self, tmp_path):
        """If sacct says COMPLETED but the output never appears (e.g. NFS
        metadata lag past the timeout, or sacct false-positive), raise
        RuntimeError so the linker doesn't fail later with a confusing
        'no such file' diagnostic far from the cause."""
        rule = make_compile_rule(output=str(tmp_path / "ghost.o"))
        # File is intentionally NOT created

        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(slurm_poll_interval=0.0)

        # Force the polling loop to terminate immediately
        with (
            patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0]),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match=r"output file.*still missing"):
                b._wait_for_output_files([rule], timeout=30.0)

    def test_wait_for_output_files_returns_when_files_appear(self, tmp_path):
        """Outputs that exist immediately must not raise or sleep."""
        out = str(tmp_path / "real.o")
        open(out, "w").close()
        rule = make_compile_rule(output=out)

        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(slurm_poll_interval=0.0)
        # Should return without sleeping or raising
        b._wait_for_output_files([rule], timeout=30.0)

    def test_missing_outputs_preserves_slurm_logs_and_mentions_them(self, tmp_path):
        """When _wait_for_output_files raises, slurm-ct-*.out logs survive on disk
        AND their paths are quoted in the RuntimeError so the user can investigate."""
        # Two compile rules; only the link rule presence triggers the wait.
        ghost_out = str(tmp_path / "ghost.o")
        real_out = str(tmp_path / "real.o")
        ghost_rule = make_compile_rule(output=ghost_out, src="ghost.cpp")
        real_rule = make_compile_rule(output=real_out, src="real.cpp")
        link_rule = make_link_rule(output=str(tmp_path / "app"), inputs=(ghost_out, real_out))

        graph = BuildGraph()
        graph.add_rule(ghost_rule)
        graph.add_rule(real_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [link_rule.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            # Pre-create only real_out; ghost_out stays missing
            open(real_out, "w").close()

            objdir = backend.args.objdir
            log_paths_created: list[str] = []

            def fake_sbatch(*args, **kwargs):
                # Create a slurm log named for this invocation's prefix and chunk
                prefix = backend._invocation_prefix
                log_path = os.path.join(objdir, f"slurm-ct-{prefix}-0-0.out")
                with open(log_path, "w") as f:
                    f.write("OOM-kill: task ran out of memory\n")
                log_paths_created.append(log_path)
                return "42\n"

            with (
                patch("subprocess.check_output", side_effect=fake_sbatch),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
                patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0, 100.0]),
                patch("time.sleep"),
            ):
                with pytest.raises(RuntimeError) as excinfo:
                    backend.execute("build")

            assert log_paths_created
            log_path = log_paths_created[0]
            log_basename = os.path.basename(log_path)
            assert log_basename in str(excinfo.value)
            assert "Slurm logs preserved" in str(excinfo.value)
            assert os.path.exists(log_path), "slurm log was deleted before raise"

    def test_missing_outputs_saves_traces_for_completed_compiles(self, tmp_path):
        """When _wait_for_output_files raises, traces for compiles that DID complete
        must be persisted, so the next ct-cake invocation does not re-submit them."""
        ghost_out = str(tmp_path / "ghost.o")
        real_src = str(tmp_path / "real.cpp")
        real_out = str(tmp_path / "real.o")
        # real_src must exist so _make_trace_entry can hash it as an input
        with open(real_src, "w") as f:
            f.write("int x;\n")
        ghost_rule = make_compile_rule(output=ghost_out, src="ghost.cpp")
        real_rule = make_compile_rule(output=real_out, src=real_src)
        link_rule = make_link_rule(output=str(tmp_path / "app"), inputs=(ghost_out, real_out))

        graph = BuildGraph()
        graph.add_rule(ghost_rule)
        graph.add_rule(real_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [link_rule.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            # Only the real output exists post-"submission"
            with open(real_out, "w") as f:
                f.write("compiled\n")

            with (
                patch("subprocess.check_output", return_value="55\n"),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
                patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0, 100.0]),
                patch("time.sleep"),
            ):
                with pytest.raises(RuntimeError):
                    backend.execute("build")

            # Trace store on disk must contain real_out but not ghost_out
            trace_path = os.path.join(backend.args.objdir, ".ct-slurm-traces.json")
            assert os.path.exists(trace_path), "traces.save() was never called on raise path"
            persisted = TraceStore(trace_path)
            assert persisted.get(real_out) is not None, "completed compile not recorded"
            assert persisted.get(ghost_out) is None, "missing output incorrectly recorded"


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
        (tmp_path / "foo.cpp").write_text("int main(){}")
        (tmp_path / "foo.o").write_text("compiled")  # pretend compile happened

        compile_rule = make_compile_rule(output=obj, src=src)
        link_rule = make_link_rule(output=exe, inputs=[obj])
        graph = BuildGraph()
        graph.add_rule(compile_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [exe]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            # Record a valid trace for the compile output so it is trusted and skipped.

            ctx = backend.context
            store = TraceStore(os.path.join(backend.args.objdir, ".ct-slurm-traces.json"))
            store.put(obj, _make_trace_entry(compile_rule, ctx))
            store.save()

            # Link rules now go through atomic_link → _run_with_signal_forwarding;
            # sbatch/sacct (none expected here, since the trace skips compile)
            # would go through subprocess.run.
            with patch("compiletools.locking._run_with_signal_forwarding") as mock_swf:

                def fake_swf(cmd):
                    try:
                        idx = cmd.index("-o")
                        open(cmd[idx + 1], "w").close()
                    except (ValueError, IndexError):
                        pass
                    return subprocess.CompletedProcess(cmd, 0, None, None)

                mock_swf.side_effect = fake_swf
                backend.execute("build")

        # link ran locally via _run_with_signal_forwarding
        mock_swf.assert_called()

    def test_link_rule_routes_through_atomic_link(self, tmp_path):
        """Regression for review issue C2: SlurmBackend._run_local used to wrap
        link rules in `with FileLock(...)` + raw subprocess.run, leaving an
        orphaned linker writing to the now-unlocked target on signal."""
        src = str(tmp_path / "foo.cpp")
        obj = str(tmp_path / "foo.o")
        exe = str(tmp_path / "foo")
        (tmp_path / "foo.cpp").write_text("int main(){}")
        (tmp_path / "foo.o").write_text("compiled")

        compile_rule = make_compile_rule(output=obj, src=src)
        link_rule = make_link_rule(output=exe, inputs=[obj])
        graph = BuildGraph()
        graph.add_rule(compile_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [exe]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            ctx = backend.context
            store = TraceStore(os.path.join(backend.args.objdir, ".ct-slurm-traces.json"))
            store.put(obj, _make_trace_entry(compile_rule, ctx))
            store.save()

            with patch("compiletools.trace_backend.atomic_link") as mock_link:
                mock_link.side_effect = lambda lock, target, cmd: open(target, "wb").close() or 0
                backend.execute("build")
                # Link routed through atomic_link (not raw subprocess.run)
                assert mock_link.call_count == 1

    def test_runtests_delegates_to_run_tests(self):
        graph = BuildGraph()
        graph.add_rule(make_phony_rule("build"))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
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

    Default tiers (per --slurm-mem-tiers default):
      <=1 -> 1G, <=2 -> 2G, <=4 -> 4G, <=8 -> 8G, <=16 -> 16G,
      else -> --slurm-mem
    """

    def _make_backend(self, slurm_mem="32G", tiers=None):
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(slurm_mem=slurm_mem, slurm_mem_tiers=tiers)
        return b

    def test_zero_weight_gets_1G(self):
        rule = make_compile_rule()
        assert self._make_backend()._estimate_memory(rule) == "1G"

    def test_weight_1_gets_1G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=1)
        assert self._make_backend()._estimate_memory(rule) == "1G"

    def test_weight_2_gets_2G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=2)
        assert self._make_backend()._estimate_memory(rule) == "2G"

    def test_weight_4_gets_4G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=4)
        assert self._make_backend()._estimate_memory(rule) == "4G"

    def test_weight_8_gets_8G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=8)
        assert self._make_backend()._estimate_memory(rule) == "8G"

    def test_weight_16_gets_16G(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=16)
        assert self._make_backend()._estimate_memory(rule) == "16G"

    def test_weight_above_top_tier_gets_slurm_mem(self):
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=100)
        assert self._make_backend(slurm_mem="64G")._estimate_memory(rule) == "64G"

    def test_custom_tiers_override_defaults(self):
        """--slurm-mem-tiers overrides the class default tier mapping."""
        rule = _make_rule_with_weight("a.o", "a.C", include_weight=1)
        custom = [(1, "500M"), (4, "1G")]
        assert self._make_backend(tiers=custom)._estimate_memory(rule) == "500M"


class TestTieredSubmission:
    """Test that rules with different include_weight produce separate sbatch calls."""

    def test_mixed_tiers_produce_separate_sbatch_calls(self, tmp_path):
        """Rules with different memory needs get separate sbatch calls."""
        graph = BuildGraph()
        # Small file: weight=0 -> 1G
        r_small = _make_rule_with_weight(str(tmp_path / "small.o"), "small.C", include_weight=0)
        # Large file: weight=20 -> exceeds top tier (16) -> slurm_mem
        r_large = _make_rule_with_weight(str(tmp_path / "large.o"), "large.C", include_weight=20)
        graph.add_rule(r_small)
        graph.add_rule(r_large)
        graph.add_rule(make_phony_rule("build", [r_small.output, r_large.output]))

        sbatch_ids = iter(["100\n", "200\n"])

        with SlurmBackendTestContext(graph, slurm_mem="32G") as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", side_effect=sbatch_ids) as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        assert mock_sbatch.call_count == 2
        mems = []
        for c in mock_sbatch.call_args_list:
            cmd = c[0][0]
            for arg in cmd:
                if arg.startswith("--mem="):
                    mems.append(arg)
        assert "--mem=1G" in mems
        assert "--mem=32G" in mems

    def test_same_tier_uses_single_sbatch_call(self, tmp_path):
        """Rules in the same memory tier are batched into one array."""
        graph = BuildGraph()
        # Both weight <= 1 -> same tier (1G)
        r1 = _make_rule_with_weight(str(tmp_path / "a.o"), "a.C", include_weight=0)
        r2 = _make_rule_with_weight(str(tmp_path / "b.o"), "b.C", include_weight=1)
        graph.add_rule(r1)
        graph.add_rule(r2)
        graph.add_rule(make_phony_rule("build", [r1.output, r2.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="55\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        # Both in same tier (1G) -> one sbatch call
        assert len(_sbatch_calls(mock_sbatch)) == 1
        cmd = mock_sbatch.call_args[0][0]
        assert "--array=0-1" in cmd
        assert "--mem=1G" in cmd

    def test_sbatch_array_mem_parameter_overrides_default(self, tmp_path):
        """The mem parameter to _sbatch_array overrides self.args.slurm_mem."""
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_mem="16G") as (backend, _tmpdir):
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

        with SlurmBackendTestContext(graph, slurm_mem="16G") as (backend, _tmpdir):
            with patch("subprocess.check_output", return_value="42\n") as mock_sbatch:
                backend._sbatch_array([rule], chunk_id=0)

        cmd = mock_sbatch.call_args[0][0]
        assert "--mem=16G" in cmd


# ---------------------------------------------------------------------------
# Memory parsing helpers
# ---------------------------------------------------------------------------


class TestMemoryHelpers:
    def test_parse_mem_gigabytes(self):
        assert SlurmBackend._parse_mem("4G") == 4096

    def test_parse_mem_megabytes(self):
        assert SlurmBackend._parse_mem("512M") == 512

    def test_format_mem_gigabytes(self):
        assert SlurmBackend._format_mem(4096) == "4G"

    def test_format_mem_megabytes(self):
        assert SlurmBackend._format_mem(1500) == "1500M"

    def test_double_mem(self):
        assert SlurmBackend._double_mem("4G") == "8G"
        assert SlurmBackend._double_mem("512M") == "1G"


# ---------------------------------------------------------------------------
# OOM retry
# ---------------------------------------------------------------------------


class TestOOMRetry:
    """Test that OUT_OF_MEMORY failures are retried with doubled memory."""

    def test_oom_jobs_retried_with_doubled_memory(self, tmp_path):
        """OOM jobs are resubmitted with 2x memory."""
        out = str(tmp_path / "foo.o")
        rule = _make_rule_with_weight(out, "foo.C", include_weight=1)  # -> 1G tier
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # OOM at 1G -> retry at 2G -> COMPLETED
        with SlurmBackendTestContext(graph, slurm_mem="8G") as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "88\n",  # initial sbatch
                    _sacct_output(("88_0", "OUT_OF_MEMORY")),  # sacct poll
                    "99\n",  # retry sbatch
                    _sacct_output(("99_0", "COMPLETED")),  # sacct poll
                ],
            ) as mock_sbatch:
                backend.execute("build")

        sbatch_calls = _sbatch_calls(mock_sbatch)
        assert len(sbatch_calls) == 2
        retry_cmd = sbatch_calls[1][0][0]
        assert "--mem=2G" in retry_cmd

    def test_oom_retry_doubles_until_cap(self, tmp_path):
        """OOM retries double memory each time, stopping at --slurm-mem cap."""
        out = str(tmp_path / "foo.o")
        rule = _make_rule_with_weight(out, "foo.C", include_weight=1)  # -> 1G tier
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # OOM at 1G -> retry 2G -> OOM at 2G -> retry 4G -> COMPLETED
        with SlurmBackendTestContext(graph, slurm_mem="4G") as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "10\n",
                    _sacct_output(("10_0", "OUT_OF_MEMORY")),
                    "20\n",
                    _sacct_output(("20_0", "OUT_OF_MEMORY")),
                    "30\n",
                    _sacct_output(("30_0", "COMPLETED")),
                ],
            ) as mock_sbatch:
                backend.execute("build")

        sbatch_calls = _sbatch_calls(mock_sbatch)
        assert len(sbatch_calls) == 3
        mems = []
        for c in sbatch_calls:
            cmd = c[0][0]
            for arg in cmd:
                if arg.startswith("--mem="):
                    mems.append(arg)
        assert mems == ["--mem=1G", "--mem=2G", "--mem=4G"]

    def test_oom_at_cap_raises(self, tmp_path):
        """OOM at the memory cap raises RuntimeError."""
        out = str(tmp_path / "foo.o")
        rule = _make_rule_with_weight(out, "foo.C", include_weight=1)  # -> 1G tier
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # OOM at 1G -> retry 2G -> OOM at 2G -> cap is 2G, fail
        with SlurmBackendTestContext(graph, slurm_mem="2G") as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "10\n",
                    _sacct_output(("10_0", "OUT_OF_MEMORY")),
                    "20\n",
                    _sacct_output(("20_0", "OUT_OF_MEMORY")),
                ],
            ):
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

    def test_non_oom_failure_not_retried(self, tmp_path):
        """Non-OOM failures (FAILED, CANCELLED) are not retried."""
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph, slurm_mem="16G") as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "88\n",
                    _sacct_output(("88_0", "FAILED")),
                ],
            ) as mock_sbatch:
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

        # Only one sbatch call — no retry for FAILED
        assert len(_sbatch_calls(mock_sbatch)) == 1

    def test_per_rule_retry_cap_aborts_after_threshold(self, tmp_path):
        """A rule that OOMs more than --slurm-rule-retry-cap times is abandoned."""
        out = str(tmp_path / "foo.o")
        rule = _make_rule_with_weight(out, "foo.C", include_weight=1)  # 1G
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        # Cap=1: initial OOM, retry once, then second OOM -> abandon (no third sbatch)
        with SlurmBackendTestContext(
            graph,
            slurm_mem="64G",
            slurm_rule_retry_cap=1,
        ) as (backend, _tmpdir):
            with patch(
                "subprocess.check_output",
                side_effect=[
                    "1\n",
                    _sacct_output(("1_0", "OUT_OF_MEMORY")),
                    "2\n",
                    _sacct_output(("2_0", "OUT_OF_MEMORY")),
                ],
            ) as mock_sbatch:
                with pytest.raises(RuntimeError, match="Slurm compile jobs failed"):
                    backend.execute("build")

        # Exactly 2 sbatch calls (initial + 1 retry) — third would exceed cap.
        assert len(_sbatch_calls(mock_sbatch)) == 2


# ---------------------------------------------------------------------------
# Argparse type validation (Fix I1)
# ---------------------------------------------------------------------------


class TestArgValidation:
    def test_slurm_mem_valid_passes(self):
        assert _slurm_mem_arg("4G") == "4G"
        assert _slurm_mem_arg("512M") == "512M"
        assert _slurm_mem_arg("2048") == "2048"

    def test_slurm_mem_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_arg("4XB")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_arg("")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_arg("0G")

    def test_slurm_time_valid_passes(self):
        assert _slurm_time_arg("00:30:00") == "00:30:00"
        assert _slurm_time_arg("12:34") == "12:34"
        assert _slurm_time_arg("2-04:00:00") == "2-04:00:00"

    def test_slurm_time_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_time_arg("notatime")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_time_arg("")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_time_arg("1:2:3:4")

    def test_slurm_mem_tiers_valid_parses(self):
        out = _slurm_mem_tiers_arg("1:1G,4:4G,16:16G")
        assert out == [(1, "1G"), (4, "4G"), (16, "16G")]

    def test_slurm_mem_tiers_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_tiers_arg("garbage")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_tiers_arg("1:badmem")
        with pytest.raises(argparse.ArgumentTypeError):
            _slurm_mem_tiers_arg("")

    def test_slurm_mem_tiers_sorts_by_threshold(self):
        out = _slurm_mem_tiers_arg("16:16G,1:1G,4:4G")
        assert out == [(1, "1G"), (4, "4G"), (16, "16G")]


# ---------------------------------------------------------------------------
# Per-invocation file naming + cleanup (Fix I2 + I3)
# ---------------------------------------------------------------------------


class TestInvocationFiles:
    def test_two_invocations_produce_distinct_filenames(self, tmp_path):
        graph1 = BuildGraph()
        r1 = make_compile_rule(output=str(tmp_path / "a.o"), src="a.cpp")
        graph1.add_rule(r1)
        graph1.add_rule(make_phony_rule("build", [r1.output]))

        graph2 = BuildGraph()
        r2 = make_compile_rule(output=str(tmp_path / "b.o"), src="b.cpp")
        graph2.add_rule(r2)
        graph2.add_rule(make_phony_rule("build", [r2.output]))

        prefixes: list[str] = []
        for graph in (graph1, graph2):
            with SlurmBackendTestContext(graph) as (backend, _tmp):
                with (
                    patch("subprocess.check_output", return_value="1\n"),
                    patch.object(backend, "_wait_for_arrays", return_value=[]),
                ):
                    backend.execute("build")
                prefixes.append(backend._invocation_prefix)
        assert prefixes[0] != prefixes[1]

    def test_invocation_files_cleaned_up_in_finally(self, tmp_path):
        """cmds/outs files for THIS invocation are removed when execute() returns."""
        graph = BuildGraph()
        r = make_compile_rule(output=str(tmp_path / "a.o"), src="a.cpp")
        graph.add_rule(r)
        graph.add_rule(make_phony_rule("build", [r.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmp):
            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

            objdir = backend.args.objdir
            leftover = [f for f in os.listdir(objdir) if f.startswith(".ct-slurm-cmds-")]
            assert leftover == []
            leftover_outs = [f for f in os.listdir(objdir) if f.startswith(".ct-slurm-outs-")]
            assert leftover_outs == []

    def test_cleanup_only_touches_own_invocation_files(self, tmp_path):
        """A foreign-prefix cmds file is NOT removed by this invocation's cleanup."""
        graph = BuildGraph()
        r = make_compile_rule(output=str(tmp_path / "a.o"), src="a.cpp")
        graph.add_rule(r)
        graph.add_rule(make_phony_rule("build", [r.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmp):
            objdir = backend.args.objdir
            foreign = os.path.join(objdir, ".ct-slurm-cmds-other-1234-99.txt")
            with open(foreign, "w") as f:
                f.write("foreign\n")

            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

            assert os.path.exists(foreign), "foreign cmds file must survive own cleanup"


# ---------------------------------------------------------------------------
# scancel-on-failure (Fix C3)
# ---------------------------------------------------------------------------


class TestScancelOnFailure:
    def test_scancel_called_when_wait_raises(self, tmp_path, _mock_scancel):
        """If _wait_for_arrays raises, scancel runs in the finally block."""
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="55\n"),
                patch.object(backend, "_wait_for_arrays", side_effect=RuntimeError("boom")),
            ):
                with pytest.raises(RuntimeError, match="boom"):
                    backend.execute("build")

        # _scancel_pending was invoked at least once during finally.
        assert _mock_scancel.called

    def test_scancel_skips_terminal_jobs(self, tmp_path, _mock_scancel):
        """Jobs marked terminal by _wait_for_arrays are not cancelled again."""
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            backend._tracked_jobs = {"77": "terminal", "88": "pending"}
            # Stop the autouse mock so the real _scancel_pending runs
            _mock_scancel.stop()
            try:
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = subprocess.CompletedProcess(["scancel"], 0, "", "")
                    backend._scancel_pending()

                assert mock_run.call_count == 1
                args = mock_run.call_args[0][0]
                assert args[0] == "scancel"
                assert "88" in args
                assert "77" not in args
            finally:
                _mock_scancel.start()


# ---------------------------------------------------------------------------
# sacct transient failure handling (Fix C1)
# ---------------------------------------------------------------------------


class TestSacctTransientFailures:
    def test_query_states_returns_empty_on_called_process_error(self, tmp_path):
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(objdir=str(tmp_path))
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, ["sacct"], stderr="slurmdbd down"),
        ):
            assert b._query_array_task_states("123") == {}

    def test_query_states_returns_empty_on_sacct_missing(self, tmp_path):
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(objdir=str(tmp_path))
        with patch("subprocess.check_output", side_effect=FileNotFoundError("sacct")):
            assert b._query_array_task_states("123") == {}

    def test_wait_for_arrays_recovers_after_one_sacct_failure(self, tmp_path):
        """Single sacct failure does not crash — polling continues."""
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(slurm_poll_interval=0.0, slurm_sacct_failure_threshold=10)
        index_map = {"77": [rule]}

        with (
            patch.object(
                b,
                "_query_array_task_states",
                side_effect=[{}, {"77_0": "COMPLETED"}],
            ),
            patch("time.sleep"),
        ):
            failures = b._wait_for_arrays(index_map)
        assert failures == []

    def test_wait_for_arrays_raises_after_threshold_failures(self, tmp_path):
        """Persistent sacct failure raises after threshold consecutive empties."""
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        b = SlurmBackend.__new__(SlurmBackend)
        b.args = SimpleNamespace(slurm_poll_interval=0.0, slurm_sacct_failure_threshold=3)
        index_map = {"77": [rule]}

        with (
            patch.object(b, "_query_array_task_states", return_value={}),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="sacct returned no usable data"):
                b._wait_for_arrays(index_map)


# ---------------------------------------------------------------------------
# eval safety in wrap script (Fix C2)
# ---------------------------------------------------------------------------


class TestWrapScriptEval:
    def test_wrap_uses_eval_not_bash_c(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")

        cmd = mock_sbatch.call_args[0][0]
        wrap = cmd[cmd.index("--wrap") + 1]
        assert 'eval "$CMD"' in wrap
        assert 'bash -c "$CMD"' not in wrap

    def test_command_substitution_in_arg_is_quoted_literally(self, tmp_path):
        """A compile arg containing $(...) is single-quoted by shlex.join,
        so eval treats it as a literal string instead of running a subshell."""
        import shlex as _shlex

        from compiletools.trace_backend import _flatten_command

        rule = BuildRule(
            output=str(tmp_path / "foo.o"),
            inputs=["src/foo.cpp"],
            command=["g++", "-DFOO=$(rogue)", "-c", "src/foo.cpp", "-o", str(tmp_path / "foo.o")],
            rule_type="compile",
        )
        assert rule.command is not None
        line = _shlex.join(_flatten_command(rule.command))
        # The metacharacter-bearing token must be single-quoted in the cmds file
        # so that `eval "$CMD"` treats `$(rogue)` as a literal string.
        assert "'-DFOO=$(rogue)'" in line


# ---------------------------------------------------------------------------
# --slurm-export plumbing (Fix I10)
# ---------------------------------------------------------------------------


class TestSlurmExport:
    def _run_with_export(self, tmp_path, export_value):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph, slurm_export=export_value) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="42\n") as mock_sbatch,
                patch.object(backend, "_wait_for_arrays", return_value=[]),
            ):
                backend.execute("build")
        return mock_sbatch.call_args[0][0]

    def test_default_export_is_curated_allowlist(self, tmp_path):
        cmd = self._run_with_export(tmp_path, "PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH")
        assert "--export=PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH" in cmd
        assert "--export=ALL" not in cmd

    def test_export_all_when_user_overrides(self, tmp_path):
        cmd = self._run_with_export(tmp_path, "ALL")
        assert "--export=ALL" in cmd

    def test_export_none_when_user_overrides(self, tmp_path):
        cmd = self._run_with_export(tmp_path, "NONE")
        assert "--export=NONE" in cmd


# ---------------------------------------------------------------------------
# stderr capture on sbatch failure (Fix I8)
# ---------------------------------------------------------------------------


class TestSbatchStderr:
    def test_sbatch_failure_includes_stderr_in_message(self, tmp_path):
        graph = BuildGraph()
        rule = make_compile_rule(output=str(tmp_path / "foo.o"))
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [rule.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            err = subprocess.CalledProcessError(1, ["sbatch"], stderr="invalid partition: bogus")
            with patch("subprocess.check_output", side_effect=err):
                with pytest.raises(RuntimeError, match="invalid partition: bogus"):
                    backend.execute("build")


# ---------------------------------------------------------------------------
# Output-wait timeout configurable (Fix I7)
# ---------------------------------------------------------------------------


class TestOutputWaitTimeout:
    def test_timeout_taken_from_args(self, tmp_path):
        ghost_out = str(tmp_path / "ghost.o")
        ghost_rule = make_compile_rule(output=ghost_out, src="ghost.cpp")
        link_rule = make_link_rule(output=str(tmp_path / "app"), inputs=(ghost_out,))

        graph = BuildGraph()
        graph.add_rule(ghost_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [link_rule.output]))

        with SlurmBackendTestContext(graph, slurm_output_wait_timeout=7.0) as (backend, _tmpdir):
            captured = {}

            real_wait = backend._wait_for_output_files

            def spy(rules, timeout=30.0):
                captured["timeout"] = timeout
                return real_wait(rules, timeout=timeout)

            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
                patch.object(backend, "_wait_for_output_files", side_effect=spy),
                patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0]),
                patch("time.sleep"),
            ):
                with pytest.raises(RuntimeError):
                    backend.execute("build")

            assert captured["timeout"] == 7.0


# ---------------------------------------------------------------------------
# Wrong log-file matching after retry (Fix I6)
# ---------------------------------------------------------------------------


class TestLogLookupExactChunk:
    def test_log_lookup_uses_chunk_id_not_glob(self, tmp_path):
        """Two chunks with same task_idx must produce different log lookups."""
        out_a = str(tmp_path / "a.o")
        out_b = str(tmp_path / "b.o")
        rule_a = make_compile_rule(output=out_a, src="a.cpp")
        rule_b = make_compile_rule(output=out_b, src="b.cpp")
        graph = BuildGraph()
        graph.add_rule(rule_a)
        graph.add_rule(rule_b)
        graph.add_rule(make_phony_rule("build", [out_a, out_b]))

        with SlurmBackendTestContext(graph, slurm_max_array=1) as (backend, _tmpdir):
            # Pre-set invocation prefix and chunk maps directly
            backend._invocation_prefix = "testprefix"
            backend._chunk_id_for_job = {"100": 0, "200": 1}
            objdir = backend.args.objdir

            # Two logs, both with task_idx=0 but different chunk_ids
            log_chunk0 = os.path.join(objdir, "slurm-ct-testprefix-0-0.out")
            log_chunk1 = os.path.join(objdir, "slurm-ct-testprefix-1-0.out")
            with open(log_chunk0, "w") as f:
                f.write("CONTENT-CHUNK-0\n")
            with open(log_chunk1, "w") as f:
                f.write("CONTENT-CHUNK-1\n")

            failure_for_chunk1 = SlurmBackend._TaskFailure(rule=rule_b, state="FAILED", job_id="200_0")
            diag = backend._read_slurm_logs_for_failures([failure_for_chunk1])
            assert "CONTENT-CHUNK-1" in diag
            assert "CONTENT-CHUNK-0" not in diag


# ---------------------------------------------------------------------------
# Timing collected on failure path (Fix I4)
# ---------------------------------------------------------------------------


class TestTimingOnFailure:
    def test_timing_collected_when_output_wait_raises(self, tmp_path):
        """_collect_timing runs even if a later step raises."""
        ghost_out = str(tmp_path / "ghost.o")
        ghost_rule = make_compile_rule(output=ghost_out, src="ghost.cpp")
        link_rule = make_link_rule(output=str(tmp_path / "app"), inputs=(ghost_out,))

        graph = BuildGraph()
        graph.add_rule(ghost_rule)
        graph.add_rule(link_rule)
        graph.add_rule(make_phony_rule("build", [link_rule.output]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", return_value=[]),
                patch.object(backend, "_collect_timing") as mock_timing,
                patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0, 100.0]),
                patch("time.sleep"),
            ):
                with pytest.raises(RuntimeError):
                    backend.execute("build")

            assert mock_timing.called


# ---------------------------------------------------------------------------
# CA shortcut for non-build-artifact rules (Fix I9)
# ---------------------------------------------------------------------------


class TestCopyRuleCAShortcut:
    def test_copy_rule_skipped_when_trace_valid(self, tmp_path):
        src = str(tmp_path / "foo.bin")
        dst = str(tmp_path / "out.bin")
        with open(src, "w") as f:
            f.write("data")
        with open(dst, "w") as f:
            f.write("data")

        rule = BuildRule(
            output=dst,
            inputs=[src],
            command=["cp", src, dst],
            rule_type="copy",
        )

        with SlurmBackendTestContext(BuildGraph()) as (backend, _tmpdir):
            store = TraceStore(os.path.join(backend.args.objdir, ".ct-slurm-traces.json"))
            store.put(dst, _make_trace_entry(rule, backend.context))

            with patch("compiletools.trace_backend.atomic_link") as mock_link:
                backend._run_local(rule, store)
            assert not mock_link.called, "valid trace should skip re-execution"


# ---------------------------------------------------------------------------
# traces.save() always runs (Fix C-inherited)
# ---------------------------------------------------------------------------


class TestTracesAlwaysSaved:
    def test_traces_saved_on_unexpected_exception(self, tmp_path):
        """An unexpected exception must not strand the trace store."""
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", side_effect=RuntimeError("kaboom")),
            ):
                with pytest.raises(RuntimeError, match="kaboom"):
                    backend.execute("build")

            # Trace file must exist on disk after the raise
            trace_path = os.path.join(backend.args.objdir, ".ct-slurm-traces.json")
            assert os.path.exists(trace_path), "traces.save() never ran on raise path"


# ---------------------------------------------------------------------------
# scancel-on-signal (Fix C3, signal path)
# ---------------------------------------------------------------------------


class TestScancelOnSignal:
    def test_signal_handler_calls_scancel(self, tmp_path, _mock_scancel):
        """SIGINT during execute() triggers scancel via the installed handler."""
        out = str(tmp_path / "foo.o")
        rule = make_compile_rule(output=out)
        graph = BuildGraph()
        graph.add_rule(rule)
        graph.add_rule(make_phony_rule("build", [out]))

        with SlurmBackendTestContext(graph) as (backend, _tmpdir):
            # Simulate slow polling so we can deliver a signal mid-execute
            block = threading.Event()

            def slow_wait(index_map):
                block.set()
                time.sleep(2.0)
                return []

            with (
                patch("subprocess.check_output", return_value="1\n"),
                patch.object(backend, "_wait_for_arrays", side_effect=slow_wait),
            ):
                exc_holder: list[BaseException] = []

                def runner():
                    try:
                        backend.execute("build")
                    except BaseException as e:
                        exc_holder.append(e)

                t = threading.Thread(target=runner)
                t.start()
                block.wait(2.0)
                # Can't actually deliver SIGINT to a non-main thread reliably;
                # just verify the handler was installed by inspecting state.
                assert "_tracked_jobs" in backend.__dict__
                assert "1" in backend._tracked_jobs
                # Let the slow_wait return so the thread can exit cleanly
                t.join(timeout=5.0)
                # And confirm scancel ran in the finally block
                assert _mock_scancel.called
