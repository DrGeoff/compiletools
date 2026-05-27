"""Tests for the OpenTelemetry / OTLP exporter."""

from __future__ import annotations

import sys
import types

import pytest

# importorskip the SDK, not the bare ``opentelemetry`` namespace package:
# ``opentelemetry-api`` (a common transitive dependency) satisfies the PEP 420
# namespace import on its own, so ``importorskip("opentelemetry")`` would pass
# in an api-only environment and then the module-level ``opentelemetry.sdk``
# import below would raise ModuleNotFoundError -- a collection error that
# interrupts the whole pytest run rather than skipping just this module.
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from compiletools.build_timer import BuildTimer
from compiletools.otel_exporter import (
    _MISSING_EXTRA_HINT,
    _parse_kv_pairs,
    _to_wall_ns,
    export_buildtimer,
)

# ----------------------------------------------------------------- test helpers


def _make_args(**overrides):
    """Tiny stand-in for an argparse Namespace."""
    defaults = dict(
        otel_service_name=None,
        otel_endpoint=None,
        otel_resource_attr=[],
        otel_protocol="grpc",
        otel_headers=None,
        otel_insecure=None,
        variant="gcc.debug",
        backend="ninja",
        diagnostics_dir=None,
        bindir=None,
    )
    defaults.update(overrides)
    ns = types.SimpleNamespace(**defaults)
    return ns


def _make_timer_with_tree() -> BuildTimer:
    """Build a synthetic finished BuildTimer with two phases + a few rules."""
    timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
    base = timer._root.start_s
    with timer.phase("build_graph"):
        pass
    with timer.phase("build_execution"):
        timer.record_rule(
            rule_type="compile",
            target="obj/foo.o",
            source="src/foo.cpp",
            elapsed_s=0.5,
            start_s=base + 1.0,
            end_s=base + 1.5,
        )
        timer.record_rule(
            rule_type="compile",
            target="obj/bar.o",
            source="src/bar.cpp",
            elapsed_s=0.25,
            start_s=base + 1.5,
            end_s=base + 1.75,
        )
        timer.record_rule(
            rule_type="link",
            target="bin/app",
            source="",
            elapsed_s=0.1,
            start_s=base + 1.75,
            end_s=base + 1.85,
        )
    timer.finish()
    return timer


def _export_into_memory(timer: BuildTimer, args) -> list:
    """Run the exporter with an in-memory backend and return the spans."""
    sink = InMemorySpanExporter()
    export_buildtimer(timer, args, _processor=SimpleSpanProcessor(sink))
    return list(sink.get_finished_spans())


# ----------------------------------------------------------------- _parse_kv_pairs


class TestParseKvPairs:
    def test_empty(self):
        assert _parse_kv_pairs(None) == {}
        assert _parse_kv_pairs([]) == {}
        assert _parse_kv_pairs("") == {}

    def test_single_string(self):
        assert _parse_kv_pairs("a=1") == {"a": "1"}

    def test_comma_separated(self):
        assert _parse_kv_pairs("a=1,b=2") == {"a": "1", "b": "2"}

    def test_list_of_strings(self):
        assert _parse_kv_pairs(["a=1", "b=2,c=3"]) == {"a": "1", "b": "2", "c": "3"}

    def test_drops_malformed(self):
        assert _parse_kv_pairs(["junk", "=novalue", "a=1"]) == {"a": "1"}

    def test_value_can_be_empty(self):
        assert _parse_kv_pairs("a=") == {"a": ""}


# ----------------------------------------------------------------- _to_wall_ns


def test_to_wall_ns_subtracts_offset():
    # offset = monotonic - wall; wall_ns = (monotonic - offset) * 1e9
    assert _to_wall_ns(monotonic_s=100.5, mono_to_wall_offset=50.0) == int(50.5 * 1e9)


# ----------------------------------------------------------------- export round-trip


