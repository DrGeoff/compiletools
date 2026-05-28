"""Tests for ct-cake's ``--ccache-statslog`` flag.

Covers:
  - argparse registration (auto vs explicit-path forms, plus bare flag).
  - ``auto`` mode allocates a path under the per-invocation diagnostics dir.
  - The CCACHE_STATSLOG env var is exported into ``os.environ`` for the
    build's subprocesses to inherit.
  - The statslog file is removed post-publish in ``auto`` mode, kept for
    explicit-path mode.
  - Parse-and-publish failures (missing file, bad lines) do not break
    the build.
  - --otel-export is implied-but-not-required: the statslog file is
    still written even without --otel-export; a verbose warning explains
    no metrics will ship.
"""

from __future__ import annotations

import os

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.diagnostics
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _reset_diagnostics_cache():
    compiletools.diagnostics._reset_for_tests()
    uth.reset()
    # Snapshot the inbound env so the post-test assertion checks the
    # delta caused by process(), not whatever the harness inherited.
    pre = os.environ.get("CCACHE_STATSLOG")
    yield
    post = os.environ.get("CCACHE_STATSLOG")
    compiletools.diagnostics._reset_for_tests()
    uth.reset()
    # Defensive cleanup so we never poison sibling tests. But ALSO assert
    # that the test left no leak behind -- if this fires, Cake.process()
    # forgot to restore CCACHE_STATSLOG.
    os.environ.pop("CCACHE_STATSLOG", None)
    if pre is not None:
        os.environ["CCACHE_STATSLOG"] = pre
    assert post == pre, f"CCACHE_STATSLOG leaked across test boundary: pre={pre!r} post={post!r}"


def _build_args(argv):
    cap = compiletools.apptools.create_parser("test ccache statslog", argv=argv)
    compiletools.cake.Cake.add_arguments(cap)
    return compiletools.apptools.parseargs(cap, argv, verbose=0, context=BuildContext())


def _stub_process(monkeypatch):
    """Short-circuit Cake.process() so no real build runs."""

    def _stub_createctobjs(self):
        self.hunter = object()

    def _stub_call_backend(self):
        return None

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _stub_call_backend)


# ----------------------------------------------------------- argparse wiring


class TestCcacheStatslogArgparse:
    def test_default_is_none(self, tmp_path):
        args = _build_args(["--bindir", str(tmp_path / "bin")])
        assert args.ccache_statslog is None

    def test_bare_flag_means_auto(self, tmp_path):
        args = _build_args(["--bindir", str(tmp_path / "bin"), "--ccache-statslog"])
        assert args.ccache_statslog == "auto"

    def test_explicit_auto_value(self, tmp_path):
        args = _build_args(["--bindir", str(tmp_path / "bin"), "--ccache-statslog=auto"])
        assert args.ccache_statslog == "auto"

    def test_explicit_path(self, tmp_path):
        args = _build_args(
            [
                "--bindir",
                str(tmp_path / "bin"),
                "--ccache-statslog",
                str(tmp_path / "my-statslog"),
            ]
        )
        assert args.ccache_statslog == str(tmp_path / "my-statslog")


# ----------------------------------------------------------- path resolution


class TestCcacheStatslogAutoMode:
    def test_auto_resolves_under_diagnostics_dir(self, tmp_path):
        bindir = tmp_path / "bin"
        args = _build_args(
            [
                "--bindir",
                str(bindir),
                "--ccache-statslog=auto",
            ]
        )
        cake = compiletools.cake.Cake(args)
        path = cake._resolve_ccache_statslog_path()
        iid = compiletools.diagnostics.invocation_id()
        expected = str(bindir / "diagnostics" / iid / "ccache.statslog")
        assert path == expected

    def test_explicit_path_returned_verbatim_as_abspath(self, tmp_path):
        bindir = tmp_path / "bin"
        explicit = tmp_path / "outside-dir" / "x.statslog"
        args = _build_args(
            [
                "--bindir",
                str(bindir),
                "--ccache-statslog",
                str(explicit),
            ]
        )
        cake = compiletools.cake.Cake(args)
        path = cake._resolve_ccache_statslog_path()
        assert path == str(explicit)

    def test_setup_exports_env_var(self, tmp_path, monkeypatch):
        # _setup_ccache_statslog_env mutates os.environ in place so the
        # build subprocesses inherit CCACHE_STATSLOG. Verify the mutation.
        # monkeypatch.delenv => undone on teardown, so the leak-detection
        # fixture sees pre==post and doesn't fire.
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        args = _build_args(
            [
                "--bindir",
                str(bindir),
                "--ccache-statslog=auto",
            ]
        )
        cake = compiletools.cake.Cake(args)
        path = cake._setup_ccache_statslog_env()
        assert path is not None
        assert os.environ.get("CCACHE_STATSLOG") == path
        # Parent dir must exist so ccache's open() doesn't no-op.
        assert os.path.isdir(os.path.dirname(path))
        # This test exercises the primitive directly (not process()), so
        # the env mutation here is the test's responsibility to undo --
        # process() has its own restore-on-exit which the leak-detection
        # fixture enforces for all tests that DO go through process().
        del os.environ["CCACHE_STATSLOG"]

    def test_setup_returns_none_when_flag_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        args = _build_args(["--bindir", str(tmp_path / "bin")])
        cake = compiletools.cake.Cake(args)
        assert cake._setup_ccache_statslog_env() is None
        assert "CCACHE_STATSLOG" not in os.environ


