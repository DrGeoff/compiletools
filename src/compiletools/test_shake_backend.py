"""Unit tests for the Shake build backend (no compiler required)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from pathlib import Path
from unittest import mock

import pytest

import compiletools.shake_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.shake_backend import (
    ShakeBackend,
    TraceEntry,
    TraceStore,
    _is_content_addressable,
    hash_command,
)

from compiletools.testhelper import ShakeBackendTestContext, fake_subprocess_result


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
        h1 = hash_command(cmd)
        h2 = hash_command(cmd)
        assert h1 == h2

    def test_hash_command_differs_for_different_commands(self):
        h1 = hash_command(["g++", "-O0", "foo.cpp"])
        h2 = hash_command(["g++", "-O2", "foo.cpp"])
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

    def test_verify_passes_when_hashes_match(self, monkeypatch):
        """Compile rule with existing output is skipped via content-addressable
        short-circuit (os.path.exists), not trace verification."""
        graph = self._make_graph_with_compile()

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            # Create source and object files
            (td / "foo.cpp").write_text("int main() {}")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            source_hash = get_file_hash(str(td / "foo.cpp"))
            obj_hash = get_file_hash(str(td / "foo.o"))
            cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

            # Pre-populate trace store with matching hashes
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash=obj_hash,
                    input_hashes={"foo.cpp": source_hash},
                    command_hash=hash_command(cmd),
                ),
            )
            store.save()

            # Build — subprocess should NOT be called
            with mock.patch("compiletools.shake_backend.subprocess.run") as mock_run:
                backend.execute("build")
                mock_run.assert_not_called()

    def test_verify_fails_on_input_hash_change(self, monkeypatch):
        """If an input file changed, rebuild (uses link rule to test trace verification,
        since compile rules bypass traces via content-addressable short-circuit)."""
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

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() { return 1; }")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

            # Pre-populate trace with OLD source hash
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash="old_obj_hash",
                    input_hashes={"foo.cpp": "old_source_hash"},
                    command_hash=hash_command(cmd),
                ),
            )
            store.save()

            rule = graph.rules[0]
            ca = backend._ca_target(rule)

            def fake_run(cmd, **kwargs):
                with open(ca, "wb") as f:
                    f.write(b"\x7fELF rebuilt")
                return fake_subprocess_result()

            with mock.patch("compiletools.shake_backend.subprocess.run", side_effect=fake_run) as mock_run:
                backend.execute("build")
                mock_run.assert_called_once()

    def test_verify_fails_on_command_hash_change(self, monkeypatch):
        """If the command changed, rebuild (uses copy rule to test trace verification,
        since compile/link/library rules bypass traces via content-addressable short-circuit)."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["cp", "foo.cpp", "foo.o"],
                rule_type="copy",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() {}")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            source_hash = get_file_hash(str(td / "foo.cpp"))
            obj_hash = get_file_hash(str(td / "foo.o"))

            # Trace has a DIFFERENT command hash
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash=obj_hash,
                    input_hashes={"foo.cpp": source_hash},
                    command_hash=hash_command(["cp", "-v", "foo.cpp", "foo.o"]),
                ),
            )
            store.save()

            mock_result = fake_subprocess_result()

            with mock.patch("compiletools.shake_backend.subprocess.run", return_value=mock_result) as mock_run:
                backend.execute("build")
                mock_run.assert_called_once()

    def test_verify_fails_on_added_input(self, monkeypatch):
        """If the input set changed (new input added), rebuild (uses copy rule to test
        trace verification, since compile/link/library rules bypass traces via short-circuit)."""
        # Graph now has TWO inputs
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp", "foo.h"],
                command=["cp", "foo.cpp", "foo.o"],
                rule_type="copy",
                order_only_deps=["obj"],
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["foo.o"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() {}")
            (td / "foo.h").write_text("// header")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            source_hash = get_file_hash(str(td / "foo.cpp"))
            cmd = ["cp", "foo.cpp", "foo.o"]

            # Trace only knows about ONE input
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash=get_file_hash(str(td / "foo.o")),
                    input_hashes={"foo.cpp": source_hash},
                    command_hash=hash_command(cmd),
                ),
            )
            store.save()

            mock_result = fake_subprocess_result()

            with mock.patch("compiletools.shake_backend.subprocess.run", return_value=mock_result) as mock_run:
                backend.execute("build")
                # subprocess.run may also be called by git_utils (e.g. git rev-parse
                # via check_output which delegates to run in Python 3.14+), so check
                # for the specific build command rather than assert_called_once.
                build_calls = [c for c in mock_run.call_args_list if c.args[0] == cmd]
                assert len(build_calls) == 1


# ---------------------------------------------------------------------------
# Early cutoff
# ---------------------------------------------------------------------------