class TestExportRoundTrip:
    def test_emits_root_phase_and_rule_spans(self, monkeypatch):
        monkeypatch.setattr("socket.gethostname", lambda: "test-host")
        monkeypatch.setattr(
            "compiletools.otel_exporter._get_git_commit_sha",
            lambda cwd=None: "abc123",
        )
        monkeypatch.setattr(
            "compiletools.otel_exporter._invocation_id_from_diag_dir",
            lambda args: "inv-001",
        )

        timer = _make_timer_with_tree()
        spans = _export_into_memory(timer, _make_args())

        names = [s.name for s in spans]
        assert "compiletools.build" in names
        assert "phase.build_graph" in names
        assert "phase.build_execution" in names
        # Rule span names include the parent-dir basename to disambiguate
        # files that share a basename.
        assert "compile.src/foo.cpp" in names
        assert "compile.src/bar.cpp" in names
        assert "link.bin/app" in names

    def test_rule_attributes_are_set(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr(
            "compiletools.otel_exporter._invocation_id_from_diag_dir",
            lambda args: "",
        )

        timer = _make_timer_with_tree()
        spans = _export_into_memory(timer, _make_args())
        by_name = {s.name: s for s in spans}

        foo = by_name["compile.src/foo.cpp"]
        assert foo.attributes["ct.rule_type"] == "compile"
        assert foo.attributes["ct.target"] == "obj/foo.o"
        assert foo.attributes["ct.source"] == "src/foo.cpp"

        link = by_name["link.bin/app"]
        assert link.attributes["ct.rule_type"] == "link"
        assert link.attributes["ct.target"] == "bin/app"
        # link has no source in the synthetic tree
        assert "ct.source" not in link.attributes

    def test_root_resource_attributes(self, monkeypatch):
        monkeypatch.setattr("socket.gethostname", lambda: "test-host")
        monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "deadbeef")
        monkeypatch.setattr(
            "compiletools.otel_exporter._invocation_id_from_diag_dir",
            lambda args: "inv-xyz",
        )

        timer = _make_timer_with_tree()
        spans = _export_into_memory(timer, _make_args())
        root = next(s for s in spans if s.name == "compiletools.build")

        attrs = dict(root.resource.attributes)
        assert attrs["service.name"] == "compiletools"
        assert attrs["service.namespace"] == "compiletools"
        assert attrs["host.name"] == "test-host"
        assert attrs["git.commit.sha"] == "deadbeef"
        assert attrs["ct.variant"] == "gcc.debug"
        assert attrs["ct.backend"] == "ninja"
        assert attrs["ct.invocation_id"] == "inv-xyz"

    def test_parent_child_links_form_tree(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr(
            "compiletools.otel_exporter._invocation_id_from_diag_dir",
            lambda args: "",
        )

        timer = _make_timer_with_tree()
        spans = _export_into_memory(timer, _make_args())
        by_name = {s.name: s for s in spans}

        root = by_name["compiletools.build"]
        build_exec = by_name["phase.build_execution"]
        foo = by_name["compile.src/foo.cpp"]

        # phase.build_execution's parent is compiletools.build
        assert build_exec.parent is not None
        assert build_exec.parent.span_id == root.context.span_id
        # compile.foo.cpp's parent is phase.build_execution
        assert foo.parent is not None
        assert foo.parent.span_id == build_exec.context.span_id
        # all spans share one trace id
        trace_ids = {s.context.trace_id for s in spans}
        assert len(trace_ids) == 1

    def test_timestamps_round_trip_via_wall_offset(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr(
            "compiletools.otel_exporter._invocation_id_from_diag_dir",
            lambda args: "",
        )

        timer = _make_timer_with_tree()
        offset = timer._wall_to_monotonic_offset
        # find the compile.src/foo.cpp event's monotonic start, in the same
        # synthetic tree we built
        compile_event = next(ev for phase in timer._root.children for ev in phase.children if ev.target == "obj/foo.o")
        # The synthetic tree always sets end_s; narrow the Optional for pyright.
        assert compile_event.end_s is not None
        expected_start_ns = int((compile_event.start_s - offset) * 1e9)
        expected_end_ns = int((compile_event.end_s - offset) * 1e9)

        spans = _export_into_memory(timer, _make_args())
        foo = next(s for s in spans if s.name == "compile.src/foo.cpp")
        assert foo.start_time == expected_start_ns
        assert foo.end_time == expected_end_ns


# ----------------------------------------------------------------- disabled timer


def test_disabled_timer_emits_nothing():
    timer = BuildTimer(enabled=False)
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(), _processor=SimpleSpanProcessor(sink))
    assert sink.get_finished_spans() == ()


# ----------------------------------------------------------------- missing extra


def test_missing_extra_raises_with_install_hint(monkeypatch):
    """When opentelemetry imports fail, raise RuntimeError with the hint."""
    # Force the lazy imports inside export_buildtimer to fail.  Setting
    # the package to None makes `from opentelemetry import ...` raise
    # ModuleNotFoundError on the next import attempt.
    saved = {k: v for k, v in sys.modules.items() if k.startswith("opentelemetry")}
    try:
        for k in list(sys.modules):
            if k.startswith("opentelemetry"):
                del sys.modules[k]
        monkeypatch.setitem(sys.modules, "opentelemetry", None)

        timer = BuildTimer(enabled=True)
        with pytest.raises(RuntimeError) as excinfo:
            export_buildtimer(timer, _make_args())
        assert _MISSING_EXTRA_HINT in str(excinfo.value)
        assert "compiletools[otel]" in str(excinfo.value)
    finally:
        # Restore so later tests can still import the SDK.
        sys.modules.pop("opentelemetry", None)
        sys.modules.update(saved)


# ----------------------------------------------------------------- determinism


def test_round_trip_determinism(monkeypatch):
    """Exporting the same BuildTimer twice yields the same span shape.

    Span/trace IDs differ between runs (the SDK uses a random ID
    generator), but span names, parent linkage, attribute dicts, and
    timestamps must be stable.
    """
    monkeypatch.setattr("socket.gethostname", lambda: "test-host")
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "abc123")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "inv-001",
    )

    timer = _make_timer_with_tree()
    args = _make_args()

    def fingerprint(spans):
        # ID-independent fingerprint: (name, parent_name, attrs, start, end).
        by_id = {s.context.span_id: s.name for s in spans}
        rows = []
        for s in spans:
            parent_name = by_id.get(s.parent.span_id) if s.parent else None
            rows.append(
                (
                    s.name,
                    parent_name,
                    tuple(sorted(dict(s.attributes).items())),
                    s.start_time,
                    s.end_time,
                )
            )
        return sorted(rows)

    spans_a = _export_into_memory(timer, args)
    spans_b = _export_into_memory(timer, args)
    assert fingerprint(spans_a) == fingerprint(spans_b)


