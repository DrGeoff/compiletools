"""End-of-build OpenTelemetry (OTLP) export for BuildTimer span trees.

Walks the in-memory :class:`~compiletools.build_timer.BuildTimer` tree
once the build has finished and ships one OTel span per ``TimingEvent``
to an OTLP collector. Timestamps are converted from BuildTimer's
monotonic clock to wall-clock nanoseconds using the offset captured at
``BuildTimer.__init__``, so spans align with everything else in the
user's observability stack.

Lazy SDK import; install the optional ``otel`` extra
(``pip install 'compiletools[otel]'``) to enable.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

from compiletools.otel._connection import (
    MISSING_EXTRA_HINT,
    _build_processor,
    build_resource,
)

if TYPE_CHECKING:
    from compiletools.build_timer import BuildTimer, TimingEvent


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


def _emit_event(tracer, event: TimingEvent, parent_ctx, mono_to_wall_offset: float, *, args=None) -> None:
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

        # Lift TimingEvent.metadata onto span attributes.  Producer-side
        # opt-in: only keys the recorder explicitly set land here, so
        # there's no allow-list to maintain.  The try/except keeps a
        # misbehaving producer (a value the SDK can't serialize — dict,
        # set, datetime, ...) from killing the whole span: the offending
        # attribute is dropped and the rest of the span still exports.
        for key, value in event.metadata.items():
            if value is None:
                continue
            try:
                span.set_attribute(key, value)
            except (TypeError, ValueError) as exc:
                # One bad attribute should not poison the whole export.
                # Surface it at verbose>=1 so a producer wiring bug is
                # findable without scraping the collector.
                if getattr(args, "verbose", 0) >= 1:
                    print(f"otel: dropped span attr {key!r}: {exc}", file=sys.stderr)

        if event.children:
            child_ctx = trace.set_span_in_context(span, parent_ctx)
            for child in event.children:
                _emit_event(tracer, child, child_ctx, mono_to_wall_offset, args=args)
    finally:
        # End the span even when a child emission raises; otherwise the
        # BatchSpanProcessor never flushes the parent and the whole subtree
        # is silently dropped.
        span.end(end_time=end_ns)


def export_buildtimer(timer: BuildTimer, args, *, _processor=None) -> str | None:
    """Export a finished BuildTimer's span tree via OTLP.

    Emits one OTel span per ``TimingEvent`` (root + phases + rules),
    preserving parent/child via SDK Context propagation.  All timestamps
    are wall-clock nanoseconds derived from BuildTimer's monotonic
    timeline via ``BuildTimer._wall_to_monotonic_offset``, so the
    resulting trace lines up with the rest of the user's observability
    data.

    Returns the **hex-encoded trace_id** of the root build span, or
    ``None`` when timing is disabled and no span tree was emitted. The
    P4 ccache-stats hook in ct-cake uses this as the ``ct.invocation_id``
    on the emitted ccache metrics so metrics natively join to spans
    without a side-channel correlation id.

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
        raise RuntimeError(MISSING_EXTRA_HINT) from exc

    if not timer.enabled:
        return None

    timer.finish()

    resource = build_resource(args)
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
    # Capture the trace_id once -- get_span_context() is safe to call
    # even after end() but capturing eagerly keeps the return path simple.
    try:
        root_trace_id = format(root_span.get_span_context().trace_id, "032x")
    except Exception:
        root_trace_id = None
    try:
        # Lift any metadata producers attached to the root TimingEvent
        # (P4 ccache headline numbers today; P5 cross-layer aggregates
        # tomorrow). Same try/except shape as the per-rule lift -- a single
        # mis-typed value must not poison the whole export. Run before
        # child emission so the root span is fully populated even if a
        # child emission later raises.
        for key, value in (timer._root.metadata or {}).items():
            if value is None:
                continue
            try:
                root_span.set_attribute(key, value)
            except (TypeError, ValueError) as exc:
                if getattr(args, "verbose", 0) >= 1:
                    print(f"otel: dropped root span attr {key!r}: {exc}", file=sys.stderr)
        try:
            root_ctx = trace.set_span_in_context(root_span)
            for child in timer._root.children:
                _emit_event(tracer, child, root_ctx, mono_to_wall_offset, args=args)
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
    return root_trace_id
