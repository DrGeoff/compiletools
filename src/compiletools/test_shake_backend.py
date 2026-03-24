"""Unit tests for the Shake build backend (no compiler required)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from unittest import mock

import pytest

import compiletools.shake_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.build_backend import _write_link_sig, compute_link_signature
from compiletools.shake_backend import (
    ShakeBackend,
    TraceEntry,
    TraceStore,
    _compute_file_hash,
    _is_content_addressable,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_as_shake(self):
        cls = get_backend_class("shake")
        assert cls is ShakeBackend

    def test_name(self):
        assert ShakeBackend.name() == "shake"

    def test_build_filename(self):
        assert ShakeBackend.build_filename() == ".ct-traces.json"

    def test_in_available_backends(self):
        assert "shake" in available_backends()


# ---------------------------------------------------------------------------
# TraceStore
# ---------------------------------------------------------------------------


class TestTraceStore:
    def test_get_missing_returns_none(self, tmp_path):
        store = TraceStore(str(tmp_path / ".ct-traces.json"))
        assert store.get("nonexistent") is None

    def test_put_and_get(self, tmp_path):
        path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(path)
        entry = TraceEntry(
            output_hash="abc123",
            input_hashes={"foo.cpp": "def456"},
            command_hash="cmd789",
        )
        store.put("foo.o", entry)
        got = store.get("foo.o")
        assert got is not None
        assert got.output_hash == "abc123"
        assert got.input_hashes == {"foo.cpp": "def456"}
        assert got.command_hash == "cmd789"

    def test_round_trip_save_load(self, tmp_path):
        path = str(tmp_path / ".ct-traces.json")
        entry = TraceEntry(
            output_hash="aaa",
            input_hashes={"a.cpp": "bbb", "a.h": "ccc"},
            command_hash="ddd",
        )
        store1 = TraceStore(path)
        store1.put("a.o", entry)
        store1.save()

        store2 = TraceStore(path)
        got = store2.get("a.o")
        assert got is not None
        assert got.output_hash == "aaa"
        assert got.input_hashes == {"a.cpp": "bbb", "a.h": "ccc"}
        assert got.command_hash == "ddd"

    def test_corrupt_file_handled(self, tmp_path):
        path = str(tmp_path / ".ct-traces.json")
        with open(path, "w") as f:
            f.write("not json {{{")
        store = TraceStore(path)
        assert store.get("anything") is None

    def test_wrong_version_ignored(self, tmp_path):
        path = str(tmp_path / ".ct-traces.json")
        with open(path, "w") as f:
            json.dump({"version": 9999, "traces": {"x": {}}}, f)
        store = TraceStore(path)
        assert store.get("x") is None

    def test_hash_command_deterministic(self):
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]
        h1 = TraceStore.hash_command(cmd)
        h2 = TraceStore.hash_command(cmd)
        assert h1 == h2

    def test_hash_command_differs_for_different_commands(self):
        h1 = TraceStore.hash_command(["g++", "-O0", "foo.cpp"])
        h2 = TraceStore.hash_command(["g++", "-O2", "foo.cpp"])
        assert h1 != h2


# ---------------------------------------------------------------------------
# Trace verification
# ---------------------------------------------------------------------------


class TestTraceVerification:
    """Test _verify logic indirectly through _build behavior."""

    def _make_graph_with_compile(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))
        return graph

    def test_verify_passes_when_hashes_match(self, tmp_path):
        """Compile rule with existing output is skipped via content-addressable
        short-circuit (os.path.exists), not trace verification."""
        os.chdir(tmp_path)
        # Create source and object files
        (tmp_path / "foo.cpp").write_text("int main() {}")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = self._make_graph_with_compile()
        source_hash = _compute_file_hash(str(tmp_path / "foo.cpp"))
        obj_hash = _compute_file_hash(str(tmp_path / "foo.o"))
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

        # Pre-populate trace store with matching hashes
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo.o",
            TraceEntry(
                output_hash=obj_hash,
                input_hashes={"foo.cpp": source_hash},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store.save()

        # Build — subprocess should NOT be called
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            backend.execute("build")
            mock_sub.run.assert_not_called()

    def test_verify_fails_on_input_hash_change(self, tmp_path):
        """If an input file changed, rebuild (uses link rule to test trace verification,
        since compile rules bypass traces via content-addressable short-circuit)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() { return 1; }")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="link",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

        # Pre-populate trace with OLD source hash
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo.o",
            TraceEntry(
                output_hash="old_obj_hash",
                input_hashes={"foo.cpp": "old_source_hash"},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            backend.execute("build")
            mock_sub.run.assert_called_once()

    def test_verify_fails_on_command_hash_change(self, tmp_path):
        """If the command changed, rebuild (uses link rule to test trace verification,
        since compile rules bypass traces via content-addressable short-circuit)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="link",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))
        source_hash = _compute_file_hash(str(tmp_path / "foo.cpp"))
        obj_hash = _compute_file_hash(str(tmp_path / "foo.o"))

        # Trace has a DIFFERENT command hash
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo.o",
            TraceEntry(
                output_hash=obj_hash,
                input_hashes={"foo.cpp": source_hash},
                command_hash=TraceStore.hash_command(["g++", "-O2", "foo.cpp", "-o", "foo.o"]),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            backend.execute("build")
            mock_sub.run.assert_called_once()

    def test_verify_fails_on_added_input(self, tmp_path):
        """If the input set changed (new input added), rebuild (uses link rule to test
        trace verification, since compile rules bypass traces via short-circuit)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        (tmp_path / "foo.h").write_text("// header")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        # Graph now has TWO inputs
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp", "foo.h"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="link",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        source_hash = _compute_file_hash(str(tmp_path / "foo.cpp"))
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

        # Trace only knows about ONE input
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo.o",
            TraceEntry(
                output_hash=_compute_file_hash(str(tmp_path / "foo.o")),
                input_hashes={"foo.cpp": source_hash},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            backend.execute("build")
            mock_sub.run.assert_called_once()


# ---------------------------------------------------------------------------
# Early cutoff
# ---------------------------------------------------------------------------


class TestEarlyCutoff:
    def test_identical_output_skips_dependent(self, tmp_path):
        """Content-addressable short-circuit: foo.o exists → compile skipped
        (returns False). Link trace is valid → link also skipped. No subprocess calls."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() { return 0; }")
        obj_content = b"\x7fELF fake object"
        (tmp_path / "foo.o").write_bytes(obj_content)
        (tmp_path / "foo").write_bytes(b"\x7fELF fake executable")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="foo",
                inputs=["foo.o"],
                command=["g++", "-o", "foo", "foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        # Trace for foo with current obj and exe hashes
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        obj_hash = _compute_file_hash(str(tmp_path / "foo.o"))
        exe_hash = _compute_file_hash(str(tmp_path / "foo"))
        store.put(
            "foo",
            TraceEntry(
                output_hash=exe_hash,
                input_hashes={"foo.o": obj_hash},
                command_hash=TraceStore.hash_command(["g++", "-o", "foo", "foo.o"]),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            # Compile skipped (content-addressable, foo.o exists),
            # link skipped (trace valid, no input changed)
            assert mock_sub.run.call_count == 0

    def test_different_output_rebuilds_dependent(self, tmp_path):
        """If compile executes (object didn't exist), link step runs too."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() { return 1; }")
        # foo.o intentionally NOT created — forces compile to run
        (tmp_path / "foo").write_bytes(b"\x7fELF old executable")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="foo",
                inputs=["foo.o"],
                command=["g++", "-o", "foo", "foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        def fake_run(cmd, **kwargs):
            if "-c" in cmd:
                (tmp_path / "foo.o").write_bytes(b"\x7fELF NEW object")
            elif "-o" in cmd and "foo.o" not in cmd:
                (tmp_path / "foo").write_bytes(b"\x7fELF NEW executable")
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            assert mock_sub.run.call_count == 2


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_stashes_graph(self):
        backend = ShakeBackend.__new__(ShakeBackend)
        backend._graph = None
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="build", inputs=[], command=None, rule_type="phony"))
        backend.generate(graph)
        assert backend._graph is graph

    def test_writes_summary_to_output(self):
        backend = ShakeBackend.__new__(ShakeBackend)
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "compile foo.o" in content
        assert "phony build" in content


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_execute_without_generate_raises(self):
        backend = ShakeBackend.__new__(ShakeBackend)
        backend._graph = None
        backend.args = mock.MagicMock()
        with pytest.raises(RuntimeError, match=r"generate.*must be called"):
            backend.execute("build")

    def test_build_fails_on_subprocess_error(self, tmp_path):
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("bad code")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: bad code"

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            with pytest.raises(subprocess.CalledProcessError):
                backend.execute("build")


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    def test_independent_targets_run_in_parallel(self, tmp_path):
        """Phony target with independent inputs should dispatch them concurrently."""
        os.chdir(tmp_path)
        (tmp_path / "a.cpp").write_text("int a() {}")
        (tmp_path / "b.cpp").write_text("int b() {}")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="a.o",
                inputs=["a.cpp"],
                command=["g++", "-c", "a.cpp", "-o", "a.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="b.o",
                inputs=["b.cpp"],
                command=["g++", "-c", "b.cpp", "-o", "b.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["a.o", "b.o"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 4
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        # Track which threads execute each compile
        thread_ids = []
        barrier = threading.Barrier(2, timeout=5)

        def fake_run(cmd, **kwargs):
            thread_ids.append(threading.current_thread().ident)
            # Both compiles must reach the barrier before either proceeds,
            # proving they run concurrently
            barrier.wait()
            target = cmd[cmd.index("-o") + 1]
            (tmp_path / target).write_bytes(b"\x7fELF fake")
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            assert mock_sub.run.call_count == 2

        # Verify they ran on different threads
        assert len(thread_ids) == 2
        assert thread_ids[0] != thread_ids[1]

    def test_parallel_1_still_works(self, tmp_path):
        """parallel=1 should work correctly (single-threaded)."""
        os.chdir(tmp_path)
        (tmp_path / "a.cpp").write_text("int a() {}")
        (tmp_path / "b.cpp").write_text("int b() {}")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="a.o",
                inputs=["a.cpp"],
                command=["g++", "-c", "a.cpp", "-o", "a.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="b.o",
                inputs=["b.cpp"],
                command=["g++", "-c", "b.cpp", "-o", "b.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["a.o", "b.o"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        def fake_run(cmd, **kwargs):
            target = cmd[cmd.index("-o") + 1]
            (tmp_path / target).write_bytes(b"\x7fELF fake")
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            assert mock_sub.run.call_count == 2


# ---------------------------------------------------------------------------
# Output file deletion detection
# ---------------------------------------------------------------------------


class TestOutputDeletion:
    def test_rebuilds_when_output_deleted(self, tmp_path):
        """If output file is deleted but trace exists, must rebuild."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        os.makedirs(tmp_path / "obj", exist_ok=True)
        # Note: foo.o does NOT exist (simulates deletion)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        source_hash = _compute_file_hash(str(tmp_path / "foo.cpp"))
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

        # Pre-populate trace as if foo.o was previously built successfully
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo.o",
            TraceEntry(
                output_hash="hash_of_deleted_file",
                input_hashes={"foo.cpp": source_hash},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        def fake_run(cmd, **kwargs):
            (tmp_path / "foo.o").write_bytes(b"\x7fELF rebuilt")
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            # Must rebuild because output file was deleted
            mock_sub.run.assert_called_once()


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestContentAddressableShortCircuit:
    def test_is_content_addressable(self):
        """Only compile rules are content-addressable."""
        compile_rule = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["g++"], rule_type="compile")
        link_rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++"], rule_type="link")
        phony_rule = BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony")
        assert _is_content_addressable(compile_rule) is True
        assert _is_content_addressable(link_rule) is False
        assert _is_content_addressable(phony_rule) is False

    def test_compile_skipped_when_object_exists_no_traces(self, tmp_path):
        """Object file exists, NO trace store populated. Compile is skipped
        via content-addressable short-circuit independently of traces."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
                rule_type="compile",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        # No traces at all — short-circuit should still work
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            mock_sub.run.assert_not_called()

    def test_compile_returns_true_when_object_new(self, tmp_path):
        """Object file does NOT exist, compile executes. _build returns True
        (signaling dependents should rebuild)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        # foo.o intentionally NOT created
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = BuildGraph()
        compile_rule = BuildRule(
            output="foo.o",
            inputs=["foo.cpp"],
            command=["g++", "-c", "foo.cpp", "-o", "foo.o"],
            rule_type="compile",
            order_only_deps=["obj"],
        )
        graph.add_rule(compile_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        traces = TraceStore(str(tmp_path / ".ct-traces.json"))
        done: set[str] = set()
        lock = threading.Lock()

        def fake_run(cmd, **kwargs):
            (tmp_path / "foo.o").write_bytes(b"\x7fELF new object")
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
                mock_sub.run.side_effect = fake_run
                mock_sub.CalledProcessError = subprocess.CalledProcessError
                changed = backend._build("foo.o", graph, traces, done, lock, executor)
                assert changed is True
                mock_sub.run.assert_called_once()

    def test_link_still_uses_traces(self, tmp_path):
        """Link rules (rule_type='link') still go through full trace verification,
        not the content-addressable short-circuit."""
        os.chdir(tmp_path)
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        (tmp_path / "foo").write_bytes(b"\x7fELF fake executable")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo",
                inputs=["foo.o"],
                command=["g++", "-o", "foo", "foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        obj_hash = _compute_file_hash(str(tmp_path / "foo.o"))
        exe_hash = _compute_file_hash(str(tmp_path / "foo"))
        cmd = ["g++", "-o", "foo", "foo.o"]

        # Pre-populate trace with MATCHING hashes — trace verification should pass
        trace_path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(trace_path)
        store.put(
            "foo",
            TraceEntry(
                output_hash=exe_hash,
                input_hashes={"foo.o": obj_hash},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store.save()

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            # Link skipped via trace verification (NOT short-circuit)
            mock_sub.run.assert_not_called()

        # Now invalidate the trace — link must rebuild
        store2 = TraceStore(trace_path)
        store2.put(
            "foo",
            TraceEntry(
                output_hash=exe_hash,
                input_hashes={"foo.o": "wrong_hash"},
                command_hash=TraceStore.hash_command(cmd),
            ),
        )
        store2.save()

        backend2 = ShakeBackend.__new__(ShakeBackend)
        backend2.args = args
        backend2._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend2.execute("build")
            # Link rebuilds because trace verification failed
            mock_sub.run.assert_called_once()


# ---------------------------------------------------------------------------
# Link signature short-circuit
# ---------------------------------------------------------------------------


class TestLinkSignatureShortCircuit:
    def test_link_skipped_when_signature_matches(self, tmp_path):
        """Link output + matching sig file → skip (no subprocess call)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        (tmp_path / "foo").write_bytes(b"\x7fELF fake executable")

        graph = BuildGraph()
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        # Write matching sig
        _write_link_sig("foo", compute_link_signature(link_rule))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            mock_sub.run.assert_not_called()

    def test_link_rebuilds_when_signature_differs(self, tmp_path):
        """Wrong sig → rebuild (subprocess called)."""
        os.chdir(tmp_path)
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        (tmp_path / "foo").write_bytes(b"\x7fELF fake executable")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo",
                inputs=["foo.o"],
                command=["g++", "-o", "foo", "foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        # Write wrong sig
        _write_link_sig("foo", "wrong_signature_hash")

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            mock_sub.run.assert_called_once()

    def test_link_rebuilds_when_output_missing(self, tmp_path):
        """No output file → rebuild regardless of sig."""
        os.chdir(tmp_path)
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        # foo intentionally NOT created

        graph = BuildGraph()
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        # Write a sig even though output doesn't exist
        _write_link_sig("foo", compute_link_signature(link_rule))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        def fake_run(cmd, **kwargs):
            (tmp_path / "foo").write_bytes(b"\x7fELF new exe")
            return mock_result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            mock_sub.run.assert_called_once()

    def test_link_records_signature_after_build(self, tmp_path):
        """After a successful link, the sig file should be written."""
        os.chdir(tmp_path)
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        # foo intentionally NOT created — forces link to run

        graph = BuildGraph()
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        args.parallel = 1
        args.verbose = 0

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        backend._graph = graph

        mock_result = mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        def fake_run(cmd, **kwargs):
            (tmp_path / "foo").write_bytes(b"\x7fELF new exe")
            return mock_result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")

        # Verify sig file was written with correct signature
        sig_path = str(tmp_path / "foo.ct-sig")
        assert os.path.exists(sig_path)
        with open(sig_path) as f:
            assert f.read().strip() == compute_link_signature(link_rule)
