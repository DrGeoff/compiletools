"""Tests for ct-cake's --diagnostics-dir flag and timing.json routing.

Covers:
  - argparse registration (CLI/env/conf wiring via configargparse)
  - timing.json output is routed through resolve_diagnostics_dir(), not
    the old <objdir>/.ct-timing.json location.
"""

from __future__ import annotations

import os

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.diagnostics
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.build_timer import BuildTimer


@pytest.fixture(autouse=True)
def _reset_diagnostics_cache():
    compiletools.diagnostics._reset_for_tests()
    uth.reset()
    yield
    compiletools.diagnostics._reset_for_tests()
    uth.reset()


def _build_args(argv):
    """Build args via the same parser ct-cake uses."""
    cap = compiletools.apptools.create_parser("test cake diagnostics", argv=argv)
    compiletools.cake.Cake.add_arguments(cap)
    return compiletools.apptools.parseargs(cap, argv, verbose=0, context=BuildContext())


def _emulate_finally_block(args):
    """Reproduce the timer-write step of Cake.process()'s finally block."""
    timer = BuildTimer(enabled=True, variant=getattr(args, "variant", ""), backend=getattr(args, "backend", "make"))
    diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(args)
    out = os.path.join(diag_dir, "timing.json")
    timer.to_json(out)
    return out


