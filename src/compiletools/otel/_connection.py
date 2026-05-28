"""Shared OTLP transport + resource construction for compiletools.

Private to the ``compiletools.otel`` subpackage. Tests may import this
module directly; no other production caller should. The public surface
(``export_buildtimer``, future ``export_cache_metrics`` /
``export_ccache_metrics``) is re-exported from ``compiletools.otel``.
"""

from __future__ import annotations

import os
import socket
import subprocess
from typing import Any

MISSING_EXTRA_HINT = (
    "compiletools OTel export requested but the opentelemetry SDK is not "
    "installed. Install the optional extra: pip install 'compiletools[otel]'"
)

# Per-request timeout passed to both OTLP exporters. The SDK's
# BatchSpanProcessor.force_flush(timeout_millis=...) does NOT propagate its
# budget to the underlying synchronous HTTP/gRPC call (SDK source comment:
# "Not used. No way currently to pass timeout to export."), so the only
# real latency bound is the per-request timeout the exporter itself uses.
DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS = 5

# Paths the OTLP/HTTP receiver listens on; the SDK appends these only on the
# env-var fallback path (_append_trace_path / _append_metric_path), not on a
# constructor-supplied endpoint. Replicated here so an explicit
# --otel-endpoint base URL still reaches /v1/{traces,metrics} rather than
# POSTing to "/".
_OTLP_HTTP_TRACES_PATH = "v1/traces"
_OTLP_HTTP_METRICS_PATH = "v1/metrics"


def ensure_http_path(endpoint: str, *, signal: str) -> str:
    """Append the OTLP/HTTP signal path to a base endpoint when missing.

    ``signal`` is ``"traces"`` or ``"metrics"``. Idempotent.
    """
    if signal == "traces":
        suffix = _OTLP_HTTP_TRACES_PATH
    elif signal == "metrics":
        suffix = _OTLP_HTTP_METRICS_PATH
    else:
        raise ValueError(f"unknown OTLP signal: {signal!r}")
    if not endpoint:
        return endpoint
    path_only = endpoint.split("?", 1)[0].split("#", 1)[0]
    if path_only.endswith("/" + suffix) or path_only.endswith("/" + suffix + "/"):
        return endpoint
    if endpoint.endswith("/"):
        return endpoint + suffix
    return endpoint + "/" + suffix


def get_git_commit_sha(cwd: str | None = None) -> str:
    """Return the current HEAD commit SHA, or empty string on failure.

    Runs from *cwd* (the project gitroot when called from the exporter) so
    a ct-cake invoked from a subdirectory of an unrelated repo still
    reports the build's own commit, not the caller's.
    """
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            cwd=cwd,
        ).strip()
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return ""


def parse_kv_pairs(items) -> dict[str, str]:
    """Parse ``K=V`` or ``K=V,K=V`` strings into a dict.

    Accepts either a single string or an iterable of strings; silently
    skips malformed pieces (no ``=`` separator or empty key).
    """
    out: dict[str, str] = {}
    if not items:
        return out
    if isinstance(items, str):
        items = [items]
    for raw in items:
        if not raw:
            continue
        for piece in str(raw).split(","):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            k, v = piece.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                out[k] = v
    return out


def _invocation_id_from_diag_dir(args) -> str:
    """Return the per-invocation diagnostics-dir basename, or empty string.

    Side-effect-free: derives the basename from ``compiletools.diagnostics.invocation_id()``
    directly rather than calling ``resolve_diagnostics_dir(args)`` (which
    would create the diagnostics directory just to read its leaf).
    """
    try:
        from compiletools.diagnostics import invocation_id

        return invocation_id()
    except (AttributeError, OSError):
        # AttributeError: pre-init or stripped diagnostics module.
        # OSError: pid lookup / strftime failure on hostile systems.
        return ""


