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

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.namer
import compiletools.testhelper as uth
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.makefile_backend import MakefileBackend
from compiletools.testhelper import ShakeBackendTestContext
from compiletools.trace_backend import (
    ShakeBackend,
    SlurmBackend,
    TraceEntry,
    TraceStore,
    _is_build_artifact,
    hash_command,
)


def _swf_writer(content: bytes = b"\x7fELF fake", returncode: int = 0):
    """Build a fake `compiletools.locking._run_with_signal_forwarding` that
    writes `content` to the rewritten output path in the cmd.

    Both `atomic_compile` (for non-direct_compile locks like _NullLock used
    when file_locking=False) and `atomic_link` rewrite the `-o` flag (or the
    archive arg for `ar`) to point at a `{target}.{pid}.{rand}.tmp` file
    before invoking `_run_with_signal_forwarding`. The fake writes there so
    the subsequent rename produces a real target file.
    """
    written: list[str] = []

    def fake(cmd):
        output = None
        if "-o" in cmd:
            i = cmd.index("-o")
            if i + 1 < len(cmd):
                output = cmd[i + 1]
        elif cmd and os.path.basename(cmd[0]) == "ar" and len(cmd) >= 3:
            output = cmd[2]
        if output is not None and returncode == 0:
            with open(output, "wb") as f:
                f.write(content)
            written.append(output)
        return subprocess.CompletedProcess(cmd, returncode, None, None)

    return fake


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

    def test_per_entry_corruption_keeps_valid_entries(self, tmp_path, caplog):
        path = str(tmp_path / ".ct-traces.json")
        good_entry = {
            "output_hash": "ok",
            "input_hashes": {"a.cpp": "h"},
            "command_hash": "cmd",
        }
        bad_entry = {"output_hash": "missing_other_fields"}
        with open(path, "w") as f:
            json.dump(
                {"version": 1, "traces": {"good.o": good_entry, "bad.o": bad_entry}},
                f,
            )
        with caplog.at_level("WARNING", logger="compiletools.trace_backend"):
            store = TraceStore(path)
        assert store.get("good.o") is not None
        assert store.get("bad.o") is None
        assert any("bad.o" in rec.message or "trace entry" in rec.message.lower() for rec in caplog.records)

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

            source_hash = get_file_hash(str(td / "foo.cpp"), backend.context)
            obj_hash = get_file_hash(str(td / "foo.o"), backend.context)
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
            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
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

            # Link rule routes through atomic_link → _run_with_signal_forwarding
            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(b"\x7fELF rebuilt"),
            ) as mock_swf:
                backend.execute("build")
                assert mock_swf.call_count == 1

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

            source_hash = get_file_hash(str(td / "foo.cpp"), backend.context)
            obj_hash = get_file_hash(str(td / "foo.o"), backend.context)

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

            # Copy rule routes through atomic_link → _run_with_signal_forwarding
            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(),
            ) as mock_swf:
                backend.execute("build")
                assert mock_swf.call_count == 1

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

            source_hash = get_file_hash(str(td / "foo.cpp"), backend.context)
            cmd = ["cp", "foo.cpp", "foo.o"]

            # Trace only knows about ONE input
            trace_path = str(td / ".ct-traces.json")
            store = TraceStore(trace_path)
            store.put(
                "foo.o",
                TraceEntry(
                    output_hash=get_file_hash(str(td / "foo.o"), backend.context),
                    input_hashes={"foo.cpp": source_hash},
                    command_hash=hash_command(cmd),
                ),
            )
            store.save()

            # Copy rule routes through atomic_link → _run_with_signal_forwarding;
            # _run_with_signal_forwarding is the boundary that's specific to the
            # build subprocess (git_utils uses check_output, not this helper).
            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(),
            ) as mock_swf:
                backend.execute("build")
                build_calls = [c for c in mock_swf.call_args_list if c.args[0] == cmd]
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

            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
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

            # Both compile and link route through _run_with_signal_forwarding
            # (via atomic_compile and atomic_link respectively).
            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(b"\x7fELF NEW"),
            ) as mock_swf:
                backend.execute("build")
                # 1 compile + 1 link = 2 build subprocess calls
                assert mock_swf.call_count == 2


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

            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(returncode=1),
            ):
                with pytest.raises(subprocess.CalledProcessError):
                    backend.execute("build")


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