def _bindir_objdir_argv(tmp_path, *extras):
    """Return (bindir, objdir, argv) for the recurring
    --bindir/--cas-objdir/--timing skeleton. ``*extras`` appends extra
    argv items (e.g. ``--filename irrelevant.cpp``)."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = ["--bindir", str(bindir), "--cas-objdir", str(objdir), "--timing", *extras]
    return bindir, objdir, argv


def test_diagnostics_dir_default_falls_back_to_bindir(tmp_path):
    """With --bindir set and no --diagnostics-dir, timing.json lands under
    <bindir>/diagnostics/<invocation-id>/."""
    bindir, _objdir, argv = _bindir_objdir_argv(tmp_path)
    args = _build_args(argv)
    out = _emulate_finally_block(args)
    iid = compiletools.diagnostics.invocation_id()
    expected = str(bindir / "diagnostics" / iid / "timing.json")
    assert out == expected
    assert os.path.isfile(out)


def test_diagnostics_dir_explicit_override(tmp_path):
    """With --diagnostics-dir set, timing.json lands under <diag>/<invocation-id>/."""
    bindir = tmp_path / "bin"
    diag = tmp_path / "diag"
    argv = [
        "--bindir",
        str(bindir),
        "--diagnostics-dir",
        str(diag),
        "--timing",
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(diag)
    out = _emulate_finally_block(args)
    iid = compiletools.diagnostics.invocation_id()
    expected = str(diag / iid / "timing.json")
    assert out == expected
    assert os.path.isfile(out)


def test_diagnostics_not_in_objdir(tmp_path):
    """Timing JSON must not land in --cas-objdir (content-addressable cache)."""
    _bindir, objdir, argv = _bindir_objdir_argv(tmp_path)
    args = _build_args(argv)
    objdir.mkdir(parents=True, exist_ok=True)
    _emulate_finally_block(args)
    assert not os.path.exists(objdir / ".ct-timing.json")
    assert not os.path.exists(objdir / "timing.json")


def test_diagnostics_timing_json_written_through_process(monkeypatch, tmp_path):
    """End-to-end anchor: catches refactors that disconnect the finally block
    from the diagnostics helper.

    The other tests in this module exercise the timing-write step via a local
    helper that mirrors cake.py's finally block. This test drives
    ``Cake.process()`` itself so a future refactor that deletes/renames/skips
    the finally-block call to ``resolve_diagnostics_dir`` will fail loudly.
    The backend is short-circuited (raises) so the build itself is never
    invoked; we only assert that the finally block ran and wrote timing.json
    at the resolved diagnostics location even when ``_call_backend`` raises.
    """
    bindir, _objdir, argv = _bindir_objdir_argv(tmp_path, "--filename", "irrelevant.cpp")
    args = _build_args(argv)

    # Short-circuit the parts of process() that would do real work. We keep
    # the finally block in process() reachable on both the success and the
    # exception path.
    def _stub_createctobjs(self):
        # process() asserts self.hunter is not None inside the target_discovery
        # phase, so satisfy that without doing real header-deps work.
        self.hunter = object()

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)

    def _boom(self):
        raise RuntimeError("backend disabled for this test")

    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _boom)

    cake = compiletools.cake.Cake(args)
    with pytest.raises(RuntimeError, match="backend disabled"):
        cake.process()

    iid = compiletools.diagnostics.invocation_id()
    expected = str(bindir / "diagnostics" / iid / "timing.json")
    assert os.path.isfile(expected), (
        f"Cake.process() finally block did not write timing.json at {expected}; "
        "diagnostics-dir wiring may have been disconnected."
    )


def test_diagnostics_dir_env_var(monkeypatch, tmp_path):
    """DIAGNOSTICS_DIR env var binds to args.diagnostics_dir via configargparse."""
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("DIAGNOSTICS_DIR", str(env_dir))
    argv = [
        "--bindir",
        str(tmp_path / "bin"),
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(env_dir)


def test_diagnostics_dir_config_file(tmp_path):
    """diagnostics-dir = <path> in a config file binds to args.diagnostics_dir."""
    conf_path = tmp_path / "diag.conf"
    conf_dir = tmp_path / "from-conf"
    conf_path.write_text(f"diagnostics-dir = {conf_dir}\n")
    argv = [
        "-c",
        str(conf_path),
        "--bindir",
        str(tmp_path / "bin"),
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(conf_dir)


def test_otel_export_failure_does_not_fail_build(monkeypatch, tmp_path, capsys):
    """README.ct-otel.rst promise: 'a failed export does not fail the build'.

    A RuntimeError from export_buildtimer (e.g. the SDK-missing hint, or any
    runtime collector failure) must be swallowed into a stderr warning so the
    surrounding finally block still completes and the caller of process()
    never sees the exception.
    """
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--cas-objdir",
        str(objdir),
        "--timing",
        "--otel-export",
        "--filename",
        "irrelevant.cpp",
    ]
    args = _build_args(argv)
    assert args.otel_export is True

    def _stub_createctobjs(self):
        self.hunter = object()

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", lambda self: None)

    import compiletools.otel as oe

    def _boom(timer, args):
        raise RuntimeError("boom")

    monkeypatch.setattr(oe, "export_buildtimer", _boom)

    cake = compiletools.cake.Cake(args)
    cake.process()

    captured = capsys.readouterr()
    assert "OTLP export failed: boom" in captured.err


def test_otel_export_with_no_timing_hard_errors(tmp_path):
    """``--otel-export --no-timing`` is internally contradictory and
    must hard-error at validate time, not silently warn and continue.
    The user has asked the exporter to ship spans while also asking the
    collector not to collect them; better to fail loudly than ship an
    empty span tree."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--cas-objdir",
        str(objdir),
        "--otel-export",
        "--no-timing",
        "--filename",
        "irrelevant.cpp",
    ]
    args = _build_args(argv)
    assert args.otel_export is True
    assert args.timing is False

    with pytest.raises(SystemExit, match="mutually exclusive"):
        compiletools.apptools.validate_otel_timing_pair(args)


