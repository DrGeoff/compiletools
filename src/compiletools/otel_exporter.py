"""End-of-build OpenTelemetry (OTLP) exporter for BuildTimer.

Walks the in-memory :class:`~compiletools.build_timer.BuildTimer` tree
once the build has finished and ships one OTel span per
``TimingEvent`` to an OTLP collector.  Timestamps are converted from
BuildTimer's monotonic clock to wall-clock nanoseconds using the
offset captured at ``BuildTimer.__init__``, so spans align with
everything else in the user's observability stack.

The OpenTelemetry SDK is imported lazily.  Install the optional
``otel`` extra (``pip install 'compiletools[otel]'``) to enable; with
the flag off and the extra missing, this module's import is free.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from compiletools.build_timer import BuildTimer, TimingEvent


_MISSING_EXTRA_HINT = (
    "compiletools OTel export requested but the opentelemetry SDK is not "
    "installed. Install the optional extra: pip install 'compiletools[otel]'"
)

# Per-request timeout passed to both OTLP exporters. The SDK's
# BatchSpanProcessor.force_flush(timeout_millis=...) does NOT propagate its
# budget to the underlying synchronous HTTP/gRPC call (SDK source comment:
# "Not used. No way currently to pass timeout to export."), so the only
# real latency bound is the per-request timeout the exporter itself uses.
_DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS = 5

# Path the OTLP/HTTP receiver listens on; the SDK appends this only on the
# env-var fallback path (_append_trace_path), not on a constructor-supplied
# endpoint. Replicated here so an explicit --otel-endpoint base URL still
# reaches /v1/traces rather than POSTing to "/".
_OTLP_HTTP_TRACES_PATH = "v1/traces"


def _ensure_http_traces_path(endpoint: str) -> str:
    """Append ``/v1/traces`` to a base OTLP/HTTP endpoint when missing.

    Mirrors the SDK's private ``_append_trace_path`` (handles trailing
    slash) and is idempotent: if the URL already targets the traces path
    it is returned unchanged.
    """
    if not endpoint:
        return endpoint
    # Strip a query/fragment for the suffix check so "...?k=v" still matches.
    path_only = endpoint.split("?", 1)[0].split("#", 1)[0]
    if path_only.endswith("/" + _OTLP_HTTP_TRACES_PATH) or path_only.endswith("/" + _OTLP_HTTP_TRACES_PATH + "/"):
        return endpoint
    if endpoint.endswith("/"):
        return endpoint + _OTLP_HTTP_TRACES_PATH
    return endpoint + "/" + _OTLP_HTTP_TRACES_PATH


def _get_git_commit_sha(cwd: str | None = None) -> str:
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


def _parse_kv_pairs(items) -> dict[str, str]:
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


def _build_resource(args):
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

    gitroot = _resolve_gitroot(args)

    defaults: dict[str, Any] = {
        "service.namespace": "compiletools",
        "host.name": socket.gethostname(),
        "git.commit.sha": _get_git_commit_sha(cwd=gitroot),
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

    cli_attrs = _parse_kv_pairs(getattr(args, "otel_resource_attr", None))
    if cli_attrs:
        resource = resource.merge(Resource(cli_attrs))
    return resource


def _resolve_gitroot(args) -> str | None:
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
    ``RuntimeError(_MISSING_EXTRA_HINT)`` that ``export_buildtimer`` uses
    for the api/sdk hoist, so a partial install (api + sdk present but the
    requested wire exporter missing) yields the install hint rather than a
    raw ``ImportError``.
    """
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    protocol = (getattr(args, "otel_protocol", "grpc") or "grpc").lower()
    endpoint = getattr(args, "otel_endpoint", None)
    headers = _parse_kv_pairs(getattr(args, "otel_headers", None))
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
        raise RuntimeError(_MISSING_EXTRA_HINT) from exc

    kwargs: dict[str, Any] = {"timeout": _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS}
    if endpoint:
        # The SDK only auto-appends /v1/traces on the env-var fallback path;
        # a constructor-supplied endpoint is used verbatim, so do it here.
        kwargs["endpoint"] = _ensure_http_traces_path(endpoint) if protocol == "http" else endpoint
    if headers:
        kwargs["headers"] = headers
    # gRPC-only: omit when None so the SDK's URL-scheme inference takes over
    # (http://... -> insecure True, https://... -> insecure False).
    if protocol != "http" and insecure is not None:
        kwargs["insecure"] = insecure
    exporter = OTLPSpanExporter(**kwargs)

    return BatchSpanProcessor(exporter)


def _to_wall_ns(monotonic_s: float, mono_to_wall_offset: float) -> int:
    """Convert BuildTimer's ``time.monotonic()`` seconds to wall-clock ns."""
    return int((monotonic_s - mono_to_wall_offset) * 1_000_000_000)


def _rule_span_name(event: TimingEvent) -> str:
    """Build a rule span name, disambiguating siblings that share a basename.

    ``event.name`` is the basename of source-or-target; two TUs called
    ``util.cpp`` in different directories would otherwise produce
    identical span names.  Prepend the parent-directory basename when
    present so ``src/util.cpp`` and ``tests/util.cpp`` show up as
    ``compile.src/util.cpp`` and ``compile.tests/util.cpp`` in the trace
    UI.  ``ct.target`` / ``ct.source`` attributes remain the canonical
    query keys.
    """
    disambiguator = event.source or event.target
    if disambiguator:
        parent = os.path.basename(os.path.dirname(disambiguator))
        if parent:
            return f"{event.category}.{parent}/{event.name}"
    return f"{event.category}.{event.name}"