# --------------------------------------------------------------- partial install


def test_missing_grpc_exporter_raises_with_install_hint(monkeypatch):
    """Partial install (api+sdk present, OTLP gRPC exporter missing) hint."""
    # Block the gRPC OTLP exporter import while leaving api/sdk intact.
    grpc_mod = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    saved = sys.modules.get(grpc_mod)
    try:
        sys.modules.pop(grpc_mod, None)
        monkeypatch.setitem(sys.modules, grpc_mod, None)

        timer = _make_timer_with_tree()
        with pytest.raises(RuntimeError) as excinfo:
            # No _processor passed: real _build_processor() runs and trips
            # the partial-install import.
            export_buildtimer(timer, _make_args())
        assert _MISSING_EXTRA_HINT in str(excinfo.value)
        assert "compiletools[otel]" in str(excinfo.value)
    finally:
        sys.modules.pop(grpc_mod, None)
        if saved is not None:
            sys.modules[grpc_mod] = saved


# --------------------------------------------------------------- service.name precedence


def test_service_name_env_wins_when_cli_unset(monkeypatch):
    """OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES must beat the literal default."""
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=from-env")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_timer_with_tree()
    spans = _export_into_memory(timer, _make_args(otel_service_name=None))
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "from-env"


def test_service_name_cli_wins_over_env(monkeypatch):
    """An explicit --otel-service-name still beats OTEL_SERVICE_NAME."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_timer_with_tree()
    spans = _export_into_memory(timer, _make_args(otel_service_name="from-cli"))
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "from-cli"


def test_service_name_fallback_when_neither_set(monkeypatch):
    """With no CLI and no env, the literal ``compiletools`` fallback applies."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_timer_with_tree()
    spans = _export_into_memory(timer, _make_args(otel_service_name=None))
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "compiletools"


