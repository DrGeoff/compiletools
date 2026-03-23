"""Unit tests for the Shake build backend (no compiler required)."""

from __future__ import annotations

import io
import json
import os
import subprocess
from unittest import mock

import pytest

import compiletools.shake_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.shake_backend import (
    ShakeBackend,
    TraceEntry,
    TraceStore,
    _compute_file_hash,
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
        """If input hashes and command hash match the trace, skip rebuild."""
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
        """If an input file changed, rebuild."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() { return 1; }")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = self._make_graph_with_compile()
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
        """If the command changed, rebuild."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() {}")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF fake object")
        os.makedirs(tmp_path / "obj", exist_ok=True)

        graph = self._make_graph_with_compile()
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
        """If the input set changed (new input added), rebuild."""
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
                rule_type="compile",
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
        """If a compile produces byte-identical output, the link step is skipped."""
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

        # Trace for foo.o with OLD source hash (triggers rebuild)
        # Trace for foo with current obj hash
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

        # Compile subprocess writes same bytes (early cutoff)
        def fake_run(cmd, **kwargs):
            if "-c" in cmd:
                (tmp_path / "foo.o").write_bytes(obj_content)  # identical
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch("compiletools.shake_backend.subprocess") as mock_sub:
            mock_sub.run.side_effect = fake_run
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            backend.execute("build")
            # Compile called, but link NOT called (early cutoff)
            assert mock_sub.run.call_count == 1
            called_cmd = mock_sub.run.call_args[0][0]
            assert "-c" in called_cmd

    def test_different_output_rebuilds_dependent(self, tmp_path):
        """If compile output changes, link step runs."""
        os.chdir(tmp_path)
        (tmp_path / "foo.cpp").write_text("int main() { return 1; }")
        (tmp_path / "foo.o").write_bytes(b"\x7fELF old object")
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