def _emit_event(tracer, event: TimingEvent, parent_ctx, mono_to_wall_offset: float) -> None:
    """Recursively emit OTel spans for a TimingEvent subtree.

    Rule events whose ``start_s`` is the ``record_rule`` default-sentinel
    of 0.0 (Slurm jobs with unparseable sacct timestamps land here) would
    otherwise emit spans dated decades before the build, dragging the
    trace timeline to boot-time wall clock.  Skip them entirely; the
    rule's elapsed time is still in ``timing.json`` for offline review.
    """
    from opentelemetry import trace

    if event.category != "phase" and event.start_s == 0.0:
        return

    if event.category == "phase":
        name = f"phase.{event.name}"
    else:
        name = _rule_span_name(event)

    start_ns = _to_wall_ns(event.start_s, mono_to_wall_offset)
    end_s = event.end_s if event.end_s is not None else event.start_s + event.elapsed_s
    end_ns = _to_wall_ns(end_s, mono_to_wall_offset)

    span = tracer.start_span(name, context=parent_ctx, start_time=start_ns)
    try:
        if event.category != "phase":
            span.set_attribute("ct.rule_type", event.category)
        if event.target:
            span.set_attribute("ct.target", event.target)
        if event.source:
            span.set_attribute("ct.source", event.source)

        if event.children:
            child_ctx = trace.set_span_in_context(span, parent_ctx)
            for child in event.children:
                _emit_event(tracer, child, child_ctx, mono_to_wall_offset)
    finally:
        # End the span even when a child emission raises; otherwise the
        # BatchSpanProcessor never flushes the parent and the whole subtree
        # is silently dropped.
        span.end(end_time=end_ns)


def export_buildtimer(timer: BuildTimer, args, *, _processor=None) -> None:
    """Export a finished BuildTimer's span tree via OTLP.

    Emits one OTel span per ``TimingEvent`` (root + phases + rules),
    preserving parent/child via SDK Context propagation.  All timestamps
    are wall-clock nanoseconds derived from BuildTimer's monotonic
    timeline via ``BuildTimer._wall_to_monotonic_offset``, so the
    resulting trace lines up with the rest of the user's observability
    data.

    Raises ``RuntimeError`` if the optional ``otel`` extra isn't
    installed.  ``_processor`` is a test seam: pass a
    ``SimpleSpanProcessor(InMemorySpanExporter())`` to capture spans
    in-memory instead of shipping over the network.
    """
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        raise RuntimeError(_MISSING_EXTRA_HINT) from exc

    if not timer.enabled:
        return

    timer.finish()

    resource = _build_resource(args)
    provider = TracerProvider(resource=resource)
    processor = _processor if _processor is not None else _build_processor(args)
    provider.add_span_processor(processor)

    tracer = provider.get_tracer("compiletools")

    mono_to_wall_offset = timer._wall_to_monotonic_offset
    root_start_ns = _to_wall_ns(timer._root.start_s, mono_to_wall_offset)
    root_end_s = timer._root.end_s if timer._root.end_s is not None else timer._root.start_s
    root_end_ns = _to_wall_ns(root_end_s, mono_to_wall_offset)

    # Force a fresh root: an empty Context() guarantees compiletools.build
    # has no parent even when something upstream (CI wrapper, profiler)
    # left an active span in the ambient context.
    root_span = tracer.start_span(
        "compiletools.build",
        context=otel_context.Context(),
        start_time=root_start_ns,
    )
    try:
        try:
            root_ctx = trace.set_span_in_context(root_span)
            for child in timer._root.children:
                _emit_event(tracer, child, root_ctx, mono_to_wall_offset)
        finally:
            # End root even if child emission raised; otherwise the
            # BatchSpanProcessor never flushes it and the whole tree
            # silently disappears.  Swallow end()'s own errors so that
            # flush+shutdown still runs and the daemon thread is joined.
            try:
                root_span.end(end_time=root_end_ns)
            except Exception as exc:
                print(f"Warning: OTLP export failed to end root span: {exc}", file=sys.stderr)
    finally:
        # provider.shutdown() force-flushes internally, but call force_flush
        # first as a nominal latency hint. NOTE: the SDK does NOT propagate
        # this timeout to the underlying exporter network call, so the real
        # latency bound is _DEFAULT_EXPORT_REQUEST_TIMEOUT_SECONDS per request,
        # multiplied by the exporter's internal retry count. shutdown() always
        # runs so the BatchSpanProcessor daemon thread is joined.
        flush_start = time.monotonic()
        try:
            flushed = provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            flushed = False
            print(f"Warning: OTLP export flush raised: {exc}", file=sys.stderr)
        if not flushed:
            print(
                "Warning: OTLP export timed out flushing spans; some spans may be lost "
                "(with retries, total flush can exceed the 5s nominal budget)",
                file=sys.stderr,
            )
        elif time.monotonic() - flush_start > 2.0:
            print(
                f"Warning: OTLP export took {time.monotonic() - flush_start:.1f}s to flush",
                file=sys.stderr,
            )
        try:
            provider.shutdown()
        except Exception as exc:
            print(f"Warning: OTLP export shutdown raised: {exc}", file=sys.stderr)