class TestEarlyCutoff:
    def test_identical_output_skips_dependent(self, monkeypatch):
        """Content-addressable short-circuit: foo.o exists → compile skipped.
        CA link target exists → link also skipped. No subprocess calls."""
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
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
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() { return 0; }")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            # Pre-create the CA link target so the short-circuit fires
            ca = backend._ca_target(link_rule)
            with open(ca, "wb") as f:
                f.write(b"\x7fELF cached executable")

            with mock.patch("compiletools.shake_backend.subprocess.run") as mock_run:
                backend.execute("build")
                # Compile skipped (content-addressable, foo.o exists),
                # link skipped (CA target exists, copied to human target)
                assert mock_run.call_count == 0

    def test_different_output_rebuilds_dependent(self, monkeypatch):
        """If compile executes (object didn't exist), link step runs too."""
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
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
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph, file_locking=False) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() { return 1; }")
            # foo.o intentionally NOT created — forces compile to run
            os.makedirs(td / "obj", exist_ok=True)

            ca = backend._ca_target(link_rule)
            compile_calls = []

            def fake_atomic(target, cmd):
                compile_calls.append(target)
                (td / "foo.o").write_bytes(b"\x7fELF NEW object")
                return fake_subprocess_result()

            def fake_run(cmd, **kwargs):
                # Link builds to the CA target path
                with open(ca, "wb") as f:
                    f.write(b"\x7fELF NEW executable")
                return fake_subprocess_result()

            with (
                mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic),
                mock.patch("compiletools.shake_backend.subprocess.run", side_effect=fake_run) as mock_run,
            ):
                backend.execute("build")
                assert len(compile_calls) == 1  # compile via atomic
                assert mock_run.call_count == 1  # link via subprocess


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

    def test_build_fails_on_subprocess_error(self, monkeypatch):
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

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("bad code")
            os.makedirs(td / "obj", exist_ok=True)

            def fake_atomic_fail(target, cmd):
                raise subprocess.CalledProcessError(1, cmd, "", "error: bad code")

            with mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic_fail):
                with pytest.raises(subprocess.CalledProcessError):
                    backend.execute("build")


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    def test_independent_targets_run_in_parallel(self, monkeypatch):
        """Phony target with independent inputs should dispatch them concurrently."""
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

        with ShakeBackendTestContext(graph, parallel=4) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "a.cpp").write_text("int a() {}")
            (td / "b.cpp").write_text("int b() {}")
            os.makedirs(td / "obj", exist_ok=True)

            # Track which threads execute each compile
            thread_ids = []
            barrier = threading.Barrier(2, timeout=5)

            def fake_atomic(target, cmd):
                thread_ids.append(threading.current_thread().ident)
                # Both compiles must reach the barrier before either proceeds,
                # proving they run concurrently
                barrier.wait()
                (td / os.path.basename(target)).write_bytes(b"\x7fELF fake")
                return fake_subprocess_result()

            with mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic):
                backend.execute("build")

            # Verify they ran on different threads
            assert len(thread_ids) == 2
            assert thread_ids[0] != thread_ids[1]

    def test_parallel_1_still_works(self, monkeypatch):
        """parallel=1 should work correctly (single-threaded)."""
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

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "a.cpp").write_text("int a() {}")
            (td / "b.cpp").write_text("int b() {}")
            os.makedirs(td / "obj", exist_ok=True)

            compile_calls = []

            def fake_atomic(target, cmd):
                compile_calls.append(target)
                (td / os.path.basename(target)).write_bytes(b"\x7fELF fake")
                return fake_subprocess_result()

            with mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic):
                backend.execute("build")
                assert len(compile_calls) == 2


# ---------------------------------------------------------------------------
# Output file deletion detection
# ---------------------------------------------------------------------------