class TestTracePersistenceOnFailure:
    """Traces for rules that completed must be saved even when a later rule
    raises an exception."""

    def test_traces_saved_on_failure(self, monkeypatch):
        # Chain ok.txt -> bad.txt so ok.txt must complete and have its trace
        # recorded before bad.txt runs and raises.
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="ok.txt",
                inputs=["trace_save_src.txt"],
                command=["cp", "trace_save_src.txt", "ok.txt"],
                rule_type="copy",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bad.txt",
                inputs=["ok.txt"],
                command=["cp", "ok.txt", "bad.txt"],
                rule_type="copy",
            )
        )
        graph.add_rule(BuildRule(output="build", inputs=["bad.txt"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "trace_save_src.txt").write_text("data")

            def fake(cmd):
                if any("bad.txt" in tok for tok in cmd):
                    return subprocess.CompletedProcess(cmd, 1, None, None)
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                elif len(cmd) >= 3:
                    out = cmd[-1]
                else:
                    out = None
                if out is not None:
                    with open(out, "wb") as f:
                        f.write(b"data")
                return subprocess.CompletedProcess(cmd, 0, None, None)

            with mock.patch("compiletools.locking._run_with_signal_forwarding", side_effect=fake):
                with pytest.raises(subprocess.CalledProcessError):
                    backend.execute("build")

            trace_path = Path(backend.args.objdir) / ".ct-traces.json"
            assert trace_path.exists()
            data = json.loads(trace_path.read_text())
            traces = data.get("traces", {})
            assert "ok.txt" in traces


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

            def fake_swf(cmd):
                thread_ids.append(threading.current_thread().ident)
                # Both compiles must reach the barrier before either proceeds,
                # proving they run concurrently
                barrier.wait()
                # atomic_compile rewrote -o to a temp path; honour it
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                    with open(out, "wb") as f:
                        f.write(b"\x7fELF fake")
                return subprocess.CompletedProcess(cmd, 0, None, None)

            with mock.patch("compiletools.locking._run_with_signal_forwarding", side_effect=fake_swf):
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

            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(),
            ) as mock_swf:
                backend.execute("build")
                assert mock_swf.call_count == 2


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

            source_hash = get_file_hash(str(td / "foo.cpp"), backend.context)
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

            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(b"\x7fELF rebuilt"),
            ) as mock_swf:
                backend.execute("build")
                # Must rebuild because output file was deleted
                assert mock_swf.call_count == 1


# ---------------------------------------------------------------------------
# Content-addressable short-circuit
# ---------------------------------------------------------------------------


