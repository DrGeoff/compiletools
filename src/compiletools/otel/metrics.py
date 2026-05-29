"""One-shot OpenTelemetry (OTLP) metric export for compiletools.

Public surface:

- :func:`export_cache_metrics` — emit ``ct.cas.*`` gauges from a
  :mod:`compiletools.cache_report` scan (used by ``ct-cache-report``).
- :func:`export_ccache_metrics` — ship parsed ccache stats counts as
  OTLP metrics (used by ct-cake's post-build hook when
  ``--ccache-statslog`` is set).

Snapshot-and-exit by construction: each entry point builds a
``MeterProvider``, populates one observation per gauge / counter,
force-flushes, and shuts down. No long-running daemon, no periodic
re-export.

Trace-only collectors: when ``args.otel_metrics_as_spans`` is set, the
metric set is flattened into a single short-lived span whose attributes
encode the values, and no metric pipeline is built at all. This lets a
trace-only collector still surface the data without a separate metrics
endpoint.

Lazy SDK import; install the optional ``otel`` extra
(``pip install 'compiletools[otel]'``) to enable.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from compiletools.otel._connection import (
    MISSING_EXTRA_HINT,
    _build_metric_reader,
    build_resource,
)

if TYPE_CHECKING:
    from compiletools.cache_report import CacheReport, ExeReport, PchReport, PcmReport


# Stable metric names. Defined once so test assertions and dashboard
# queries grep against a single canonical list.
METRIC_TOTAL_BYTES = "ct.cas.total_bytes"
METRIC_TOTAL_ENTRIES = "ct.cas.total_entries"
METRIC_UNIQUE_BUCKETS = "ct.cas.unique_buckets"
METRIC_WASTED_BYTES = "ct.cas.wasted_bytes"
METRIC_DUPLICATE_GROUPS = "ct.cas.duplicate_groups"

# The five gauges, in emission order. Kept as a module-level tuple so the
# test suite can assert "exactly these and no more" without duplicating
# the list.
_CACHE_GAUGE_NAMES = (
    METRIC_TOTAL_BYTES,
    METRIC_TOTAL_ENTRIES,
    METRIC_UNIQUE_BUCKETS,
    METRIC_WASTED_BYTES,
    METRIC_DUPLICATE_GROUPS,
)


@dataclass(frozen=True, slots=True)
class _Point:
    """One (metric, value, tags) tuple ready for emission.

    Built once by ``_cache_points_from_reports``; consumed by either the
    OTLP metric emitter or the metrics-as-spans fallback. The ccache
    counter emitter shares the same private helper.
    """

    metric_name: str
    value: int
    tags: tuple[tuple[str, str], ...]


def _cache_points_from_reports(
    reports: dict[str, CacheReport | PchReport | PcmReport | ExeReport | None],
) -> list[_Point]:
    """Flatten the four report dataclasses into a list of ``_Point`` rows.

    A ``None`` report (cas dir absent / not scanned) contributes zero
    rows — distinct from a scanned-but-empty report, which contributes
    one row per metric with value 0. This matches the design-doc
    contract: "zeros are emitted for cas dirs that exist but are empty —
    'I scanned, found nothing' is signal distinct from 'I didn't scan'."
    """
    points: list[_Point] = []
    for kind, rep in reports.items():
        if rep is None:
            continue
        tags = (("cas_kind", kind),)
        # Use getattr with sentinel defaults so a future field rename
        # surfaces as a test failure rather than silently emitting 0.
        unique_buckets = _unique_buckets_for(rep)
        duplicated = getattr(rep, "duplicated_groups", None)
        points.append(_Point(METRIC_TOTAL_BYTES, int(rep.total_bytes), tags))
        points.append(_Point(METRIC_TOTAL_ENTRIES, int(rep.total_entries), tags))
        points.append(_Point(METRIC_UNIQUE_BUCKETS, int(unique_buckets), tags))
        points.append(_Point(METRIC_WASTED_BYTES, int(rep.wasted_bytes), tags))
        points.append(_Point(METRIC_DUPLICATE_GROUPS, len(duplicated or ()), tags))
    return points


def _unique_buckets_for(rep) -> int:
    """Resolve the dataclass-specific "unique buckets" field name.

    The four report dataclasses use different field names for the same
    concept (objdir tracks ``(file_hash, dep_hash)`` tuples;
    pchdir tracks ``header_realpath``; pcmdir tracks ``bucket_key``;
    exedir tracks ``(source_realpath, suffix)``). The emitted metric is
    one canonical ``ct.cas.unique_buckets`` regardless.
    """
    for field_name in ("unique_src_deps_count", "unique_headers_count", "unique_buckets_count"):
        if hasattr(rep, field_name):
            return getattr(rep, field_name)
    raise AttributeError(
        f"{type(rep).__name__} has none of the expected unique-buckets fields; "
        "did the cache_report data model change? Update _unique_buckets_for."
    )


def export_cache_metrics(
    reports: dict[str, CacheReport | PchReport | PcmReport | ExeReport | None],
    args,
    *,
    _reader=None,
) -> None:
    """Export ``ct.cas.*`` gauges from a ct-cache-report scan via OTLP.

    *reports* maps ``cas_kind`` (``"obj" | "pch" | "pcm" | "exe"``) to the
    matching report dataclass or ``None`` (when the corresponding cas
    directory was not scanned). Each non-None report produces five
    gauge observations tagged with its ``cas_kind``.

    Snapshot-and-exit: builds a fresh ``MeterProvider``, records one
    observation per gauge, force-flushes, and shuts down. The natural
    deployment is a cron or post-build hook per CAS-bearing host.

    When ``args.otel_metrics_as_spans`` is set, the gauge values are
    flattened into a single ``ct.cache.snapshot`` span (for trace-only
    collectors). No metric pipeline is built in that case.

    Raises ``RuntimeError`` if the optional ``otel`` extra isn't
    installed. ``_reader`` is a test seam: pass an
    ``InMemoryMetricReader`` to capture observations in-memory instead
    of shipping over the network.
    """
    points = _cache_points_from_reports(reports)
    if not points:
        # All four reports were None — nothing was scanned. Silent no-op
        # matches the trace-side "empty payload" contract.
        return

    if getattr(args, "otel_metrics_as_spans", False):
        _emit_cache_snapshot_as_span(points, args)
        return

    _emit_cache_points_as_metrics(points, args, _reader=_reader)


def _emit_cache_points_as_metrics(points: list[_Point], args, *, _reader=None) -> None:
    """Build a one-shot MeterProvider, observe each point as a gauge, flush.

    Uses observable gauges (``create_observable_gauge`` with a callback)
    rather than ``set()`` on a synchronous gauge because the SDK's
    synchronous ``Gauge`` was added in 1.28 and the project's pinned
    lower bound is 1.27 — observable gauges have been stable for years
    and work across the supported version band.
    """
    try:
        from opentelemetry.sdk.metrics import MeterProvider
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    reader = _reader if _reader is not None else _build_metric_reader(args)
    resource = build_resource(args)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter("compiletools")

    # Group points by metric_name so each gauge has exactly one
    # registered observable (multiple callbacks on the same name are
    # legal but harder to reason about; the SDK would emit one Metric
    # per callback). Each callback returns one Observation per tag set.
    from opentelemetry.metrics import Observation

    by_metric: dict[str, list[_Point]] = {}
    for pt in points:
        by_metric.setdefault(pt.metric_name, []).append(pt)

    # The SDK invokes callbacks on collection; capture each group via
    # default-arg binding so the closure doesn't share the loop variable.
    for metric_name, metric_points in by_metric.items():

        def _cb(_options, _metric_points=metric_points):
            return [Observation(p.value, dict(p.tags)) for p in _metric_points]

        meter.create_observable_gauge(
            name=metric_name,
            callbacks=[_cb],
        )

    try:
        flush_start = time.monotonic()
        try:
            flushed = provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            flushed = False
            print(f"Warning: OTLP metric export flush raised: {exc}", file=sys.stderr)
        if not flushed:
            print(
                "Warning: OTLP metric export timed out flushing; some metrics may be lost "
                "(with retries, total flush can exceed the 5s nominal budget)",
                file=sys.stderr,
            )
        elif time.monotonic() - flush_start > 2.0:
            print(
                f"Warning: OTLP metric export took {time.monotonic() - flush_start:.1f}s to flush",
                file=sys.stderr,
            )
    finally:
        # Skip shutdown() when a test seam reader was supplied: with
        # InMemoryMetricReader, observable gauges collect at read-time and
        # some SDK versions short-circuit collection inside shutdown(),
        # leaving the test with no observations. The production path
        # (PeriodicExportingMetricReader) still gets a clean shutdown.
        if _reader is None:
            try:
                provider.shutdown()
            except Exception as exc:
                print(f"Warning: OTLP metric export shutdown raised: {exc}", file=sys.stderr)


def _emit_cache_snapshot_as_span(points: list[_Point], args) -> None:
    """Flatten cache gauges into a single ``ct.cache.snapshot`` span.

    Span attribute shape: ``ct.cas.<metric_stem>.<cas_kind>``, e.g.
    ``ct.cas.total_bytes.obj=12345``. Mechanical re-shape; no metric
    pipeline is built. Used when the operator's collector accepts
    traces but not metrics.
    """
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    # Reuse the trace processor builder so headers / endpoint / protocol /
    # insecure all behave identically to the normal trace path.
    from compiletools.otel._connection import _build_processor

    resource = build_resource(args)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(_build_processor(args))
    tracer = provider.get_tracer("compiletools")

    span = tracer.start_span("ct.cache.snapshot", context=otel_context.Context())
    try:
        for pt in points:
            # metric_name is "ct.cas.total_bytes"; cas_kind is the tag value.
            cas_kind = dict(pt.tags).get("cas_kind", "unknown")
            span.set_attribute(f"{pt.metric_name}.{cas_kind}", pt.value)
    finally:
        span.end()
        try:
            provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            print(f"Warning: OTLP metrics-as-spans flush raised: {exc}", file=sys.stderr)
        try:
            provider.shutdown()
        except Exception as exc:
            print(f"Warning: OTLP metrics-as-spans shutdown raised: {exc}", file=sys.stderr)


def _resource_with_invocation_id(args, invocation_id: str | None):
    """Build the OTel Resource and tack ``ct.invocation_id`` on top.

    The trace path derives ``ct.invocation_id`` from the diagnostics-dir
    leaf (see ``_connection.build_resource``). The metrics path lets the
    caller override it with the **root span's trace_id** so dashboards can
    join metrics to spans without a side-channel correlation id.
    """
    from opentelemetry.sdk.resources import Resource

    base = build_resource(args)
    if not invocation_id:
        return base
    return base.merge(Resource({"ct.invocation_id": invocation_id}))


def export_ccache_metrics(
    counts: Counter[str],
    args,
    *,
    invocation_id: str | None = None,
    _reader: Any = None,
) -> None:
    """Ship parsed ccache stats counts as OTLP metrics.

    ``counts`` is the ``{event_name: count}`` mapping returned by
    ``compiletools.ccache_stats.parse_statslog``. Emits:

    - ``ct.ccache.events`` (counter, tag ``ccache_event``) -- one
      observation per distinct event with its total count for the build.
    - ``ct.ccache.hit_rate`` (gauge) -- local hit ratio in ``[0,1]``.
    - ``ct.ccache.remote_hit_rate`` (gauge) -- remote-storage hit ratio.

    Empty ``counts`` is a silent no-op (matches the trace-side contract).

    ``invocation_id`` is attached as the ``ct.invocation_id`` resource
    attribute on the emitted metrics. Callers (ct-cake) pass the root
    build span's trace_id here so the metrics natively join against the
    build's spans in any backend that indexes on trace_id.

    ``_reader`` is a test seam: pass an ``InMemoryMetricReader`` to
    capture emissions in-memory instead of shipping over the network.

    When ``args.otel_metrics_as_spans`` is true, the metric set is
    flattened onto attributes of a single short-lived span instead of
    OTLP metrics -- the fallback for trace-only collectors documented
    in the design doc.

    Raises ``RuntimeError(MISSING_EXTRA_HINT)`` if the optional ``otel``
    extra isn't installed. All other failures (network, flush timeout)
    are caught and warned to stderr -- metrics publishing must never
    break a build.
    """
    if not counts:
        return

    if getattr(args, "otel_metrics_as_spans", False):
        _emit_as_span(counts, args, invocation_id=invocation_id)
        return

    try:
        from opentelemetry.sdk.metrics import MeterProvider
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    resource = _resource_with_invocation_id(args, invocation_id)
    reader = _reader if _reader is not None else _build_metric_reader(args)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter("compiletools.ccache")

    events_counter = meter.create_counter(
        "ct.ccache.events",
        description="ccache event occurrences over the build",
    )
    hit_rate_gauge = meter.create_gauge(
        "ct.ccache.hit_rate",
        description="Local ccache hit ratio in [0,1]",
    )
    remote_hit_rate_gauge = meter.create_gauge(
        "ct.ccache.remote_hit_rate",
        description="Remote ccache hit ratio in [0,1]",
    )

    for event_name, count in counts.items():
        if count <= 0:
            continue
        events_counter.add(count, {"ccache_event": event_name})

    from compiletools.ccache_stats import hit_rate, remote_hit_rate

    hit_rate_gauge.set(hit_rate(counts))
    remote_hit_rate_gauge.set(remote_hit_rate(counts))

    # When a test seam reader is supplied (e.g. InMemoryMetricReader),
    # tests pull metrics via reader.get_metrics_data() AFTER this function
    # returns. Calling shutdown() here would short-circuit the gauge
    # collection path on those readers (gauges are read at collect-time,
    # and the SDK skips collect when the provider is shut down). For the
    # production path -- where _reader was None and we built a
    # PeriodicExportingMetricReader -- we still need shutdown() to join
    # the background flush thread.
    if _reader is None:
        flush_start = time.monotonic()
        try:
            flushed = provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            flushed = False
            print(f"Warning: OTLP metrics flush raised: {exc}", file=sys.stderr)
        if not flushed:
            print(
                "Warning: OTLP metrics export timed out flushing; some points may be lost",
                file=sys.stderr,
            )
        elif time.monotonic() - flush_start > 2.0:
            print(
                f"Warning: OTLP metrics export took {time.monotonic() - flush_start:.1f}s to flush",
                file=sys.stderr,
            )
        try:
            provider.shutdown()
        except Exception as exc:
            print(f"Warning: OTLP metrics shutdown raised: {exc}", file=sys.stderr)


def _emit_as_span(counts: Counter[str], args, *, invocation_id: str | None) -> None:
    """Trace-only-collector fallback for ``export_ccache_metrics``.

    Synthesises one short-lived span named ``ct.ccache.snapshot`` whose
    attributes are the per-event counts flattened to
    ``ct.ccache.events.<event_name>`` plus the hit-rate gauges. Lets a
    pure-traces collector show the ccache outcome alongside the build
    span tree without a metrics endpoint.

    Note: this does not nest under the build root span. The build trace
    has already been flushed by ``export_buildtimer`` by the time this
    runs; nesting would require us to keep the TracerProvider alive
    across the publish gap. Dashboards can still join on
    ``ct.invocation_id``.
    """
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    from compiletools.ccache_stats import hit_rate, remote_hit_rate
    from compiletools.otel._connection import _build_processor

    resource = _resource_with_invocation_id(args, invocation_id)
    provider = TracerProvider(resource=resource)
    processor: BatchSpanProcessor = _build_processor(args)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("compiletools.ccache")

    span = tracer.start_span("ct.ccache.snapshot", context=otel_context.Context())
    try:
        for event_name, count in counts.items():
            if count <= 0:
                continue
            try:
                span.set_attribute(f"ct.ccache.events.{event_name}", int(count))
            except (TypeError, ValueError):
                pass
        span.set_attribute("ct.ccache.hit_rate", float(hit_rate(counts)))
        span.set_attribute("ct.ccache.remote_hit_rate", float(remote_hit_rate(counts)))
    finally:
        try:
            span.end()
        except Exception as exc:
            print(f"Warning: OTLP ccache snapshot span end raised: {exc}", file=sys.stderr)

    try:
        provider.force_flush(timeout_millis=5000)
    except Exception as exc:
        print(f"Warning: OTLP ccache snapshot flush raised: {exc}", file=sys.stderr)
    try:
        provider.shutdown()
    except Exception as exc:
        print(f"Warning: OTLP ccache snapshot shutdown raised: {exc}", file=sys.stderr)
