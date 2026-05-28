"""Tests for resource construction and transport helpers in compiletools.otel._connection."""

from __future__ import annotations

import sys
import types

import pytest

# importorskip the SDK, not the bare ``opentelemetry`` namespace package:
# ``opentelemetry`` is a PEP 420 implicit namespace package (no top-level
# ``__init__.py``; ``opentelemetry.__file__`` is ``None`` and ``__path__`` is
# a ``_NamespacePath``), and the ``opentelemetry-api`` distribution (a common
# transitive dependency) ships portion subpackages under ``opentelemetry/``,
# which is enough for ``import opentelemetry`` to succeed. So a bare
# ``importorskip("opentelemetry")`` would pass in an api-only environment and
# then the module-level ``opentelemetry.sdk`` import below would raise
# ModuleNotFoundError -- a collection error that interrupts the whole pytest
# run rather than skipping just this module.
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from compiletools.build_timer import BuildTimer
from compiletools.otel._connection import (
    DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS as _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS,
)
from compiletools.otel._connection import (
    MISSING_EXTRA_HINT as _MISSING_EXTRA_HINT,
)
from compiletools.otel._connection import (
    _build_processor,
)
from compiletools.otel._connection import (
    parse_kv_pairs as _parse_kv_pairs,
)
from compiletools.otel.traces import export_buildtimer

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


def _make_minimal_timer() -> BuildTimer:
    """Minimal finished BuildTimer sufficient to emit a root span via export_buildtimer."""
    timer = BuildTimer(enabled=True)
    timer.finish()
    return timer


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


# --------------------------------------------------------------- partial install


def test_missing_grpc_exporter_raises_with_install_hint(monkeypatch):
    """Partial install (api+sdk present, OTLP gRPC exporter missing) hint."""
    # Block the gRPC OTLP exporter import while leaving api/sdk intact.
    grpc_mod = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    saved = sys.modules.get(grpc_mod)
    try:
        sys.modules.pop(grpc_mod, None)
        monkeypatch.setitem(sys.modules, grpc_mod, None)

        timer = _make_minimal_timer()
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


def test_service_instance_id_is_hostname_and_pid(monkeypatch):
    """service.instance.id must be f'{hostname}:{pid}' — the OTel convention
    attribute that disambiguates concurrent emitters on the same host
    (e.g., cron-driven ct-cache-report colliding with a manual run, or
    parallel ct-cake invocations).
    """
    import os as _os

    monkeypatch.setattr("socket.gethostname", lambda: "fake-host")
    monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel._connection._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_minimal_timer()
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(), _processor=SimpleSpanProcessor(sink))
    spans = list(sink.get_finished_spans())
    root = next(s for s in spans if s.name == "compiletools.build")
    attrs = dict(root.resource.attributes)
    assert "service.instance.id" in attrs
    assert attrs["service.instance.id"] == f"fake-host:{_os.getpid()}"


def test_service_name_env_wins_when_cli_unset(monkeypatch):
    """OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES must beat the literal default."""
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=from-env")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel._connection._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_minimal_timer()
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(otel_service_name=None), _processor=SimpleSpanProcessor(sink))
    spans = list(sink.get_finished_spans())
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "from-env"


def test_service_name_cli_wins_over_env(monkeypatch):
    """An explicit --otel-service-name still beats OTEL_SERVICE_NAME."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
    monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel._connection._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_minimal_timer()
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(otel_service_name="from-cli"), _processor=SimpleSpanProcessor(sink))
    spans = list(sink.get_finished_spans())
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "from-cli"


def test_service_name_fallback_when_neither_set(monkeypatch):
    """With no CLI and no env, the literal ``compiletools`` fallback applies."""
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
    monkeypatch.setattr(
        "compiletools.otel._connection._invocation_id_from_diag_dir",
        lambda args: "",
    )

    timer = _make_minimal_timer()
    sink = InMemorySpanExporter()
    export_buildtimer(timer, _make_args(otel_service_name=None), _processor=SimpleSpanProcessor(sink))
    spans = list(sink.get_finished_spans())
    root = next(s for s in spans if s.name == "compiletools.build")
    assert root.resource.attributes["service.name"] == "compiletools"


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
        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_http_url_with_explicit_traces_path_is_unchanged(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318/v1/traces")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_http_url_with_trailing_slash(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318/")
        _build_processor(args)
        assert captured["endpoint"] == "http://localhost:4318/v1/traces"

    def test_https_honeycomb_url_with_traces_path_is_unchanged(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="https://api.honeycomb.io/v1/traces")
        _build_processor(args)
        assert captured["endpoint"] == "https://api.honeycomb.io/v1/traces"

    def test_grpc_endpoint_is_not_modified(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(otel_protocol="grpc", otel_endpoint="http://tempo:4317")
        _build_processor(args)
        assert captured["endpoint"] == "http://tempo:4317"


# --------------------------------------------------------- exporter request timeout


class TestExporterRequestTimeout:
    """Finding B: a real per-request timeout must be set on both exporters."""

    def test_http_exporter_has_request_timeout(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "http")
        args = _make_args(otel_protocol="http", otel_endpoint="http://localhost:4318")
        _build_processor(args)
        assert captured["timeout"] == _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS
        assert captured["timeout"] == 5

    def test_grpc_exporter_has_request_timeout(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(otel_protocol="grpc", otel_endpoint="http://tempo:4317")
        _build_processor(args)
        assert captured["timeout"] == _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS
        assert captured["timeout"] == 5


class TestGrpcInsecureTriState:
    """gRPC ``insecure`` kwarg honours the tri-state of args.otel_insecure."""

    def test_grpc_insecure_omitted_when_args_is_none(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=None,
        )
        _build_processor(args)
        assert "insecure" not in captured

    def test_grpc_insecure_true_when_args_true(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=True,
        )
        _build_processor(args)
        assert captured["insecure"] is True

    def test_grpc_insecure_false_when_args_false(self, monkeypatch):
        captured = _capture_exporter_kwargs(monkeypatch, "grpc")
        args = _make_args(
            otel_protocol="grpc",
            otel_endpoint="http://collector:4317",
            otel_insecure=False,
        )
        _build_processor(args)
        assert captured["insecure"] is False

    def test_http_never_passes_insecure(self, monkeypatch):
        for value in (None, True, False):
            captured = _capture_exporter_kwargs(monkeypatch, "http")
            args = _make_args(
                otel_protocol="http",
                otel_endpoint="http://localhost:4318",
                otel_insecure=value,
            )
            _build_processor(args)
            assert "insecure" not in captured, f"http branch leaked insecure for {value!r}"