# --------------------------------------------------------------- root span isolation


def test_root_span_has_no_parent_even_with_ambient_context(monkeypatch):
    """compiletools.build is always a root, even when a span is active upstream."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    ambient_provider = TracerProvider()
    ambient_tracer = ambient_provider.get_tracer("ambient-wrapper")
    with ambient_tracer.start_as_current_span("upstream-wrapper"):
        timer = _make_timer_with_tree()
        spans = _export_into_memory(timer, _make_args())
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.parent is None, "compiletools.build must always be a root span"
    # Sanity: the wrapper tracer's notion of a current span did exist.
    assert trace.get_current_span() is trace.INVALID_SPAN


# --------------------------------------------------------------- error containment


def test_root_span_ends_even_when_child_emission_raises(monkeypatch):
    """A child exception must not abandon the parent span unflushed."""
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_timer_with_tree()
    sink = InMemorySpanExporter()
    boom_count = {"n": 0}

    real = __import__("compiletools.otel_exporter", fromlist=["_emit_event"])._emit_event

    def maybe_boom(tracer, event, parent_ctx, offset):
        # Raise inside the first compile event's emission, after its parent
        # phase span has been started.
        if event.category == "compile" and boom_count["n"] == 0:
            boom_count["n"] += 1
            raise RuntimeError("synthetic child failure")
        return real(tracer, event, parent_ctx, offset)

    monkeypatch.setattr("compiletools.otel_exporter._emit_event", maybe_boom)
    try:
        export_buildtimer(timer, _make_args(), _processor=SimpleSpanProcessor(sink))
    except RuntimeError:
        pass

    names = {s.name for s in sink.get_finished_spans()}
    # The root and the partially-emitted phase must both have been ended
    # (and therefore flushed) despite the child exception.
    assert "compiletools.build" in names
    assert "phase.build_execution" in names


# --------------------------------------------------------------- flush warning


def test_flush_timeout_warns_to_stderr(monkeypatch, capsys):
    """If force_flush() returns False, emit a stderr warning (no raise)."""

    class TimeoutProcessor(SimpleSpanProcessor):
        def force_flush(self, timeout_millis=None):
            return False

    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_timer_with_tree()
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(), _processor=TimeoutProcessor(sink))
    captured = capsys.readouterr()
    assert "timed out flushing spans" in captured.err


# --------------------------------------------------------------- collision disambiguation


def test_rule_with_default_start_sentinel_is_skipped(monkeypatch):
    """Slurm rules whose sacct timestamps were unparseable land with
    ``start_s == 0.0`` (the ``record_rule`` default); emitting those would
    drag the trace to ~1970.  They should be skipped instead.
    """
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = BuildTimer(enabled=True, variant="gcc.debug", backend="slurm")
    base = timer._root.start_s
    with timer.phase("build_execution"):
        # Good rule (has explicit start/end)
        timer.record_rule(
            rule_type="compile",
            target="obj/good.o",
            source="src/good.cpp",
            elapsed_s=0.1,
            start_s=base + 1.0,
            end_s=base + 1.1,
        )
        # Bad rule: omit start/end so record_rule defaults start_s to 0.0
        # (the Slurm "Unknown sacct timestamp" code path).
        timer.record_rule(
            rule_type="compile",
            target="obj/orphan.o",
            source="src/orphan.cpp",
            elapsed_s=0.1,
        )
    timer.finish()
    spans = _export_into_memory(timer, _make_args())
    names = {s.name for s in spans}
    assert "compile.src/good.cpp" in names
    assert "compile.src/orphan.cpp" not in names


# --------------------------------------------------------- exporter constructor kwargs


def _capture_exporter_kwargs(monkeypatch, protocol: str) -> dict:
    """Build a processor for *protocol* and return the kwargs the OTLPSpanExporter saw.

    Patches the real OTLPSpanExporter class in the right submodule so
    ``_build_processor``'s ``from ... import OTLPSpanExporter`` resolves
    to a capturing stand-in that records its kwargs and returns a stub
    object with the SpanExporter surface BatchSpanProcessor needs.
    """
    captured: dict = {}

    class _CapturingExporter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        # SpanExporter surface the BatchSpanProcessor pokes at.
        def export(self, spans):
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    if protocol == "http":
        mod = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    else:
        mod = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"

    import importlib

    real_mod = importlib.import_module(mod)
    monkeypatch.setattr(real_mod, "OTLPSpanExporter", _CapturingExporter)
    return captured


class TestHttpEndpointTracesPath:
    """Finding A: --otel-protocol=http endpoints must reach /v1/traces."""

    def test_http_base_url_gets_traces_path_appended(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_http_url_with_explicit_traces_path_is_unchanged(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318/v1/traces")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_http_url_with_trailing_slash(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318/")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_https_honeycomb_url_with_traces_path_is_unchanged(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="https://api.honeycomb.io/v1/traces")
        _build_processor(args)
        assert captured["endpoint"] == "https://api.honeycomb.io/v1/traces"

    def test_grpc_endpoint_is_not_modified(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(otel_protocol="grpc", otel_endpoint="http://tempo:4317")
        _build_processor(args)
        assert captured["endpoint"] == "http://tempo:4317"


# --------------------------------------------------------- exporter request timeout


class TestExporterRequestTimeout:
    """Finding B: a real per-request timeout must be set on both exporters."""

    def test_http_exporter_has_request_timeout(self, monkeypatch):
        from compiletools.otel_exporter import (
            _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS,
            _build_processor,
        )

        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318")
        _build_processor(args)
        assert captured["timeout"] == _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS
        assert captured["timeout"] == 5

    def test_grpc_exporter_has_request_timeout(self, monkeypatch):
        from compiletools.otel_exporter import (
            _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS,
            _build_processor,
        )

        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(otel_protocol="grpc", otel_endpoint="http://tempo:4317")
        _build_processor(args)
        assert captured["timeout"] == _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS
        assert captured["timeout"] == 5


class TestGrpcInsecureTriState:
    """gRPC ``insecure`` kwarg honours the tri-state of args.otel_insecure."""

    def test_grpc_insecure_omitted_when_args_is_none(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=None,
        )
        _build_processor(args)
        assert "insecure" not in captured

    def test_grpc_insecure_true_when_args_true(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=True,
        )
        _build_processor(args)
        assert captured["insecure"] is True

    def test_grpc_insecure_false_when_args_false(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=False,
        )
        _build_processor(args)
        assert captured["insecure"] is False

    def test_http_never_passes_insecure(self, monkeypatch):
        from compiletools.otel_exporter import _build_processor

        for value in (None, True, False):
            captured = _capture_exporter_kwargs(monkeypatch, "http")
            args = _make_args(
                otel_protocol="http",
                otel_endpoint="http://localhost:4318",
                otel_insecure=value,
            )
            _build_processor(args)
            assert "insecure" not in captured, f"http branch leaked insecure for {value!r}"


def test_same_basename_in_different_dirs_get_distinct_span_names(monkeypatch):
    """src/util.cpp and tests/util.cpp must not collide on span name."""
    monkeypatch.setattr("compiletools.otel_exporter._get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel_exporter._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
    base = timer._root.start_s
    with timer.phase("build_execution"):
        timer.record_rule(
            rule_type="compile",
            target="obj/src_util.o",
            source="src/util.cpp",
            elapsed_s=0.1,
            start_s=base + 1.0,
            end_s=base + 1.1,
        )
        timer.record_rule(
            rule_type="compile",
            target="obj/tests_util.o",
            source="tests/util.cpp",
            elapsed_s=0.1,
            start_s=base + 1.1,
            end_s=base + 1.2,
        )
    timer.finish()
    spans = _export_into_memory(timer, _make_args())
    names = [s.name for s in spans]
    assert "compile.src/util.cpp" in names
    assert "compile.tests/util.cpp" in names
