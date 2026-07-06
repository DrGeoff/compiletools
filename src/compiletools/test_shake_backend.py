"""Unit tests for the Shake build backend (no compiler required)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.apptools import _GITROOT_SENTINEL
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.global_hash_registry import get_file_hash
from compiletools.locking import FlockLock, atomic_compile
from compiletools.priority_gate import PriorityGate
from compiletools.testhelper import ShakeBackendTestContext
from compiletools.trace_backend import (
    ShakeBackend,
    TraceEntry,
    TraceStore,
    _is_build_artifact,
    _make_trace_entry,
    hash_command,
)


def _make_bare_shake_backend(tmp_path, *, cas_subdir=None, context=None):
    """Build a bare ShakeBackend (bypassing __init__) wired with MagicMock
    args (cas_objdir = tmp_path[/cas_subdir]) and either a real BuildContext
    (default) or the caller's supplied context. Used by tests that exercise
    pure methods like `_verify` / `_make_trace_entry` without needing
    full backend init."""
    backend = ShakeBackend.__new__(ShakeBackend)
    backend.args = mock.MagicMock()
    cas_path = tmp_path / cas_subdir if cas_subdir else tmp_path
    backend.args.cas_objdir = str(cas_path)
    backend.context = context if context is not None else BuildContext()
    return backend


def _child_writer(content: bytes = b"\x7fELF fake", returncode: int = 0):
    """Build a fake for ``compiletools.locking._run_child_async`` — the async
    dispatch boundary the shake backend routes every compile/link through — that
    writes ``content`` to the rewritten output path in the cmd and returns the
    integer returncode.

    Both ``atomic_compile_async`` (for non-direct_compile locks like _NullLock
    used when file_locking=False) and ``atomic_link_async`` rewrite the ``-o``
    flag (or the archive arg for ``ar``) to point at a ``{target}.{pid}.{rand}.tmp``
    file before invoking ``_run_child_async``. The fake writes there so the
    subsequent rename produces a real target file.

    ``_run_child_async`` is a coroutine function, so ``mock.patch`` auto-wraps
    it in an ``AsyncMock``; a plain (sync) side_effect whose return value is the
    int is awaited correctly. ``call_count`` / ``call_args`` semantics are
    unchanged (``args[0]`` is still the command list)."""
    written: list[str] = []

    def fake(cmd, *args, **kwargs):
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
        return returncode

    return fake


# ---------------------------------------------------------------------------
# M0: queue-wait attribution
# ---------------------------------------------------------------------------


def test_execute_rule_records_queue_wait(tmp_path, monkeypatch):
    """queued_at threaded into _execute_rule surfaces as metadata.queue_wait_s."""
    import time

    from compiletools.build_timer import BuildTimer

    backend = _make_bare_shake_backend(tmp_path)
    timer = BuildTimer(enabled=True, backend="shake")
    backend.context.timer = timer
    rule = BuildRule(
        output=str(tmp_path / "x.o"),
        inputs=["x.cpp"],
        command=["true"],
        rule_type="compile",
    )
    monkeypatch.setattr(
        "compiletools.trace_backend.execute_compile_rule",
        lambda *a, **k: False,
    )
    queued = time.monotonic() - 0.05
    with timer.phase("build_execution"):
        backend._execute_rule(rule, rule.output, ["true"], queued_at=queued)
    rules = [e for e in timer._collect_rules() if e.category == "compile"]
    assert rules, "expected a recorded compile rule"
    assert "queue_wait_s" in rules[0].metadata
    assert rules[0].metadata["queue_wait_s"] >= 0.0


def test_execute_rule_no_queue_wait_when_unset(tmp_path, monkeypatch):
    """Omitting queued_at leaves queue_wait_s absent (back-compat)."""
    from compiletools.build_timer import BuildTimer

    backend = _make_bare_shake_backend(tmp_path)
    timer = BuildTimer(enabled=True, backend="shake")
    backend.context.timer = timer
    rule = BuildRule(
        output=str(tmp_path / "x.o"),
        inputs=["x.cpp"],
        command=["true"],
        rule_type="compile",
    )
    monkeypatch.setattr(
        "compiletools.trace_backend.execute_compile_rule",
        lambda *a, **k: False,
    )
    with timer.phase("build_execution"):
        backend._execute_rule(rule, rule.output, ["true"])
    rules = [e for e in timer._collect_rules() if e.category == "compile"]
    assert rules
    assert "queue_wait_s" not in rules[0].metadata


# ---------------------------------------------------------------------------
# M1: critical-path scheduling wiring
# ---------------------------------------------------------------------------


def test_execute_writes_cost_sidecar(tmp_path, monkeypatch):
    """A full execute() run folds observed elapsed times into .ct-rule-costs.json."""
    from compiletools import rule_cost

    backend = _make_bare_shake_backend(tmp_path)
    backend.args.parallel = 2
    backend.args.use_mtime = False
    backend.args.verbose = 0
    graph = BuildGraph()
    obj = str(tmp_path / "a.o")
    graph.add_rule(BuildRule(output=obj, inputs=["a.cpp"], command=["true", "-o", obj], rule_type="compile"))
    backend._graph = graph

    # execute_compile_rule_async mocked so no real compiler runs; returns False
    # (ran, no cache hit). The COMPILE rule is content-addressable so _do_build
    # returns True without needing the file to exist. Production dispatch is the
    # async twin, so the async helper is the one to stub.
    async def _fake_compile(*a, **k):
        return False

    monkeypatch.setattr("compiletools.trace_backend.execute_compile_rule_async", _fake_compile)

    backend.execute(obj)

    cost_path = os.path.join(backend.args.cas_objdir, rule_cost.COST_FILE)
    assert os.path.exists(cost_path)
    hist = rule_cost.load_cost_history(cost_path)
    rule = graph.get_rule(obj)
    assert rule is not None
    assert rule_cost.cost_key(rule) in hist


def test_execute_prefers_high_critical_time_rule(tmp_path, monkeypatch):
    """With one slot, the header_unit long pole executes before cheap compiles."""
    backend = _make_bare_shake_backend(tmp_path)
    backend.args.parallel = 1  # single slot forces priority ordering
    backend.args.use_mtime = False
    backend.args.verbose = 0

    def src(name):
        p = tmp_path / name
        p.write_text("")  # real file so global_hash_registry can hash inputs
        return str(p)

    graph = BuildGraph()
    pole = str(tmp_path / "pole.pcm")
    pole_src = src("pole.hpp")
    compiles = [str(tmp_path / f"c{i}.o") for i in range(4)]
    csrcs = [src(f"c{i}.cpp") for i in range(4)]
    # A phony 'all' that depends on a cheap compile FIRST (grabs the immediate
    # free slot) then the pole then the rest, so ordering is decided by the gate.
    order_inputs = [compiles[0], pole] + compiles[1:]
    graph.add_rule(BuildRule(output="all", inputs=order_inputs, command=None, rule_type="phony"))
    graph.add_rule(BuildRule(output=pole, inputs=[pole_src], command=["true"], rule_type="header_unit"))
    for i, c in enumerate(compiles):
        graph.add_rule(BuildRule(output=c, inputs=[csrcs[i]], command=["true"], rule_type="compile"))
    backend._graph = graph

    executed: list[tuple[str, str]] = []

    async def spy(self, rule, target, flat_cmd, queued_at=None):
        executed.append((rule.rule_type, target))
        with open(target, "w"):  # materialize output so post-execute checks pass
            pass

    monkeypatch.setattr(ShakeBackend, "_execute_rule_async", spy)

    backend.execute("all")

    # First dispatched grabs the free slot; the pole must be dispatched next.
    assert executed[0][1] == compiles[0], executed
    assert executed[1] == ("header_unit", pole), executed


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

    def test_save_creates_group_readable_store_despite_restrictive_umask(self, tmp_path):
        """.ct-traces.json lives in a shared CAS pool cell; a first creator with
        umask 077 must not lock peers out of the trace history (mirrors
        locking.py's explicit-0666 convention)."""
        import stat

        path = str(tmp_path / ".ct-traces.json")
        store = TraceStore(path)
        store.put("a.o", TraceEntry(output_hash="x", input_hashes={}, command_hash="y"))
        saved = os.umask(0o077)
        try:
            store.save()
        finally:
            os.umask(saved)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o666

        # And a rewrite repairs a restrictive mode left by a pre-fix creator.
        os.chmod(path, 0o600)
        store.save()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o666

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
        """If an input file changed, rebuild (uses copy rule to test trace
        verification, since compile and link rules both bypass traces via
        content-addressable short-circuit on output existence)."""
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
            (td / "foo.cpp").write_text("int main() { return 1; }")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            cmd = ["cp", "foo.cpp", "foo.o"]

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

            # Copy rule routes through atomic_link → _run_child_async (async path)
            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(b"\x7fELF rebuilt"),
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

            # Copy rule routes through atomic_link → _run_child_async (async path)
            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(),
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

            # Copy rule routes through atomic_link → _run_child_async (async path);
            # _run_child_async is the boundary that's specific to the build
            # subprocess (git_utils uses check_output, not this helper).
            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(),
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
        Link rule's output (a cas-exe path under the new design) exists →
        link also skipped. No subprocess calls."""
        # Link rule's output is the cas-exe path — that's what production
        # _create_link_rule returns now (Namer.cas_exe_pathname-derived).
        link_rule = BuildRule(
            output="cas-exe/aa/foo_abc.exe",
            inputs=["foo.o"],
            command=["g++", "-o", "cas-exe/aa/foo_abc.exe", "foo.o"],
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
        graph.add_rule(
            BuildRule(
                output="build",
                inputs=["cas-exe/aa/foo_abc.exe"],
                command=None,
                rule_type="phony",
            )
        )

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.cpp").write_text("int main() { return 0; }")
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "obj", exist_ok=True)

            # Pre-create the cas-exe path so the existence-only short-circuit
            # fires for the link rule (matches production behavior — the link
            # output IS the cas-exe path).
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
            (td / "cas-exe" / "aa" / "foo_abc.exe").write_bytes(b"\x7fELF cached executable")

            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
                backend.execute("build")
                # Compile skipped (foo.o exists), link skipped (cas-exe exists).
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

            # Both compile and link route through _run_child_async (async path)
            # (via atomic_compile_async and atomic_link_async respectively).
            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(b"\x7fELF NEW"),
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
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(returncode=1),
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

            def fake(cmd, *args, **kwargs):
                if any("bad.txt" in tok for tok in cmd):
                    return 1
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                elif len(cmd) >= 3:
                    out = cmd[-1]
                else:
                    out = None
                if out is not None:
                    with open(out, "wb") as f:
                        f.write(b"data")
                return 0

            with mock.patch("compiletools.locking._run_child_async", side_effect=fake):
                with pytest.raises(subprocess.CalledProcessError):
                    backend.execute("build")

            trace_path = Path(backend.args.cas_objdir) / ".ct-traces.json"
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

            # Concurrency proof for the async model: both compiles must be
            # in-flight on the event loop simultaneously. Each fake awaits a
            # yield point (asyncio.sleep) while a shared counter tracks the peak
            # number of overlapping executions; a serial scheduler would peak at
            # 1. (The old thread-id assertion no longer applies — the async
            # dispatch runs on a single loop thread, not a thread pool.)
            in_flight = 0
            max_in_flight = 0

            async def fake_child(cmd, *args, **kwargs):
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.05)  # yield so the peer can also enter
                # atomic_compile rewrote -o to a temp path; honour it
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                    with open(out, "wb") as f:
                        f.write(b"\x7fELF fake")
                in_flight -= 1
                return 0

            with mock.patch("compiletools.locking._run_child_async", new=fake_child):
                backend.execute("build")

            # Both compiles overlapped → true concurrent dispatch.
            assert max_in_flight == 2

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
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(),
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
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(b"\x7fELF rebuilt"),
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

            memo: dict[str, asyncio.Task[bool]] = {}
            gate = PriorityGate(1)
            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(b"\x7fELF new object"),
            ) as mock_swf:
                changed = asyncio.run(backend._build_async("foo.o", graph, traces, memo, gate, {}))
                assert changed is True
                assert mock_swf.call_count == 1

    def test_link_skipped_when_cas_exe_exists(self, monkeypatch):
        """Link rule's output is the cas-exe path. If it already exists,
        the existence-only short-circuit fires (matches the compile rule
        path). The publish-as-symlink rule is responsible for materialising
        the user-facing bin/<name> from this cached file; in this minimal
        graph there is no symlink rule, so the test verifies only that
        the link itself is skipped."""
        link_rule = BuildRule(
            output="cas-exe/aa/foo_abc.exe",
            inputs=["foo.o"],
            command=["g++", "-o", "cas-exe/aa/foo_abc.exe", "foo.o"],
            rule_type="link",
        )
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(
            BuildRule(
                output="build",
                inputs=["cas-exe/aa/foo_abc.exe"],
                command=None,
                rule_type="phony",
            )
        )

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")

            # Pre-create the cas-exe path so the existence short-circuit fires.
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
            (td / "cas-exe" / "aa" / "foo_abc.exe").write_bytes(b"\x7fELF cached executable")

            with mock.patch("compiletools.trace_backend.subprocess.run") as mock_run:
                backend.execute("build")
                mock_run.assert_not_called()

            # Cas-exe path remains untouched.
            assert (td / "cas-exe" / "aa" / "foo_abc.exe").read_bytes() == b"\x7fELF cached executable"

    def test_link_rebuilds_when_cas_exe_missing(self, monkeypatch):
        """Cas-exe path missing → link executes, atomic_link writes to a
        temp path next to the cas-exe and renames into place. No
        intermediate CA layer (cas-exe IS the CA layer)."""
        cas_exe = "cas-exe/aa/foo_abc.exe"
        link_rule = BuildRule(
            output=cas_exe,
            inputs=["foo.o"],
            command=["g++", "-o", cas_exe, "foo.o"],
            rule_type="link",
        )
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=[cas_exe], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)

            def fake_swf(cmd, *args, **kwargs):
                # atomic_link rewrites -o from rule.output to a temp path
                # next to it; verify the temp path is sibling to cas_exe.
                assert "-o" in cmd
                out = cmd[cmd.index("-o") + 1]
                assert out != cas_exe
                assert out.startswith(cas_exe + ".") and out.endswith(".tmp")
                with open(out, "wb") as f:
                    f.write(b"\x7fELF new executable")
                return 0

            with mock.patch(
                "compiletools.locking._run_child_async",
                side_effect=fake_swf,
            ) as mock_swf:
                backend.execute("build")
                assert mock_swf.call_count == 1

            # Cas-exe path now exists with the new content.
            assert (td / cas_exe).read_bytes() == b"\x7fELF new executable"


# ---------------------------------------------------------------------------
# SYMLINK publish short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "link"), reason="os.link unavailable (e.g. Termux/Android Python)")
class TestSymlinkPublishShortCircuit:
    """A SYMLINK rule republishes a CAS artefact at a user-facing path via
    ct-cas-publish (hardlink by default, symlink fallback on EXDEV). When
    the user-facing target already resolves to the cas input on disk, the
    rule must short-circuit without hashing either file -- otherwise no-op
    builds pay an O(file-size) SHA-1 per published target."""

    @staticmethod
    def _make_symlink_graph(cas_path: str, user_path: str) -> BuildGraph:
        symlink_rule = BuildRule(
            output=user_path,
            inputs=[cas_path],
            command=["ct-cas-publish", "--cas-path", cas_path, "--user-path", user_path],
            rule_type="symlink",
        )
        graph = BuildGraph()
        graph.add_rule(symlink_rule)
        graph.add_rule(BuildRule(output="build", inputs=[user_path], command=None, rule_type="phony"))
        return graph

    def test_skipped_when_hardlinked_to_cas_input(self, monkeypatch):
        """Default publish wiring: target is a hardlink (same inode) of the
        cas input. samefile returns True, rule short-circuits without
        invoking ct-cas-publish or hashing either file."""
        cas_path = "cas-exe/aa/foo_abc.exe"
        user_path = "bin/foo"
        graph = self._make_symlink_graph(cas_path, user_path)

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
            os.makedirs(td / "bin", exist_ok=True)
            (td / cas_path).write_bytes(b"\x7fELF cached executable")
            os.link(td / cas_path, td / user_path)

            with (
                mock.patch("compiletools.trace_backend.subprocess.run") as mock_run,
                mock.patch("compiletools.trace_backend.get_file_hash") as mock_hash,
            ):
                backend.execute("build")
                mock_run.assert_not_called()
                mock_hash.assert_not_called()

    def test_skipped_when_symlinked_to_cas_input(self, monkeypatch):
        """EXDEV fallback wiring: target is a symlink to the cas input.
        samefile follows the symlink and returns True; rule short-circuits."""
        cas_path = "cas-exe/aa/foo_abc.exe"
        user_path = "bin/foo"
        graph = self._make_symlink_graph(cas_path, user_path)

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
            os.makedirs(td / "bin", exist_ok=True)
            (td / cas_path).write_bytes(b"\x7fELF cached executable")
            os.symlink(td / cas_path, td / user_path)

            with (
                mock.patch("compiletools.trace_backend.subprocess.run") as mock_run,
                mock.patch("compiletools.trace_backend.get_file_hash") as mock_hash,
            ):
                backend.execute("build")
                mock_run.assert_not_called()
                mock_hash.assert_not_called()

    def test_runs_when_target_missing(self, monkeypatch, tmp_path):
        """First publish: cas input exists but user-facing target does not.
        samefile raises OSError on the missing target; rule falls through
        to _execute_rule. Bypass the lock-wrapper subprocess chain by
        monkey-patching _execute_rule."""
        td = tmp_path
        os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
        os.makedirs(td / "bin", exist_ok=True)
        cas_path = str(td / "cas-exe" / "aa" / "foo_abc.exe")
        user_path = str(td / "bin" / "foo")
        (td / "cas-exe" / "aa" / "foo_abc.exe").write_bytes(b"\x7fELF cached executable")
        # user_path intentionally NOT created
        graph = self._make_symlink_graph(cas_path, user_path)

        with ShakeBackendTestContext(graph) as (backend, _):
            monkeypatch.chdir(tmp_path)
            executed: list[str] = []

            async def fake_execute(rule, target, flat_cmd, queued_at=None):
                # Mirror ct-cas-publish: hardlink cas_path to user_path.
                executed.append(target)
                os.link(cas_path, user_path)

            monkeypatch.setattr(backend, "_execute_rule_async", fake_execute)
            traces = TraceStore(str(td / ".ct-traces.json"))
            memo: dict[str, asyncio.Task[bool]] = {}
            gate = PriorityGate(1)
            asyncio.run(backend._build_async(user_path, graph, traces, memo, gate, {}))
            assert executed == [user_path]
            assert os.path.samefile(cas_path, user_path)

    def test_runs_when_target_points_to_stale_inode(self, monkeypatch, tmp_path):
        """Cas input was re-linked (new inode after temp+rename) so the
        existing user-facing target now points to a stale inode. samefile
        returns False; rule re-publishes."""
        td = tmp_path
        os.makedirs(td / "cas-exe" / "aa", exist_ok=True)
        os.makedirs(td / "bin", exist_ok=True)
        cas_path = str(td / "cas-exe" / "aa" / "foo_abc.exe")
        user_path = str(td / "bin" / "foo")
        # Stale: user_path hardlinks an older copy; cas_path is a
        # different inode (the new build result).
        stale = td / "stale.exe"
        stale.write_bytes(b"\x7fELF stale executable")
        os.link(stale, user_path)
        (td / "cas-exe" / "aa" / "foo_abc.exe").write_bytes(b"\x7fELF new executable")
        graph = self._make_symlink_graph(cas_path, user_path)

        with ShakeBackendTestContext(graph) as (backend, _):
            monkeypatch.chdir(tmp_path)
            executed: list[str] = []

            async def fake_execute(rule, target, flat_cmd, queued_at=None):
                # Mirror ct-cas-publish atomic re-link: unlink + hardlink.
                executed.append(target)
                os.unlink(user_path)
                os.link(cas_path, user_path)

            monkeypatch.setattr(backend, "_execute_rule_async", fake_execute)
            traces = TraceStore(str(td / ".ct-traces.json"))
            memo: dict[str, asyncio.Task[bool]] = {}
            gate = PriorityGate(1)
            asyncio.run(backend._build_async(user_path, graph, traces, memo, gate, {}))
            assert executed == [user_path]
            assert os.path.samefile(cas_path, user_path)


# ---------------------------------------------------------------------------
# Content-addressable link/library short-circuit
# ---------------------------------------------------------------------------


class TestCALinkShortCircuit:
    @pytest.fixture
    def backend(self, tmp_path):
        """Bare ShakeBackend (no __init__) wired with a MagicMock args whose
        `cas_objdir` is `tmp_path`. The 4 _ca_target tests below all use
        this minimal shape."""
        b = ShakeBackend.__new__(ShakeBackend)
        b.args = mock.MagicMock()
        b.args.cas_objdir = str(tmp_path)
        return b

    def test_ca_target_deterministic(self, backend):
        """Same rule produces the same CA target path."""
        rule = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        assert backend._ca_target(rule) == backend._ca_target(rule)

    def test_ca_target_differs_on_input_change(self, backend):
        """Different inputs produce different CA target paths."""
        rule1 = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        rule2 = BuildRule(
            output="foo", inputs=["foo.o", "bar.o"], command=["g++", "-o", "foo", "foo.o", "bar.o"], rule_type="link"
        )
        assert backend._ca_target(rule1) != backend._ca_target(rule2)

    def test_ca_target_differs_on_command_change(self, backend):
        """Different commands (same inputs) produce different CA target paths."""
        rule1 = BuildRule(output="foo", inputs=["foo.o"], command=["g++", "-o", "foo", "foo.o"], rule_type="link")
        rule2 = BuildRule(
            output="foo", inputs=["foo.o"], command=["g++", "-O2", "-o", "foo", "foo.o"], rule_type="link"
        )
        assert backend._ca_target(rule1) != backend._ca_target(rule2)

    def test_ca_target_preserves_extension(self, backend):
        """CA target preserves the file extension for libraries."""
        rule = BuildRule(
            output="libfoo.a", inputs=["foo.o"], command=["ar", "-src", "libfoo.a", "foo.o"], rule_type="static_library"
        )
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
                "compiletools.locking._run_child_async",
                side_effect=_child_writer(b"\x7fELF new exe"),
            ):
                backend.execute("build")

            assert not os.path.exists("foo.ct-sig")


# ---------------------------------------------------------------------------
# Atomic compile (TOCTOU prevention)
# ---------------------------------------------------------------------------


class TestAtomicCompile:
    def test_atomic_compile_in_locking_module(self, tmp_path):
        """Verify the shared atomic_compile function in locking.py works correctly."""

        target = str(tmp_path / "bar.o")
        lock_args = mock.MagicMock()
        lock_args.sleep_interval_flock_fallback = 0.01
        lock_args.verbose = 0
        lock = FlockLock(target, lock_args)

        observed_outputs = []

        def spy_run(cmd, *args, **kwargs):
            if "-o" in cmd:
                output_idx = cmd.index("-o") + 1
                observed_outputs.append(cmd[output_idx])
                with open(cmd[output_idx], "wb") as f:
                    f.write(b"\x7fELF object")
            return subprocess.CompletedProcess(cmd, 0, None, None)

        # SYNC atomic_compile delegates to _run_with_signal_forwarding (the
        # boundary that wraps Popen + signal forwarding); patch that. (The async
        # twin atomic_compile_async is covered in test_locking_async_contract.)
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
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "compiler_identity_src.cpp"
        obj = tmp_path / "compiler_identity_src.o"
        src.write_text("int main() {}")
        obj.write_bytes(b"\x7fELF")

        backend = _make_bare_shake_backend(tmp_path)

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
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "foo.cpp"
        obj = tmp_path / "foo.o"
        src.write_text("int main() {}")
        obj.write_bytes(b"\x7fELF")

        backend = _make_bare_shake_backend(tmp_path)

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

            def fake(cmd, *args, **kwargs):
                seen.append(list(cmd))
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                    with open(out, "wb") as f:
                        f.write(b"\x7fELF")
                return 0

            with mock.patch("compiletools.locking._run_child_async", side_effect=fake):
                backend.execute("build")

            assert len(seen) == 1
            assert "-DEXTRA=1" in seen[0]
            o_idx = seen[0].index("-o")
            assert seen[0][o_idx + 1].endswith(".tmp")


class TestVerifyAssertions:
    """Direct unit tests for ShakeBackend._verify."""

    def test_verify_asserts_when_command_is_none(self, tmp_path):
        backend = _make_bare_shake_backend(tmp_path, context=mock.MagicMock())
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
        """Link rules atomic_link directly to rule.output (the cas-exe path).
        No `_ca_target` indirection — that pattern is reserved for
        static_library/shared_library which still write to non-CAS outputs."""
        cas_exe = "cas-exe/aa/foo_abc.exe"
        link_rule = BuildRule(
            output=cas_exe,
            inputs=["foo.o"],
            command=["g++", "-o", cas_exe, "foo.o"],
            rule_type="link",
        )
        graph = BuildGraph()
        graph.add_rule(link_rule)
        graph.add_rule(BuildRule(output="build", inputs=[cas_exe], command=None, rule_type="phony"))

        with ShakeBackendTestContext(graph) as (backend, tmpdir):
            td = Path(tmpdir)
            monkeypatch.chdir(tmpdir)
            (td / "foo.o").write_bytes(b"\x7fELF fake object")
            os.makedirs(td / "cas-exe" / "aa", exist_ok=True)

            with mock.patch("compiletools.trace_backend.execute_link_rule_async") as mock_link:
                mock_link.side_effect = lambda target, cmd, args, **_kw: open(target, "wb").close() or 0
                backend.execute("build")
                assert mock_link.call_count == 1
                # execute_link_rule_async(target=rule.output, cmd, args) — direct
                # link to the cas-exe path, no in-place CA copy.
                assert mock_link.call_args.args[0] == cas_exe

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

            with mock.patch("compiletools.trace_backend.execute_link_rule_async") as mock_link:
                mock_link.side_effect = lambda target, cmd, args, **_kw: open(target, "wb").close() or 0
                backend.execute("build")
                assert mock_link.call_count == 1
                # The catch-all branch passes target=rule.output, not a CA path
                assert mock_link.call_args.args[0] == "foo.txt"

    def test_link_starts_new_session_for_signal_forwarding(self, monkeypatch):
        """End-to-end signal-forwarding regression: the async dispatch path must
        spawn the linker via ``create_subprocess_exec(..., start_new_session=True)``
        so SIGINT/SIGTERM can be forwarded to the linker's process group rather
        than orphaning it."""
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

            exec_calls = []

            class FakeProc:
                # os.getpgid(pid) must resolve, so borrow this process's pid.
                def __init__(self):
                    self.pid = os.getpid()

                async def wait(self):
                    return 0

            async def fake_exec(*args, **kwargs):
                exec_calls.append((args, kwargs))
                cmd = list(args)
                if "-o" in cmd:
                    with open(cmd[cmd.index("-o") + 1], "wb") as f:
                        f.write(b"\x7fELF link")
                return FakeProc()

            with mock.patch("compiletools.locking.asyncio.create_subprocess_exec", fake_exec):
                backend.execute("build")

            assert len(exec_calls) == 1
            kwargs = exec_calls[0][1]
            assert kwargs.get("start_new_session") is True


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

    bindir = tmp_path / "bin"
    bindir.mkdir()

    with uth.isolated_cas_dirs(tmp_path) as cas_argv:
        argv = [
            "--include",
            str(tmp_path),
            *cas_argv,
            "--bindir",
            str(bindir),
            str(src_path),
        ]

        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("Shake greeting test", argv=argv)
            uth.add_backend_arguments(cap)

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

        # Inspect the exe before isolated_cas_dirs rmtree's cas_root: on
        # platforms lacking os.link (e.g. Termux), the publish degrades to
        # a symlink and bindir/greeting would dangle after teardown.
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


# ---------------------------------------------------------------------------
# Test rules: must NOT pass through _execute_rule
# ---------------------------------------------------------------------------


class TestMakeTraceEntryGuard:
    """_make_trace_entry's invariant is that the rule's output exists.

    If a future executor ever violates this (e.g. a test rule slipping into
    the trace-execution path again), the diagnostic should name the rule and
    hint at the cause — not bubble up a cryptic FileNotFoundError from deep
    inside global_hash_registry.
    """

    def test_raises_clear_error_when_output_missing(self, tmp_path):
        rule = BuildRule(
            output=str(tmp_path / "missing.result"),
            inputs=[],
            command=["/bin/true"],
            rule_type="test",
        )
        context = BuildContext()
        with pytest.raises(RuntimeError, match="executed successfully but its output file is missing"):
            _make_trace_entry(rule, context)


class TestShakeTestRulesExecutedDuringBuild:
    """ShakeBackend walks ``RuleType.TEST`` rules during the build
    phase. ``_do_build`` recurses into a test rule (rather than early-returning)
    and feeds it to ``_execute_rule``, which runs the pure-argv test command
    in-process. The test rule never enters the trace store, so
    ``_make_trace_entry`` is never asked to hash its (XML or .result) output.
    """

    def test_do_build_executes_test_rules(self, tmp_path):
        # A real, instantly-passing test exe so _execute_rule's subprocess.run
        # succeeds and the .result marker gets touched.
        result_path = str(tmp_path / "test_foo.result")
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=result_path,
                inputs=[],
                command=["/bin/true"],
                rule_type="test",
                success_marker=result_path,
            )
        )

        with ShakeBackendTestContext(graph) as (backend, _tmpdir):
            traces = TraceStore(str(tmp_path / ".ct-traces.json"))
            memo: dict[str, asyncio.Task[bool]] = {}
            changed = asyncio.run(backend._build_async(result_path, graph, traces, memo, PriorityGate(1), {}))
            # Test rules do not participate in early cutoff.
            assert changed is False
            # The success marker was touched by _touch_result_marker on rc==0.
            assert os.path.exists(result_path)
            # The test rule never entered the trace store.
            assert traces.get(result_path) is None
            assert not backend._test_failures

    def test_do_build_aggregates_failures(self, tmp_path):
        result_path = str(tmp_path / "test_fail.result")
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=result_path,
                inputs=[],
                command=["/bin/false"],
                rule_type="test",
                success_marker=result_path,
            )
        )

        with ShakeBackendTestContext(graph) as (backend, _tmpdir):
            traces = TraceStore(str(tmp_path / ".ct-traces.json"))
            memo: dict[str, asyncio.Task[bool]] = {}
            asyncio.run(backend._build_async(result_path, graph, traces, memo, PriorityGate(1), {}))
            # Failure aggregated, not raised mid-flight; .result NOT touched.
            assert backend._test_failures
            assert result_path in backend._test_failures[0]
            assert not os.path.exists(result_path)


class TestTraceInputCanonicalization:
    """``_make_trace_entry`` must store input_hashes keys as gitroot-relative
    paths so that a trace written under workspace A verifies under workspace B
    (the cas-objdir surviving across CI checkouts at differing paths is the
    primary motivating use case — see
    docs/superpowers/specs/2026-05-09-cas-mtime-block-design.md).

    Mirrors the path-canonical CAS-key fix shipped in 9.1.0 for the per-TU
    object / PCH / PCM caches; this lifts the same property to the trace
    layer so non-compile rules (link, library) also become workspace-portable
    on a shared CAS.
    """

    def test_make_trace_entry_canonicalizes_input_keys(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "foo.cpp"
        obj = tmp_path / "foo.o"
        src.write_text("int main() {}")
        obj.write_bytes(b"\x7fELF")

        backend = _make_bare_shake_backend(tmp_path)

        rule = BuildRule(
            output=str(obj),
            inputs=[str(src)],
            command=["cp", str(src), str(obj)],
            rule_type="copy",
        )

        # Patch find_git_root so the trace's anchor is *tmp_path*. Otherwise the
        # tmp_path lies outside the real surrounding gitroot and pass-through
        # kicks in (see canonicalize_path_for_cache_key's "outside the anchor"
        # branch).
        with mock.patch("compiletools.git_utils.find_git_root", return_value=str(tmp_path)):
            entry = _make_trace_entry(rule, backend.context)

        # Every key must contain the sentinel — not the raw absolute prefix.
        assert entry.input_hashes, "entry should record at least one input"
        for key in entry.input_hashes:
            assert _GITROOT_SENTINEL in key, (
                f"input_hashes key {key!r} not canonicalized; should contain <GITROOT> sentinel"
            )
            assert str(tmp_path) not in key, f"input_hashes key {key!r} still carries absolute workspace prefix"

    def test_verify_succeeds_across_workspace_paths(self, tmp_path, monkeypatch):
        """Two workspaces with the same content but different absolute paths
        must produce traces that mutually verify. This is the cross-CI-runner
        cache-reuse property the bug report demands."""
        # Workspace A
        ws_a = tmp_path / "run-1" / "repo"
        ws_a.mkdir(parents=True)
        (ws_a / "foo.cpp").write_text("int main() {}\n")
        (ws_a / "foo.o").write_bytes(b"\x7fELF identical-bytes")

        # Workspace B (different absolute path, identical contents)
        ws_b = tmp_path / "run-2" / "repo"
        ws_b.mkdir(parents=True)
        (ws_b / "foo.cpp").write_text("int main() {}\n")
        (ws_b / "foo.o").write_bytes(b"\x7fELF identical-bytes")

        backend = _make_bare_shake_backend(tmp_path, cas_subdir="cas")

        # Build the trace under workspace A.
        rule_a = BuildRule(
            output=str(ws_a / "foo.o"),
            inputs=[str(ws_a / "foo.cpp")],
            command=["cp", str(ws_a / "foo.cpp"), str(ws_a / "foo.o")],
            rule_type="copy",
        )
        with mock.patch("compiletools.git_utils.find_git_root", return_value=str(ws_a)):
            entry = _make_trace_entry(rule_a, backend.context)

        # Verify the same content under workspace B with a different absolute prefix.
        rule_b = BuildRule(
            output=str(ws_b / "foo.o"),
            inputs=[str(ws_b / "foo.cpp")],
            command=["cp", str(ws_b / "foo.cpp"), str(ws_b / "foo.o")],
            rule_type="copy",
        )
        # Verify needs to read the workspace-B output, so the rule's output
        # path must point at ws_b. The trace output_hash was computed from
        # workspace A, but content is identical, so output hashes match.
        with mock.patch("compiletools.git_utils.find_git_root", return_value=str(ws_b)):
            assert backend._verify(rule_b, entry) is True, (
                "Trace written at workspace A must verify at workspace B "
                "when contents are identical (cross-workspace CAS portability)."
            )


class TestShakeRunsTestsInBuildPhase:
    """End-to-end: ShakeBackend.execute("build") walks the ``all``
    phony -> ``runtests`` -> every ``RuleType.TEST`` rule, so each test fires
    in-process as soon as its exe's link future resolves. Mirrors the
    make/ninja e2e tests.
    """

    @pytest.fixture(autouse=True)
    def _reset_parser_state(self):
        uth.reset()
        yield
        uth.reset()

    @pytest.fixture(autouse=True)
    def _chdir_to_tmp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

    @uth.requires_functional_compiler
    def test_shake_runs_tests_in_build(self, tmp_path):
        """After execute("build") — NOT execute("runtests") — the test's
        ``.result`` success marker exists, proving the test ran during the
        build phase."""
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_pass.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 0; }\n')

        backend, graph = uth.build_real_backend(ShakeBackend, tmp_path, [], tests=[test_src])
        backend.generate(graph)
        backend.execute("build")

        assert uth.find_result_markers(tmp_path), (
            "no .result marker after execute('build') — test did not run during the build phase"
        )

    @uth.requires_functional_compiler
    def test_shake_test_failure_fails_build(self, tmp_path):
        """A deliberately-failing no-framework test must make execute("build")
        raise, and the failing test's ``.result`` marker must NOT be created
        (the marker is only touched on rc==0)."""
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_fail.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 1; }\n')

        backend, graph = uth.build_real_backend(ShakeBackend, tmp_path, [], tests=[test_src])
        backend.generate(graph)

        with pytest.raises(RuntimeError, match="test execution failed"):
            backend.execute("build")

        assert not uth.find_result_markers(tmp_path), (
            "failing test left a .result marker (marker touched despite rc!=0)"
        )

    @uth.requires_functional_compiler
    def test_shake_aggregates_test_failures(self, tmp_path):
        """TWO deliberately-failing tests: BOTH must be reported in the raised
        error, proving shake aggregates failures (appends to _test_failures and
        continues) rather than stopping on the first failure."""
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_a = tmp_path / "test_fail_a.cpp"
        test_a.write_text('#include "unit_test.hpp"\nint main() { return 1; }\n')
        test_b = tmp_path / "test_fail_b.cpp"
        test_b.write_text('#include "unit_test.hpp"\nint main() { return 2; }\n')

        backend, graph = uth.build_real_backend(ShakeBackend, tmp_path, [], tests=[test_a, test_b])
        backend.generate(graph)

        with pytest.raises(RuntimeError) as excinfo:
            backend.execute("build")

        msg = str(excinfo.value)
        assert "test_fail_a" in msg, f"first failing test not aggregated: {msg}"
        assert "test_fail_b" in msg, f"second failing test not aggregated: {msg}"
        assert not uth.find_result_markers(tmp_path)

    @uth.requires_functional_compiler
    def test_shake_framework_test_failure_preserves_xml(self, tmp_path):
        """A failing framework-detected test writes its JUnit XML report and
        *then* exits non-zero. The test rule's ``output`` is the XML path, but
        shake never feeds a test rule to _make_trace_entry, and nothing deletes
        the XML on failure (shake has no .DELETE_ON_ERROR analogue). Asserts:
          - execute("build") raises,
          - the failing test's ``.result`` marker is NOT created,
          - the JUnit XML file DOES still exist after the failed build.
        """
        test_src = uth.write_failing_gtest_fixture(tmp_path)

        xml_dir = tmp_path / "junit"
        backend, graph = uth.build_real_backend(
            ShakeBackend, tmp_path, [], tests=[test_src], extra_argv=["--test-xml-dir=" + str(xml_dir)]
        )

        # The framework test rule's output must be the XML path (not the
        # .result marker) for this test to exercise the dual-output shape.
        test_rules = [r for r in graph.rules if r.rule_type == "test"]
        assert len(test_rules) == 1
        xml_rule = test_rules[0]
        assert xml_rule.output != xml_rule.success_marker, (
            "framework was not detected -- test rule output is still the .result marker"
        )
        assert xml_rule.output.endswith(".xml")
        xml_path = xml_rule.output

        backend.generate(graph)
        with pytest.raises(RuntimeError, match="test execution failed"):
            backend.execute("build")

        assert not uth.find_result_markers(tmp_path), (
            "failing test left a .result marker (marker touched despite rc!=0)"
        )
        assert os.path.exists(xml_path), (
            f"JUnit XML at {xml_path} was deleted after the failed shake build -- "
            "a failed framework test must still leave its report behind"
        )


# The driver runs in a REAL subprocess (main thread of the main interpreter,
# so ``loop.add_signal_handler`` actually installs -- an in-process pytest
# thread could not exercise that path) and executes a genuine
# ``ShakeBackend.execute("build")`` over a graph whose slow rule records its
# own PID before sleeping. No subprocess mocking (async dispatch tests must
# drive the real spawn path).
_SIGTERM_DRIVER = """
import sys

from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.testhelper import ShakeBackendTestContext

pidfile, slow_out, after_out = sys.argv[1], sys.argv[2], sys.argv[3]

graph = BuildGraph()
graph.add_rule(
    BuildRule(
        output=slow_out,
        inputs=[],
        command=["sh", "-c", 'echo $$ > "%s"; sleep 30; touch "%s"' % (pidfile, slow_out)],
        rule_type="copy",
    )
)
graph.add_rule(
    BuildRule(output=after_out, inputs=[slow_out], command=["touch", after_out], rule_type="copy")
)
graph.add_rule(BuildRule(output="build", inputs=[after_out], command=None, rule_type="phony"))

with ShakeBackendTestContext(graph) as (backend, tmpdir):
    backend.execute("build")
"""


class TestSigtermAbortsShakeBuild:
    """Signal-forwarding contract for the M2 single event-loop handler:
    SIGTERM to a running shake build must (a) reach the live child's process
    group, (b) abort the build (no further rules dispatched), and (c) make the
    parent exit with conventional killed-by-SIGTERM status. Counterpart of
    test_ct_lock_helper.py's TestGracefulExitSignalStack for the sync path."""

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX-only signal test")
    def test_sigterm_kills_child_and_aborts_build(self, tmp_path):
        import signal as _signal
        import sys as _sys
        import time as _time

        # Identity-safe child-death polling helpers (PID-reuse guard).
        from compiletools.test_ct_lock_helper import _child_gone, _proc_start_ticks

        pidfile = tmp_path / "CHILD_PID"
        slow_out = tmp_path / "slow.out"
        after_out = tmp_path / "after.out"

        proc = subprocess.Popen(
            [_sys.executable, "-c", _SIGTERM_DRIVER, str(pidfile), str(slow_out), str(after_out)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = None
        try:
            # Wait for the slow rule's shell to record its own pid. Generous
            # deadline: the driver imports compiletools + inits a backend first.
            # ``echo $$ > file`` creates (truncates) before writing, so on a
            # slow/networked FS the file can exist while still empty — treat
            # empty content as "not ready yet", not just missing.
            deadline = _time.time() + 30
            pid_text = ""
            while proc.poll() is None and _time.time() < deadline:
                if pidfile.exists():
                    pid_text = pidfile.read_text().strip()
                    if pid_text:
                        break
                _time.sleep(0.05)
            if not pid_text:
                out, err = proc.communicate(timeout=5)
                pytest.fail(
                    f"driver never started the slow child (rc={proc.poll()})\n"
                    f"stdout: {out.decode(errors='replace')}\nstderr: {err.decode(errors='replace')}"
                )
            child_pid = int(pid_text)
            assert child_pid > 0
            child_start_ticks = _proc_start_ticks(child_pid)

            proc.send_signal(_signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail("shake build did not exit after SIGTERM -- signal swallowed")

            # (c) Conventional killed-by-SIGTERM exit: execute() re-delivers
            # the signal via SIG_DFL + raise_signal after saving traces/costs.
            assert proc.returncode == -_signal.SIGTERM, (
                f"expected killed-by-SIGTERM (rc {-_signal.SIGTERM}), got rc={proc.returncode}; "
                f"stderr: {proc.stderr.read().decode(errors='replace') if proc.stderr else ''}"
            )

            # (a) The child process group received the forwarded signal (or the
            # CancelledError path SIGKILLed it) -- poll with identity check so a
            # recycled pid is not misread as the child surviving.
            child_alive = True
            deadline = _time.time() + 5
            while _time.time() < deadline:
                if _child_gone(child_pid, child_start_ticks):
                    child_alive = False
                    break
                _time.sleep(0.05)
            assert not child_alive, (
                f"child pid {child_pid} survived SIGTERM to the shake build -- "
                f"the event-loop handler did not forward to the child pgid"
            )

            # (b) The build aborted rather than continuing: the downstream rule
            # gated on the slow rule must never have run.
            assert not after_out.exists(), (
                "downstream rule ran after SIGTERM -- build_task.cancel() did not abort the traversal"
            )
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            if proc.poll() is None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                proc.wait()
            # The slow child lives in its OWN session (start_new_session in
            # _run_child_async), so the driver's pgid kill cannot reach it;
            # clean it up directly if the assertion above failed.
            if child_pid is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(child_pid, _signal.SIGKILL)