def build_resource(args):
    """Construct the OTel Resource for the root span.

    Precedence (low -> high): built-in fallback < OTEL_RESOURCE_ATTRIBUTES
    / OTEL_SERVICE_NAME env (resolved by ``Resource.create``) < explicit
    ``--otel-service-name`` CLI flag < ``--otel-resource-attr`` CLI values.

    Anything passed in the dict to ``Resource.create`` wins over env, so
    only seed a key when the user explicitly supplied it on the CLI — that
    way ``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES`` still take
    effect for whichever field the CLI flag leaves unset.  The literal
    ``"compiletools"`` fallback is applied AFTER the env-aware
    ``Resource.create`` only when both CLI and env are silent.
    """
    from opentelemetry.sdk.resources import Resource

    gitroot = resolve_gitroot(args)

    hostname = socket.gethostname()
    defaults: dict[str, Any] = {
        "service.namespace": "compiletools",
        "host.name": hostname,
        # OTel convention attribute disambiguating concurrent emitters on
        # the same host (e.g., cron-driven ct-cache-report colliding with
        # a manual run, or parallel ct-cake invocations). Hostname + pid
        # is unique-enough for the lifetime of any single observation set.
        "service.instance.id": f"{hostname}:{os.getpid()}",
        "git.commit.sha": get_git_commit_sha(cwd=gitroot),
        "ct.variant": getattr(args, "variant", "") or "",
        "ct.backend": getattr(args, "backend", "") or "",
        "ct.invocation_id": _invocation_id_from_diag_dir(args),
    }
    cli_service_name = getattr(args, "otel_service_name", None)
    if cli_service_name:
        defaults["service.name"] = cli_service_name

    # Drop empty values so Resource.create() doesn't emit blank attrs.
    defaults = {k: v for k, v in defaults.items() if v not in (None, "")}

    resource = Resource.create(defaults)

    # Final fallback: if neither CLI nor any env source supplied
    # service.name, OTel SDK uses its own "unknown_service" sentinel; we
    # override that to a friendlier default.
    resolved_service_name = str(resource.attributes.get("service.name", ""))
    if resolved_service_name.startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": "compiletools"}))

    cli_attrs = parse_kv_pairs(getattr(args, "otel_resource_attr", None))
    if cli_attrs:
        resource = resource.merge(Resource(cli_attrs))
    return resource


def resolve_gitroot(args) -> str | None:
    """Return the build's gitroot, or None when not in a git checkout."""
    explicit = getattr(args, "gitroot", None)
    if explicit:
        return explicit
    try:
        from compiletools.git_utils import find_git_root

        return find_git_root() or None
    except (ImportError, OSError):
        return None


def _build_processor(args):
    """Build a BatchSpanProcessor wrapping the configured OTLP exporter.

    Wraps the protocol-specific OTLP exporter import in the same friendly
    ``RuntimeError(MISSING_EXTRA_HINT)`` that ``export_buildtimer`` uses
    for the api/sdk hoist, so a partial install (api + sdk present but the
    requested wire exporter missing) yields the install hint rather than a
    raw ``ImportError``.
    """
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    protocol = (getattr(args, "otel_protocol", "grpc") or "grpc").lower()
    endpoint = getattr(args, "otel_endpoint", None)
    headers = parse_kv_pairs(getattr(args, "otel_headers", None))
    insecure = getattr(args, "otel_insecure", None)

    try:
        if protocol == "http":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    kwargs: dict[str, Any] = {"timeout": DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS}
    if endpoint:
        # The SDK only auto-appends /v1/traces on the env-var fallback path;
        # a constructor-supplied endpoint is used verbatim, so do it here.
        kwargs["endpoint"] = ensure_http_path(endpoint, signal="traces") if protocol == "http" else endpoint
    if headers:
        kwargs["headers"] = headers
    # gRPC-only: omit when None so the SDK's URL-scheme inference takes over
    # (http://... -> insecure True, https://... -> insecure False).
    if protocol != "http" and insecure is not None:
        kwargs["insecure"] = insecure
    exporter = OTLPSpanExporter(**kwargs)

    return BatchSpanProcessor(exporter)


def _build_metric_reader(args):
    """Build a one-shot MetricReader wrapping the configured OTLP metric exporter.

    Mirrors ``_build_processor`` but for the metric pipeline. The reader
    returned is a ``PeriodicExportingMetricReader``; the metrics caller is
    responsible for triggering a flush + shutdown immediately after
    populating its gauges (this module is one-shot, not long-running).
    Same friendly ``RuntimeError(MISSING_EXTRA_HINT)`` semantics as the
    trace processor for partial-install detection.
    """
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    protocol = (getattr(args, "otel_protocol", "grpc") or "grpc").lower()
    endpoint = getattr(args, "otel_endpoint", None)
    headers = parse_kv_pairs(getattr(args, "otel_headers", None))
    insecure = getattr(args, "otel_insecure", None)

    try:
        if protocol == "http":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    kwargs: dict[str, Any] = {"timeout": DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS}
    if endpoint:
        # The SDK only auto-appends /v1/metrics on the env-var fallback path;
        # a constructor-supplied endpoint is used verbatim, so do it here.
        kwargs["endpoint"] = ensure_http_path(endpoint, signal="metrics") if protocol == "http" else endpoint
    if headers:
        kwargs["headers"] = headers
    # gRPC-only: omit when None so the SDK's URL-scheme inference takes over.
    if protocol != "http" and insecure is not None:
        kwargs["insecure"] = insecure
    exporter = OTLPMetricExporter(**kwargs)

    # ct-cache-report is one-shot — pick a very long collection interval so
    # the periodic reader doesn't fire a stray mid-run export; the caller
    # triggers force_flush() + shutdown() once gauges are populated.
    return PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=24 * 60 * 60 * 1000,  # 24h, effectively "never auto-fire"
    )