class TestOutputDeletion:
    def test_rebuilds_when_output_deleted(self, monkeypatch):
        """If output file is deleted but trace exists, must rebuild."""
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

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() {}")
            os.makedirs(td / "obj", exist_ok=True)
            # Note: foo.o does NOT exist (simulates deletion)

            source_hash = get_file_hash(str(td / "foo.cpp"))
            cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o"]

            # Pre-populate trace as if foo.o was previously built successfully
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash="hash_of_deleted_file",
                    input_hashes={"foo.cpp": source_hash},
                    command_hash=hash_command(cmd),
                ),
            )
            store.save()

            compile_calls = []

            def fake_atomic(target, cmd):
                compile_calls.append(target)
                (td / "foo.o").write_bytes(b"\x7fELF rebuilt")
                return fake_subprocess_result()

            with mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic):
                backend.execute("build")
                # Must rebuild because output file was deleted
                assert len(compile_calls) == 1


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestContentAddressableShortCircuit:
    def test_is_content_addressable(self):
        """Compile, link, and library rules are content-addressable."""
        compile_rule = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["g++"], rule_type="compile")
        link_rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++"], rule_type="link")
        static_rule = BuildRule(output="libfoo.a", inputs=["foo.o"], command=["ar"], rule_type="static_library")
        shared_rule = BuildRule(output="libfoo.so", inputs=["foo.o"], command=["g++"], rule_type="shared_library")
        phony_rule = BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony")
        assert _is_content_addressable(compile_rule) is True
        assert _is_content_addressable(link_rule) is True
        assert _is_content_addressable(static_rule) is True
        assert _is_content_addressable(shared_rule) is True
        assert _is_content_addressable(phony_rule) is False

    def test_compile_skipped_when_object_exists_no_traces(self, monkeypatch):
        """Object file exists, NO trace store populated. Compile is skipped
        via content-addressable short-circuit independently of traces."""
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
        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() {}")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            with mock.patch("compiletools.shake_backend.subprocess.run") as mock_run:
                backend.execute("build")
                mock_run.assert_not_called()

    def test_compile_returns_true_when_object_new(self, monkeypatch):
        """Object file does NOT exist, compile executes. _build returns True
        (signaling dependents should rebuild)."""
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

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() {}")
            # foo.o intentionally NOT created
            os.makedirs(td / "obj", exist_ok=True)

            traces = TraceStore(str(td / ".ct-traces.json"))
            done: set[str] = set()
            lock = threading.Lock()

            compile_calls = []

            def fake_atomic(target, cmd):
                compile_calls.append(target)
                (td / "foo.o").write_bytes(b"\x7fELF new object")
                return fake_subprocess_result()

            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as executor:
                with mock.patch.object(ShakeBackend, "_atomic_compile_no_lock", side_effect=fake_atomic):
                    changed = backend._build("foo.o", graph, traces, done, lock, executor)
                    assert changed is True
                    assert len(compile_calls) == 1

    def test_link_skipped_when_ca_target_exists(self, monkeypatch):
        """Link uses CA short-circuit: if the CA-named file exists, the link
        is skipped and the CA file is copied to the human-readable target."""
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")

            # Pre-create the CA target so the short-circuit fires
            ca = backend._ca_target(link_rule)
            with open(ca, "wb") as f:
                f.write(b"\x7fELF cached executable")

            with mock.patch("compiletools.shake_backend.subprocess.run") as mock_run:
                backend.execute("build")
                mock_run.assert_not_called()

            # Human-readable target should be a copy of the CA file
            assert (td / "foo").read_bytes() == b"\x7fELF cached executable"

    def test_link_rebuilds_when_ca_target_missing(self, monkeypatch):
        """No CA target → link executes, builds to CA target, copies to human target."""
        link_rule = BuildRule(
            output="foo",
            inputs=["foo.o"],
            command=["g++", "-o", "foo", "foo.o"],
            rule_type="link",
        )
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")

            ca = backend._ca_target(link_rule)

            mock_result = fake_subprocess_result()

            def fake_run(cmd, **kwargs):
                # The command should target the CA path, not the human-readable path
                assert ca in cmd
                with open(ca, "wb") as f:
                    f.write(b"\x7fELF new executable")
                return mock_result

            with mock.patch("compiletools.shake_backend.subprocess.run", side_effect=fake_run) as mock_run:
                backend.execute("build")
                mock_run.assert_called_once()

            # Both CA and human-readable targets should exist
            assert os.path.exists(ca)
            assert (td / "foo").read_bytes() == b"\x7fELF new executable"


# ---------------------------------------------------------------------------
# Content-addressable link/library short-circuit
# ---------------------------------------------------------------------------