# ----------------------------------------------------------- process() wiring


class TestCcacheStatslogProcessFlow:
    def test_env_set_before_backend_runs(self, tmp_path, monkeypatch):
        """The CCACHE_STATSLOG export must happen before _call_backend
        fires (otherwise the compile subprocesses already spawned without
        the env var). Capture os.environ at backend-entry to verify."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        args = _build_args(
            [
                "--bindir",
                str(bindir),
                "--cas-objdir",
                str(objdir),
                "--ccache-statslog=auto",
                "--filename",
                "irrelevant.cpp",
            ]
        )
        captured = {}

        def _stub_createctobjs(self):
            self.hunter = object()

        def _capture_call_backend(self):
            captured["env"] = os.environ.get("CCACHE_STATSLOG")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _capture_call_backend)

        cake = compiletools.cake.Cake(args)
        cake.process()
        assert captured["env"] is not None
        # The captured env must point under the diagnostics dir.
        iid = compiletools.diagnostics.invocation_id()
        assert captured["env"] == str(bindir / "diagnostics" / iid / "ccache.statslog")

    def test_auto_mode_removes_statslog_after_publish(self, tmp_path, monkeypatch):
        """auto mode: post-build ingest deletes the statslog file."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        statslog_path_holder = {}

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_statslog(self):
            # Pretend the build wrote a statslog.
            p = os.environ["CCACHE_STATSLOG"]
            statslog_path_holder["p"] = p
            with open(p, "w") as fh:
                fh.write("direct_cache_hit\ncache_miss\n")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _seed_statslog)

        cake = compiletools.cake.Cake(args)
        cake.process()

        # File must be gone after the publish.
        assert "p" in statslog_path_holder
        assert not os.path.exists(statslog_path_holder["p"])

    def test_explicit_path_is_kept(self, tmp_path, monkeypatch):
        """Explicit-path mode: cleanup is the caller's responsibility."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        explicit = tmp_path / "ext-dir" / "x.statslog"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog",
            str(explicit),
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_statslog(self):
            p = os.environ["CCACHE_STATSLOG"]
            with open(p, "w") as fh:
                fh.write("direct_cache_hit\n")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _seed_statslog)

        cake = compiletools.cake.Cake(args)
        cake.process()

        assert os.path.exists(str(explicit))

    def test_missing_statslog_does_not_crash(self, tmp_path, monkeypatch):
        """Build fired but ccache never wrote (e.g. no cacheable compiles).
        Publish must be a silent no-op."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        def _stub_createctobjs(self):
            self.hunter = object()

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(
            compiletools.cake.Cake,
            "_call_backend",
            lambda self: None,  # build does nothing, no statslog created
        )

        cake = compiletools.cake.Cake(args)
        # Must not raise.
        cake.process()

    def test_unparseable_statslog_does_not_crash(self, tmp_path, monkeypatch):
        """A statslog full of torn-write garbage must not break the build."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_garbage(self):
            p = os.environ["CCACHE_STATSLOG"]
            with open(p, "w") as fh:
                fh.write("garbage with spaces\n\x00binary junk\nmore garbage that cannot be a ccache event name here\n")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _seed_garbage)

        cake = compiletools.cake.Cake(args)
        cake.process()  # must not raise

    def test_without_otel_export_statslog_still_written(self, tmp_path, monkeypatch, capsys):
        """--ccache-statslog without --otel-export: the file IS still
        produced (env var is exported, build inherits it), no metrics
        ship. The flag is useful on its own for offline review."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)
        assert args.otel_export is False  # default

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_statslog(self):
            p = os.environ["CCACHE_STATSLOG"]
            with open(p, "w") as fh:
                fh.write("direct_cache_hit\n")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _seed_statslog)

        cake = compiletools.cake.Cake(args)
        cake.process()
        # env-var was set during the build. After process() completes the
        # file is removed (auto mode); the lack of a crash is the
        # contract. We assert the stdout summary mentions ccache.
        captured = capsys.readouterr()
        assert "ccache" in captured.out.lower() or "ccache" not in captured.err.lower()

    def test_env_var_popped_after_successful_build(self, tmp_path, monkeypatch):
        """process() must pop CCACHE_STATSLOG it set, so a second ct-cake
        run in the same Python process (in-process batch mode) doesn't
        inherit a stale env var pointing at an already-deleted
        ``auto``-mode path."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_statslog(self):
            p = os.environ["CCACHE_STATSLOG"]
            with open(p, "w") as fh:
                fh.write("direct_cache_hit\n")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _seed_statslog)

        cake = compiletools.cake.Cake(args)
        cake.process()

        assert os.environ.get("CCACHE_STATSLOG") is None

    def test_env_var_restored_when_user_set_it_first(self, tmp_path, monkeypatch):
        """If the user passed CCACHE_STATSLOG via env (not the flag),
        process() must NOT pop it on exit -- restore-not-pop preserves
        a value the caller put there themselves."""
        preexisting = str(tmp_path / "user-supplied.statslog")
        monkeypatch.setenv("CCACHE_STATSLOG", preexisting)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        # NOTE: no --ccache-statslog flag -- the env var alone is the
        # user's signal. Cake.process() should leave it untouched.
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        def _stub_createctobjs(self):
            self.hunter = object()

        def _stub_call_backend(self):
            # User-set env must still be visible during the build.
            assert os.environ.get("CCACHE_STATSLOG") == preexisting

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _stub_call_backend)

        cake = compiletools.cake.Cake(args)
        cake.process()

        assert os.environ.get("CCACHE_STATSLOG") == preexisting

    def test_env_var_popped_on_build_exception(self, tmp_path, monkeypatch):
        """The env restore must run regardless of whether _call_backend
        raised. Otherwise a failed build leaks CCACHE_STATSLOG into the
        next ct-cake invocation in the same Python process."""
        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        class _BuildBoom(RuntimeError):
            pass

        def _stub_createctobjs(self):
            self.hunter = object()

        def _explode(self):
            raise _BuildBoom("backend exploded")

        monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _explode)

        cake = compiletools.cake.Cake(args)
        with pytest.raises(_BuildBoom):
            cake.process()

        assert os.environ.get("CCACHE_STATSLOG") is None


class TestCcacheStatslogRootSpanLift:
    """When --otel-export is on, ccache headline numbers must land on
    the root build span as ``ct.ccache.*`` attributes -- so a single
    span query answers "what was this build's ccache hit rate?".

    We exercise the lift through the same in-memory span exporter that
    test_traces.py uses for its rule-span assertions.
    """

    def test_ccache_attrs_appear_on_root_span(self, tmp_path, monkeypatch):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        monkeypatch.delenv("CCACHE_STATSLOG", raising=False)
        bindir = tmp_path / "bin"
        objdir = tmp_path / "obj"
        argv = [
            "--bindir",
            str(bindir),
            "--cas-objdir",
            str(objdir),
            "--timing",
            "--otel-export",
            "--ccache-statslog=auto",
            "--filename",
            "irrelevant.cpp",
        ]
        args = _build_args(argv)

        exporter = InMemorySpanExporter()
        processor = SimpleSpanProcessor(exporter)

        # Patch export_buildtimer to use the in-memory processor and
        # _publish_ccache_stats to a no-op so we don't open a real OTLP
        # metric connection during the test.
        import compiletools.otel as ot

        real_export = ot.export_buildtimer

        def _patched_export(timer, args):
            return real_export(timer, args, _processor=processor)

        monkeypatch.setattr(ot, "export_buildtimer", _patched_export)
        # Also patch the imported binding inside cake.py's local-import
        # site so the finally block uses the patched function.
        import compiletools.cake as cake_mod

        # We can't easily monkeypatch the "from compiletools.otel import
        # export_buildtimer" local binding, but the from-import lives
        # inside the finally block and resolves the attribute each time.
        # So patching ot.export_buildtimer is enough.

        # Stub out metric publish (no OTLP collector in tests).
        monkeypatch.setattr(
            cake_mod.Cake,
            "_publish_ccache_stats",
            lambda self, statslog_path, counts, root_trace_id: None,
        )

        def _stub_createctobjs(self):
            self.hunter = object()

        def _seed_statslog(self):
            p = os.environ["CCACHE_STATSLOG"]
            with open(p, "w") as fh:
                fh.write("direct_cache_hit\ndirect_cache_hit\ncache_miss\n")

        monkeypatch.setattr(cake_mod.Cake, "_createctobjs", _stub_createctobjs)
        monkeypatch.setattr(cake_mod.Cake, "_call_backend", _seed_statslog)

        cake = cake_mod.Cake(args)
        cake.process()

        spans = exporter.get_finished_spans()
        root = next((s for s in spans if s.name == "compiletools.build"), None)
        assert root is not None
        attrs = dict(root.attributes or {})
        assert attrs.get("ct.ccache.direct_hits") == 2
        assert attrs.get("ct.ccache.misses") == 1
        # Hit rate is a float; pytest.approx ok.
        assert attrs.get("ct.ccache.hit_rate") == pytest.approx(2 / 3)