def test_otel_export_implies_timing(tmp_path):
    """``--otel-export`` without explicit ``--no-timing`` flips
    ``args.timing`` to True so the span tree is populated. Removes a
    footgun where the user thought ``--otel-export`` alone was enough."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--cas-objdir",
        str(objdir),
        "--otel-export",
        "--filename",
        "irrelevant.cpp",
    ]
    args = _build_args(argv)
    assert args.otel_export is True
    # Pre-validate: default --timing is False; the implication has not
    # fired yet because validate_otel_timing_pair has not been called.
    assert args.timing is False

    compiletools.apptools.validate_otel_timing_pair(args)
    assert args.timing is True


def test_otel_export_with_timing_no_warning(monkeypatch, tmp_path, capsys):
    """Sanity: ``--otel-export --timing`` proceeds without the legacy
    'no spans to export' warning and without raising."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--cas-objdir",
        str(objdir),
        "--timing",
        "--otel-export",
        "--filename",
        "irrelevant.cpp",
    ]
    args = _build_args(argv)
    # Validator is a silent no-op on the well-formed pairing.
    compiletools.apptools.validate_otel_timing_pair(args)
    assert args.timing is True

    def _stub_createctobjs(self):
        self.hunter = object()

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", lambda self: None)

    import compiletools.otel as oe

    monkeypatch.setattr(oe, "export_buildtimer", lambda timer, args: None)

    cake = compiletools.cake.Cake(args)
    cake.process()

    captured = capsys.readouterr()
    assert "no spans to export" not in captured.err


def test_ct_rule_outcomes_log_restored_on_exception(monkeypatch, tmp_path):
    """If a step inside the post-build pipeline (here: ``BuildTimer.to_json``)
    raises, CT_RULE_OUTCOMES_LOG set *during* the build (by _call_backend)
    must still be cleared on the way out. Otherwise the env var leaks into
    the next invocation in the same Python process (in-process batch mode,
    tests, REPL).
    """
    _bindir, _objdir, argv = _bindir_objdir_argv(tmp_path, "--filename", "irrelevant.cpp")
    args = _build_args(argv)

    def _stub_createctobjs(self):
        self.hunter = object()

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)

    # Mimic the real _call_backend, which exports the outcomes-log path
    # for backend children to append to. The _env_var_restored context in
    # process() must clear it even though to_json raises further down.
    def _stub_call_backend(self):
        os.environ["CT_RULE_OUTCOMES_LOG"] = str(tmp_path / "outcomes.log")

    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", _stub_call_backend)
    monkeypatch.delenv("CT_RULE_OUTCOMES_LOG", raising=False)

    class _PipelineBoom(RuntimeError):
        pass

    def _explode(self, path):
        raise _PipelineBoom("synthetic to_json failure")

    monkeypatch.setattr(BuildTimer, "to_json", _explode)

    cake = compiletools.cake.Cake(args)
    with pytest.raises(_PipelineBoom):
        cake.process()

    # The _env_var_restored context in process() must have restored the
    # pre-process state (absent) even though to_json raised on the way
    # out of the post-build pipeline.
    assert "CT_RULE_OUTCOMES_LOG" not in os.environ


def test_env_vars_restored_even_when_timer_disabled(monkeypatch, tmp_path):
    """Snapshot-restore env-var hygiene when timer is disabled.

    Both CCACHE_STATSLOG and CT_RULE_OUTCOMES_LOG are snapshot-restored
    (via _env_var_restored in process()), not popped. A pre-existing
    value supplied by an outer caller (here simulated via
    ``monkeypatch.setenv``) must be preserved across the process() call —
    Cake owns only the values it sets itself, and process() must be
    invariant with respect to caller-owned environment state regardless
    of timer.enabled.
    """
    _bindir, _objdir, argv = _bindir_objdir_argv(tmp_path, "--filename", "irrelevant.cpp")
    args = _build_args(argv)

    def _stub_createctobjs(self):
        self.hunter = object()

    monkeypatch.setattr(compiletools.cake.Cake, "_createctobjs", _stub_createctobjs)
    monkeypatch.setattr(compiletools.cake.Cake, "_call_backend", lambda self: None)

    # Simulate env vars set by some outer caller. Both must survive
    # process() unchanged.
    outcomes_log_value = str(tmp_path / "outcomes.log")
    monkeypatch.setenv("CT_RULE_OUTCOMES_LOG", outcomes_log_value)
    ccache_statslog_value = str(tmp_path / "ccache.log")
    monkeypatch.setenv("CCACHE_STATSLOG", ccache_statslog_value)

    cake = compiletools.cake.Cake(args)
    # Force the timer to be disabled for the finally block path.
    assert cake.context.timer is not None
    cake.context.timer.enabled = False

    cake.process()

    assert os.environ.get("CT_RULE_OUTCOMES_LOG") == outcomes_log_value
    assert os.environ.get("CCACHE_STATSLOG") == ccache_statslog_value