class TestCALinkShortCircuit:
    def test_ca_target_deterministic(self, tmp_path):
        """Same rule produces the same CA target path."""
        rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        assert backend._ca_target(rule) == backend._ca_target(rule)

    def test_ca_target_differs_on_input_change(self, tmp_path):
        """Different inputs produce different CA target paths."""
        rule1 = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        rule2 = BuildRule(
            output="foo", inputs=["foo.o", "bar.o"], command=["g++", "-o", "foo", "foo.o", "bar.o"], rule_type="link"
        )
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        assert backend._ca_target(rule1) != backend._ca_target(rule2)

    def test_ca_target_differs_on_command_change(self, tmp_path):
        """Different commands (same inputs) produce different CA target paths."""
        rule1 = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        rule2 = BuildRule(
            output="foo", inputs=["foo.o"], command=["g++", "-O2", "-o", "foo", "foo.o"], rule_type="link"
        )
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        assert backend._ca_target(rule1) != backend._ca_target(rule2)

    def test_ca_target_preserves_extension(self, tmp_path):
        """CA target preserves the file extension for libraries."""
        rule = BuildRule(
            output="libfoo.a", inputs=["foo.o"], command=["ar", "-src", "libfoo.a", "foo.o"], rule_type="static_library"
        )
        args = mock.MagicMock()
        args.objdir = str(tmp_path)
        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = args
        ca = backend._ca_target(rule)
        assert ca.endswith(".a")
        assert "libfoo_" in ca

    def test_link_no_sig_files(self, monkeypatch):
        """CA link/library rules do not produce .ct-sig sidecar files."""
        link_rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")

            ca = backend._ca_target(link_rule)
            mock_result = fake_subprocess_result()

            def fake_run(cmd, **kwargs):
                with open(ca, "wb") as f:
                    f.write(b"\x7fELF new exe")
                return mock_result

            with mock.patch("compiletools.shake_backend.subprocess.run", side_effect=fake_run):
                backend.execute("build")

            assert not os.path.exists("foo.ct-sig")


# ---------------------------------------------------------------------------
# Atomic compile (TOCTOU prevention)
# ---------------------------------------------------------------------------


class TestAtomicCompile:
    def test_compile_uses_temp_file_and_rename(self, tmp_path, monkeypatch):
        """Verify _atomic_compile_no_lock writes to a temp file then renames,
        so the target file never exists in a partially-written state."""
        monkeypatch.chdir(tmp_path)
        target = str(tmp_path / "foo.o")
        observed_files = []

        def spy_run(cmd, **kwargs):
            # During compilation, the -o flag should point to a .tmp file
            if "-o" in cmd:
                output_idx = cmd.index("-o") + 1
                output_file = cmd[output_idx]
                observed_files.append(output_file)
                # Verify it's a temp file, not the final target
                assert output_file.endswith(".tmp"), f"Expected temp file, got {output_file}"
                assert output_file != target, "Should not compile directly to target"
                # Create the temp file to simulate successful compilation
                with open(output_file, "wb") as f:
                    f.write(b"\x7fELF fake object")
            return fake_subprocess_result()

        with mock.patch("compiletools.shake_backend.subprocess.run", side_effect=spy_run):
            ShakeBackend._atomic_compile_no_lock(target, ["g++", "-c", "foo.cpp"])

        # Verify temp file was used
        assert len(observed_files) == 1
        assert observed_files[0].startswith(target)
        assert observed_files[0].endswith(".tmp")
        # Verify final target exists (renamed from temp)
        assert os.path.exists(target)
        # Verify temp file was cleaned up
        assert not os.path.exists(observed_files[0])

    def test_compile_failure_cleans_up_temp_file(self, tmp_path, monkeypatch):
        """If compilation fails, temp file should be cleaned up and target not created."""
        monkeypatch.chdir(tmp_path)
        target = str(tmp_path / "foo.o")
        temp_files = []

        def failing_run(cmd, **kwargs):
            if "-o" in cmd:
                output_idx = cmd.index("-o") + 1
                temp_files.append(cmd[output_idx])
                # Create temp file then fail
                with open(cmd[output_idx], "wb") as f:
                    f.write(b"partial")
            return fake_subprocess_result(returncode=1, stderr="error: compilation failed")

        with mock.patch("compiletools.shake_backend.subprocess.run", side_effect=failing_run):
            with pytest.raises(subprocess.CalledProcessError):
                ShakeBackend._atomic_compile_no_lock(target, ["g++", "-c", "foo.cpp"])

        # Target should not exist
        assert not os.path.exists(target)
        # Temp file should be cleaned up
        assert len(temp_files) == 1
        assert not os.path.exists(temp_files[0])

    def test_atomic_compile_in_locking_module(self, tmp_path):
        """Verify the shared atomic_compile function in locking.py works correctly."""
        from compiletools.locking import FlockLock, atomic_compile

        target = str(tmp_path / "bar.o")
        lock_args = mock.MagicMock()
        lock_args.sleep_interval_flock_fallback = 0.01
        lock_args.verbose = 0
        lock = FlockLock(target, lock_args)

        observed_outputs = []

        def spy_run(cmd, **kwargs):
            if "-o" in cmd:
                output_idx = cmd.index("-o") + 1
                observed_outputs.append(cmd[output_idx])
                with open(cmd[output_idx], "wb") as f:
                    f.write(b"\x7fELF object")
            return fake_subprocess_result()

        with mock.patch("compiletools.locking.subprocess.run", side_effect=spy_run):
            atomic_compile(lock, target, ["g++", "-c", "bar.cpp"])

        assert len(observed_outputs) == 1
        # FlockLock has direct_compile=True, so compiler gets -o target directly
        assert observed_outputs[0] == target