class TestContentAddressableShortCircuit:
    def test_is_build_artifact(self):
        """Compile, link, and library rules are content-addressable."""
        compile_rule = BuildRule(output="foo.o", inputs=["foo.cpp"], command=["g++"], rule_type="compile")
        link_rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++"], rule_type="link")
        static_rule = BuildRule(output="libfoo.a", inputs=["foo.o"], command=["ar"], rule_type="static_library")
        shared_rule = BuildRule(output="libfoo.so", inputs=["foo.o"], command=["g++"], rule_type="shared_library")
        phony_rule = BuildRule(output="build", inputs=["foo"], command=None, rule_type="phony")
        assert _is_build_artifact(compile_rule) is True
        assert _is_build_artifact(link_rule) is True
        assert _is_build_artifact(static_rule) is True
        assert _is_build_artifact(shared_rule) is True
        assert _is_build_artifact(phony_rule) is False

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

            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
                backend.execute("build")
                mock_run.assert_not_called()

    def test_compile_returns_true_when_object_new(self, monkeypatch):
        """Object file does NOT exist, compile executes. _build_async returns True
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

            import asyncio

            memo: dict[str, asyncio.Task[bool]] = {}
            sem = asyncio.Semaphore(1)
            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(b"\x7fELF new object"),
            ) as mock_swf:
                changed = asyncio.run(backend._build_async("foo.o", graph, traces, memo, sem))
                assert changed is True
                assert mock_swf.call_count == 1

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

            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
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

            def fake_swf(cmd):
                # atomic_link rewrites -o from `ca` to a temp path; the
                # original `ca` should NOT appear in the command (the
                # tempfile takes its place).  Verify the temp path is in
                # the same directory and ends with .tmp.
                assert "-o" in cmd
                out = cmd[cmd.index("-o") + 1]
                assert out != ca
                assert out.startswith(ca + ".") and out.endswith(".tmp")
                with open(out, "wb") as f:
                    f.write(b"\x7fELF new executable")
                return subprocess.CompletedProcess(cmd, 0, None, None)

            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=fake_swf,
            ) as mock_swf:
                backend.execute("build")
                assert mock_swf.call_count == 1

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

            with mock.patch(
                "compiletools.locking._run_with_signal_forwarding",
                side_effect=_swf_writer(b"\x7fELF new exe"),
            ):
                backend.execute("build")

            assert not os.path.exists("foo.ct-sig")


# ---------------------------------------------------------------------------
# Atomic compile (TOCTOU prevention)
# ---------------------------------------------------------------------------


class TestAtomicCompile:
    def test_atomic_compile_in_locking_module(self, tmp_path):
        """Verify the shared atomic_compile function in locking.py works correctly."""
        from compiletools.locking import FlockLock, atomic_compile

        target = str(tmp_path / "bar.o")
        lock_args = mock.MagicMock()
        lock_args.sleep_interval_flock_fallback = 0.01
        lock_args.verbose = 0
        lock = FlockLock(target, lock_args)

        observed_outputs = []

        def spy_run(cmd):
            if "-o" in cmd:
                output_idx = cmd.index("-o") + 1
                observed_outputs.append(cmd[output_idx])
                with open(cmd[output_idx], "wb") as f:
                    f.write(b"\x7fELF object")
            return subprocess.CompletedProcess(cmd, 0, None, None)

        # atomic_compile delegates to _run_with_signal_forwarding (the new
        # boundary that wraps Popen + signal forwarding); patch that.
        with mock.patch("compiletools.locking._run_with_signal_forwarding", side_effect=spy_run):
            atomic_compile(lock, target, ["g++", "-c", "bar.cpp"])

        # atomic_compile always routes through a temp file then renames, so
        # peer linkers reading the .o never see partial bytes (the lock
        # protects writers from each other but not readers).
        assert len(observed_outputs) == 1
        assert observed_outputs[0] != target
        assert observed_outputs[0].startswith(target + ".") and ".tmp" in observed_outputs[0]
        assert os.path.exists(target)


class TestCompilerIdentityInTrace:
    """_verify must invalidate when the compiler binary itself changes
    (e.g. in-place upgrade) even though the argv is byte-identical."""

    def test_verify_fails_when_compiler_identity_changes(self, tmp_path, monkeypatch):
        from compiletools.trace_backend import _make_trace_entry

        monkeypatch.chdir(tmp_path)
        src = tmp_path / "compiler_identity_src.cpp"
        obj = tmp_path / "compiler_identity_src.o"
        src.write_text("int main() {}")
        obj.write_bytes(b"\x7fELF")

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = mock.MagicMock()
        backend.args.objdir = str(tmp_path)
        from compiletools.build_context import BuildContext

        backend.context = BuildContext()

        rule = BuildRule(
            output=str(obj),
            inputs=[str(src)],
            command=["my-fake-compiler", "-c", str(src), "-o", str(obj)],
            rule_type="copy",
        )

        with mock.patch("compiletools.trace_backend._compiler_identity", return_value="compiler-v1"):
            entry_v1 = _make_trace_entry(rule, backend.context)

        with mock.patch("compiletools.trace_backend._compiler_identity", return_value="compiler-v2"):
            entry_v2 = _make_trace_entry(rule, backend.context)
            assert backend._verify(rule, entry_v1) is False

        # Identity is folded into the trace's command_hash, not just the verify path.
        assert entry_v1.command_hash != entry_v2.command_hash


class TestVerifyCanonicalization:
    """_verify and _make_trace_entry must canonicalize input paths so that
    cosmetic differences (./prefix, redundant slashes) do not spuriously
    invalidate traces."""

    def test_verify_ignores_prefix_differences(self, tmp_path, monkeypatch):
        from compiletools.trace_backend import _make_trace_entry

        monkeypatch.chdir(tmp_path)
        src = tmp_path / "foo.cpp"
        obj = tmp_path / "foo.o"
        src.write_text("int main() {}")
        obj.write_bytes(b"\x7fELF")

        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = mock.MagicMock()
        backend.args.objdir = str(tmp_path)
        from compiletools.build_context import BuildContext

        backend.context = BuildContext()

        # Same file referenced via canonical and ./-prefixed paths.
        rule_plain = BuildRule(
            output=str(obj),
            inputs=[str(src)],
            command=["cp", str(src), str(obj)],
            rule_type="copy",
        )
        rule_dotslash = BuildRule(
            output=str(obj),
            inputs=["./" + os.path.relpath(str(src))],
            command=["cp", str(src), str(obj)],
            rule_type="copy",
        )
        entry = _make_trace_entry(rule_plain, backend.context)
        assert backend._verify(rule_dotslash, entry) is True


class TestCompileOutputStripping:
    """The compile branch must remove '-o target' wherever it appears,
    not assume it's the last two tokens."""

    def test_compile_handles_o_flag_not_at_end(self, monkeypatch):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "foo.o", "-DEXTRA=1"],
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

            seen = []

            def fake(cmd):
                seen.append(list(cmd))
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                    with open(out, "wb") as f:
                        f.write(b"\x7fELF")
                return subprocess.CompletedProcess(cmd, 0, None, None)

            with mock.patch("compiletools.locking._run_with_signal_forwarding", side_effect=fake):
                backend.execute("build")

            assert len(seen) == 1
            assert "-DEXTRA=1" in seen[0]
            o_idx = seen[0].index("-o")
            assert seen[0][o_idx + 1].endswith(".tmp")