class TestOtelSdkEnvVarsDoNotLeakToArgs:
    """Three OTel flags must defer to the SDK as the env-var authority:
    configargparse must NOT promote OTEL_ENDPOINT (auto-uppercased),
    OTEL_EXPORTER_OTLP_ENDPOINT (SDK standard), or their trace-specific
    counterparts into args.otel_endpoint / otel_headers / otel_insecure.
    Otherwise we'd shadow the SDK's own precedence chain (which honours
    OTEL_EXPORTER_OTLP_TRACES_* over the generic forms).
    """

    def _parse(self, argv):
        cap = compiletools.apptools.create_parser("test otel env", argv=argv)
        compiletools.cake.Cake.add_arguments(cap)
        ns, _ = cap.parse_known_args(argv)
        return ns

    def test_otel_endpoint_env_does_not_leak_to_args(self, monkeypatch):
        monkeypatch.setenv("OTEL_ENDPOINT", "foo")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "bar")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "baz")
        ns = self._parse([])
        assert ns.otel_endpoint is None

    def test_otel_headers_env_does_not_leak_to_args(self, monkeypatch):
        monkeypatch.setenv("OTEL_HEADERS", "x=y")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "a=b")
        ns = self._parse([])
        assert ns.otel_headers is None

    def test_otel_insecure_env_does_not_leak_to_args(self, monkeypatch):
        monkeypatch.setenv("OTEL_INSECURE", "true")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_INSECURE", "true")
        ns = self._parse([])
        assert ns.otel_insecure is None

    def test_otel_service_name_env_does_not_leak_to_args(self, monkeypatch):
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
        ns = self._parse([])
        assert ns.otel_service_name is None

    def test_otel_resource_attr_env_does_not_leak_to_args(self, monkeypatch):
        monkeypatch.setenv("OTEL_RESOURCE_ATTR", "team=foo")
        ns = self._parse([])
        assert ns.otel_resource_attr == []

    def test_otel_cli_still_works(self):
        ns = self._parse(["--otel-endpoint=http://x", "--otel-headers=h=v", "--otel-insecure"])
        assert ns.otel_endpoint == "http://x"
        assert ns.otel_headers == "h=v"
        assert ns.otel_insecure is True

    def test_otel_insecure_tri_state_cli(self):
        # Absent: None so the SDK can infer from endpoint URL scheme.
        assert self._parse([]).otel_insecure is None
        assert self._parse(["--otel-insecure"]).otel_insecure is True
        assert self._parse(["--no-otel-insecure"]).otel_insecure is False

    def test_disabled_sentinel_hidden_from_help(self):
        from compiletools.utils import ENV_VAR_DISABLED

        cap = compiletools.apptools.create_parser("test otel env", argv=[])
        compiletools.cake.Cake.add_arguments(cap)
        help_text = cap.format_help()
        assert ENV_VAR_DISABLED not in help_text

    def test_env_var_disabled_truly_disabled_even_if_sentinel_in_env(self, monkeypatch):
        # Collision-proof: even if a real process exports the sentinel
        # name itself, configargparse's env-pickup loop must not see it.
        from compiletools.utils import ENV_VAR_DISABLED

        monkeypatch.setenv(ENV_VAR_DISABLED, "PICKED_UP")
        ns = self._parse([])
        assert ns.otel_endpoint is None
        assert ns.otel_headers is None
        assert ns.otel_insecure is None
        assert ns.otel_service_name is None
        assert ns.otel_resource_attr == []