class TestVerifyAssertions:
    """Direct unit tests for ShakeBackend._verify."""

    def test_verify_asserts_when_command_is_none(self, tmp_path):
        backend = ShakeBackend.__new__(ShakeBackend)
        backend.args = mock.MagicMock()
        backend.args.objdir = str(tmp_path)
        backend.context = mock.MagicMock()
        rule = BuildRule(output="x", inputs=[], command=None, rule_type="phony")
        trace = TraceEntry(output_hash="h", input_hashes={}, command_hash="c")
        with pytest.raises(AssertionError):
            backend._verify(rule, trace)


class TestAtomicLinkRouting:
    """Verify Shake routes link/library/copy rules through locking.atomic_link.

    Regression for review issue C2: ShakeBackend used to wrap link rules in
    `with FileLock(...)` + raw subprocess.run, leaving an orphaned linker
    writing to the now-unlocked target if the parent caught a signal.
    """

    def test_link_rule_routes_through_atomic_link(self, monkeypatch):
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

            with mock.patch("compiletools.trace_backend.atomic_link") as mock_link:
                mock_link.side_effect = lambda lock, target, cmd: open(target, "wb").close() or 0
                backend.execute("build")
                assert mock_link.call_count == 1
                call_args = mock_link.call_args
                # atomic_link(lock, target=ca, cmd=ca_cmd)
                assert call_args.args[1] == backend._ca_target(link_rule)

    def test_copy_rule_routes_through_atomic_link(self, monkeypatch):
        """Non-build-artifact rule types (e.g. 'copy') must also go through
        atomic_link to inherit signal forwarding and temp+rename."""
        copy_rule = BuildRule(
            output="foo.txt",
            inputs=["src.txt"],
            command=["cp", "src.txt", "foo.txt"],
            rule_type="copy",
        )
        graph = BuildGraph()
        graph.add_rule(copy_rule)
        graph.add_rule(BuildRule(output="build", inputs=["foo.txt"], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "src.txt").write_text("data")

            with mock.patch("compiletools.trace_backend.atomic_link") as mock_link:
                mock_link.side_effect = lambda lock, target, cmd: open(target, "wb").close() or 0
                backend.execute("build")
                assert mock_link.call_count == 1
                # The catch-all branch passes target=rule.output, not a CA path
                assert mock_link.call_args.args[1] == "foo.txt"

    def test_link_starts_new_session_for_signal_forwarding(self, monkeypatch):
        """End-to-end signal-forwarding regression: link must use Popen with
        start_new_session=True so SIGINT/SIGTERM can be forwarded to the
        linker's process group rather than orphaning it."""
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

            popen_calls = []
            real_popen = subprocess.Popen

            class FakePopen:
                def __init__(self, *args, **kwargs):
                    popen_calls.append((args, kwargs))
                    # Write the rewritten temp output then "exit 0"
                    cmd = args[0]
                    if "-o" in cmd:
                        with open(cmd[cmd.index("-o") + 1], "wb") as f:
                            f.write(b"\x7fELF link")
                    self.pid = os.getpid()
                    self.returncode = 0

                def wait(self, timeout=None):
                    return 0

                def poll(self):
                    return 0

            with mock.patch("compiletools.locking.subprocess.Popen", FakePopen):
                backend.execute("build")

            assert len(popen_calls) == 1
            kwargs = popen_calls[0][1]
            assert kwargs.get("start_new_session") is True
            del real_popen  # silence unused warning


@uth.requires_functional_compiler
def test_quoted_define_with_space_compiles_end_to_end(tmp_path, monkeypatch):
    """A //#CXXFLAGS=-DGREETING="Hello World" magic flag must reach the
    compiler as one argv element. End-to-end: build with the shake backend
    and run the resulting executable to verify std::strlen(GREETING) == 11.
    """
    src_path = tmp_path / "greeting.cpp"
    src_path.write_text(
        "//#CXXFLAGS=-DGREETING='\"Hello World\"'\n"
        "#include <cstring>\n"
        "int main() { return std::strlen(GREETING) == 11 ? 0 : 1; }\n"
    )

    objdir = tmp_path / "obj"
    bindir = tmp_path / "bin"
    bindir.mkdir()

    argv = [
        "--include",
        str(tmp_path),
        "--objdir",
        str(objdir),
        "--bindir",
        str(bindir),
        str(src_path),
    ]

    with uth.ParserContext():
        cap = compiletools.apptools.create_parser("Shake greeting test", argv=argv)
        compiletools.apptools.add_target_arguments_ex(cap)
        compiletools.apptools.add_link_arguments(cap)
        compiletools.namer.Namer.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        MakefileBackend.add_arguments(cap)
        SlurmBackend.add_arguments(cap)

        ctx = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        headerdeps = compiletools.headerdeps.create(args, context=ctx)
        magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

        BackendClass = get_backend_class("shake")
        backend = BackendClass(args=args, hunter=hunter, context=ctx)
        graph = backend.build_graph()
        backend.generate(graph)
        backend.execute("build")

    exe_path = bindir / "greeting"
    if not exe_path.exists():
        candidates = []
        for dirpath, _dirs, files in os.walk(str(tmp_path)):
            for f in files:
                full = os.path.join(dirpath, f)
                if os.access(full, os.X_OK) and f == "greeting":
                    candidates.append(full)
        assert candidates, f"greeting executable not found under {tmp_path}"
        exe_path = Path(candidates[0])

    result = subprocess.run([str(exe_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, (
        f"greeting exe failed (rc={result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
    )
