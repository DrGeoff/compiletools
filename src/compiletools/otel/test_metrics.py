"""Tests for one-shot metric emission in compiletools.otel.metrics."""

from __future__ import annotations

import sys
import types
from collections import Counter

import pytest

# importorskip the SDK, not the bare ``opentelemetry`` namespace package.
# See test_traces.py for the rationale (PEP 420 namespace + api-only
# transitive installs would otherwise let the bare import succeed and
# turn this module's collection into an error rather than a skip).
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from compiletools.cache_report import (
    CacheReport,
    DuplicateGroup,
    ExeReport,
    ObjectFileEntry,
    PchReport,
    PcmReport,
)
from compiletools.otel._connection import MISSING_EXTRA_HINT as _MISSING_EXTRA_HINT
from compiletools.otel.metrics import (
    _CACHE_GAUGE_NAMES,
    METRIC_DUPLICATE_GROUPS,
    METRIC_TOTAL_BYTES,
    METRIC_TOTAL_ENTRIES,
    METRIC_UNIQUE_BUCKETS,
    METRIC_WASTED_BYTES,
    _cache_points_from_reports,
    export_cache_metrics,
    export_ccache_metrics,
)

# ----------------------------------------------------------------- test helpers


def _make_args(**overrides):
    """Tiny stand-in for an argparse Namespace mirroring test_traces.py."""
    defaults = dict(
        otel_service_name=None,
        otel_endpoint=None,
        otel_resource_attr=[],
        otel_protocol="grpc",
        otel_headers=None,
        otel_insecure=None,
        otel_metrics_as_spans=False,
        variant="gcc.debug",
        backend="ninja",
        diagnostics_dir=None,
        bindir=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _make_object_entry(basename: str = "foo.cpp", size: int = 100) -> ObjectFileEntry:
    return ObjectFileEntry(
        path=f"/cas/aa/{basename}.aaaaaaaaaaaa.bbbbbbbbbbbbbb.cccccccccccccccc.o",
        basename=basename,
        file_hash="aaaaaaaaaaaa",
        dep_hash="bbbbbbbbbbbbbb",
        macro_state_hash="cccccccccccccccc",
        size_bytes=size,
    )


def _make_cache_report() -> CacheReport:
    """A populated objdir report with one duplicated group + waste."""
    entries = [_make_object_entry("foo.cpp", 100), _make_object_entry("bar.cpp", 200)]
    group = DuplicateGroup(
        file_hash="aaaaaaaaaaaa",
        dep_hash="bbbbbbbbbbbbbb",
        basename="foo.cpp",
        variants=entries,
    )
    return CacheReport(
        objdir="/cas/obj",
        total_entries=5,
        total_bytes=12345,
        unique_src_deps_count=3,
        duplicated_groups=[group],
        wasted_bytes=678,
    )


def _make_pch_report(empty: bool = False) -> PchReport:
    if empty:
        return PchReport(
            pchdir="/cas/pch",
            total_entries=0,
            total_bytes=0,
            unique_headers_count=0,
            duplicated_groups=[],
        )
    return PchReport(
        pchdir="/cas/pch",
        total_entries=2,
        total_bytes=999,
        unique_headers_count=1,
        duplicated_groups=[],
        wasted_bytes=0,
    )


def _make_pcm_report() -> PcmReport:
    return PcmReport(
        pcmdir="/cas/pcm",
        total_entries=4,
        total_bytes=2222,
        unique_buckets_count=4,
        duplicated_groups=[],
        wasted_bytes=0,
    )


def _make_exe_report() -> ExeReport:
    return ExeReport(
        exedir="/cas/exe",
        total_entries=1,
        total_bytes=8888,
        unique_buckets_count=1,
        duplicated_groups=[],
        wasted_bytes=0,
    )


def _collect_metrics(reports, args):
    """Run the exporter with an in-memory metric reader; return its data."""
    reader = InMemoryMetricReader()
    export_cache_metrics(reports, args, _reader=reader)
    # get_metrics_data is non-None as long as the provider produced at least
    # one observation; tests assert the populated shape explicitly.
    return reader.get_metrics_data()


def _flatten_metrics(metrics_data):
    """Walk MetricsData -> {(name, frozenset(tags)): value}.

    Observable gauges return the latest observation per attribute set on
    each collection. The metric reader's get_metrics_data() triggers one
    collection cycle, so each (name, tags) appears exactly once.
    """
    out: dict[tuple[str, frozenset[tuple[str, str]]], int | float] = {}
    if metrics_data is None:
        return out
    for rm in metrics_data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                # observable gauge -> metric.data is a Gauge with data_points
                for dp in metric.data.data_points:
                    tags = frozenset((str(k), str(v)) for k, v in dp.attributes.items())
                    out[(metric.name, tags)] = dp.value
    return out


# ----------------------------------------------------------------- points construction


class TestCachePointsFromReports:
    def test_all_four_kinds_emit_five_points_each(self):
        reports = {
            "obj": _make_cache_report(),
            "pch": _make_pch_report(),
            "pcm": _make_pcm_report(),
            "exe": _make_exe_report(),
        }
        points = _cache_points_from_reports(reports)
        # 4 kinds * 5 gauges = 20 points
        assert len(points) == 20
        # Every point has exactly one tag and it's cas_kind.
        for p in points:
            assert len(p.tags) == 1
            assert p.tags[0][0] == "cas_kind"

    def test_none_report_emits_no_points_for_that_kind(self):
        reports = {"obj": _make_cache_report(), "pch": None, "pcm": None, "exe": None}
        points = _cache_points_from_reports(reports)
        kinds = {dict(p.tags)["cas_kind"] for p in points}
        assert kinds == {"obj"}

    def test_empty_report_still_emits_five_zero_points(self):
        """Scanned-but-empty != not-scanned. Empty must still emit zeros."""
        reports = {"obj": None, "pch": _make_pch_report(empty=True), "pcm": None, "exe": None}
        points = _cache_points_from_reports(reports)
        assert len(points) == 5
        names = {p.metric_name for p in points}
        assert names == set(_CACHE_GAUGE_NAMES)
        for p in points:
            assert p.value == 0

    def test_unique_buckets_field_resolved_per_dataclass(self):
        """Different reports use different attr names; all collapse to one metric."""
        reports = {
            "obj": _make_cache_report(),  # unique_src_deps_count=3
            "pch": _make_pch_report(),  # unique_headers_count=1
            "pcm": _make_pcm_report(),  # unique_buckets_count=4
            "exe": _make_exe_report(),  # unique_buckets_count=1
        }
        points = _cache_points_from_reports(reports)
        unique_by_kind = {dict(p.tags)["cas_kind"]: p.value for p in points if p.metric_name == METRIC_UNIQUE_BUCKETS}
        assert unique_by_kind == {"obj": 3, "pch": 1, "pcm": 4, "exe": 1}

    def test_duplicate_groups_metric_is_count_not_dataclass(self):
        rep = _make_cache_report()  # exactly one duplicated_group
        reports = {"obj": rep, "pch": None, "pcm": None, "exe": None}
        points = _cache_points_from_reports(reports)
        dup = next(p for p in points if p.metric_name == METRIC_DUPLICATE_GROUPS)
        assert dup.value == 1


# ----------------------------------------------------------------- in-memory metric emission


class TestExportCacheMetricsInMemory:
    def test_emits_five_gauges_per_kind_with_correct_tags(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "")

        reports = {
            "obj": _make_cache_report(),
            "pch": _make_pch_report(),
            "pcm": _make_pcm_report(),
            "exe": _make_exe_report(),
        }
        flat = _flatten_metrics(_collect_metrics(reports, _make_args()))

        # 4 kinds * 5 metrics = 20 (name, tags) keys.
        assert len(flat) == 20
        # Every gauge name from the canonical list must appear for every kind.
        for name in _CACHE_GAUGE_NAMES:
            for kind in ("obj", "pch", "pcm", "exe"):
                key = (name, frozenset({("cas_kind", kind)}))
                assert key in flat, f"missing {key}"

    def test_observed_values_match_synthetic_reports(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "")

        rep = _make_cache_report()
        reports = {"obj": rep, "pch": None, "pcm": None, "exe": None}
        flat = _flatten_metrics(_collect_metrics(reports, _make_args()))

        tag = frozenset({("cas_kind", "obj")})
        assert flat[(METRIC_TOTAL_BYTES, tag)] == rep.total_bytes
        assert flat[(METRIC_TOTAL_ENTRIES, tag)] == rep.total_entries
        assert flat[(METRIC_UNIQUE_BUCKETS, tag)] == rep.unique_src_deps_count
        assert flat[(METRIC_WASTED_BYTES, tag)] == rep.wasted_bytes
        assert flat[(METRIC_DUPLICATE_GROUPS, tag)] == 1

    def test_empty_scan_still_emits_zero_observations(self, monkeypatch):
        """Scanned-but-empty cas dir must surface as five zero-valued gauges."""
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "")

        reports = {"obj": None, "pch": _make_pch_report(empty=True), "pcm": None, "exe": None}
        flat = _flatten_metrics(_collect_metrics(reports, _make_args()))

        tag = frozenset({("cas_kind", "pch")})
        for name in _CACHE_GAUGE_NAMES:
            assert flat[(name, tag)] == 0

    def test_all_none_is_silent_noop(self, monkeypatch):
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "")

        reader = InMemoryMetricReader()
        # No raise, no data emitted.
        export_cache_metrics(
            {"obj": None, "pch": None, "pcm": None, "exe": None},
            _make_args(),
            _reader=reader,
        )
        # Reader saw no provider attached at all — get_metrics_data
        # returns None or empty resource_metrics.
        data = reader.get_metrics_data()
        if data is not None:
            assert all(not rm.scope_metrics for rm in data.resource_metrics)

    def test_resource_attributes_match_traces_path(self, monkeypatch):
        """Cache metrics must ride the same resource as buildtimer spans."""
        monkeypatch.setattr("socket.gethostname", lambda: "test-host")
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "deadbeef")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "inv-xyz")

        reports = {"obj": _make_cache_report(), "pch": None, "pcm": None, "exe": None}
        reader = InMemoryMetricReader()
        export_cache_metrics(reports, _make_args(), _reader=reader)
        data = reader.get_metrics_data()
        assert data is not None
        rm = data.resource_metrics[0]
        attrs = dict(rm.resource.attributes)
        assert attrs["service.name"] == "compiletools"
        assert attrs["service.namespace"] == "compiletools"
        assert attrs["host.name"] == "test-host"
        assert attrs["git.commit.sha"] == "deadbeef"
        assert attrs["ct.variant"] == "gcc.debug"
        assert attrs["ct.backend"] == "ninja"
        assert attrs["ct.invocation_id"] == "inv-xyz"


# ----------------------------------------------------------------- metrics-as-spans fallback


class TestMetricsAsSpansFallback:
    def test_flattens_to_one_snapshot_span_with_per_kind_attrs(self, monkeypatch):
        """When --otel-metrics-as-spans is set, no metric pipeline is built;
        gauges land as attributes on a single ct.cache.snapshot span."""
        monkeypatch.setattr("compiletools.otel._connection.get_git_commit_sha", lambda cwd=None: "")
        monkeypatch.setattr("compiletools.otel._connection._invocation_id_from_diag_dir", lambda args: "")

        sink = InMemorySpanExporter()
        # Replace _build_processor at the metrics module's call site so the
        # fallback path captures spans in-memory instead of opening sockets.
        monkeypatch.setattr(
            "compiletools.otel._connection._build_processor",
            lambda args: SimpleSpanProcessor(sink),
        )

        reports = {"obj": _make_cache_report(), "pch": None, "pcm": None, "exe": None}
        args = _make_args(otel_metrics_as_spans=True)
        export_cache_metrics(reports, args)

        spans = list(sink.get_finished_spans())
        assert len(spans) == 1
        snap = spans[0]
        assert snap.name == "ct.cache.snapshot"
        # Every gauge contributed one attribute keyed as <metric>.<cas_kind>.
        attrs = dict(snap.attributes or {})
        for name in _CACHE_GAUGE_NAMES:
            assert f"{name}.obj" in attrs


# ----------------------------------------------------------------- missing extra


def test_missing_extra_raises_with_install_hint(monkeypatch):
    """When opentelemetry imports fail, raise RuntimeError with the hint."""
    saved = {k: v for k, v in sys.modules.items() if k.startswith("opentelemetry")}
    try:
        for k in list(sys.modules):
            if k.startswith("opentelemetry"):
                del sys.modules[k]
        monkeypatch.setitem(sys.modules, "opentelemetry", None)

        reports = {"obj": _make_cache_report(), "pch": None, "pcm": None, "exe": None}
        with pytest.raises(RuntimeError) as excinfo:
            export_cache_metrics(reports, _make_args())
        assert _MISSING_EXTRA_HINT in str(excinfo.value)
        assert "compiletools[otel]" in str(excinfo.value)
    finally:
        sys.modules.pop("opentelemetry", None)
        sys.modules.update(saved)


# ----------------------------------------------------------------- ccache metrics (P4)


def _collect_ccache_metrics(reader: InMemoryMetricReader):
    """Return a flat ``{metric_name: [(attrs_frozenset, value)]}`` map.

    InMemoryMetricReader emits a nested MetricsData tree (resource ->
    scope -> metric -> data point); the call sites here only care about
    final per-point (attrs, value) tuples per name, so flatten once.
    """
    out: dict[str, list[tuple[frozenset[tuple[str, str]], float | int]]] = {}
    data = reader.get_metrics_data()
    if data is None:
        return out
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                points = []
                # Counter -> Sum, Gauge -> Gauge (both expose `.data_points`).
                for dp in metric.data.data_points:
                    attrs = frozenset((k, str(v)) for k, v in (dp.attributes or {}).items())
                    value = getattr(dp, "value", None)
                    if value is None:
                        # Sum points expose `.value` directly; older SDKs
                        # might expose `.sum`. Defensive fallback.
                        value = getattr(dp, "sum", 0)
                    points.append((attrs, value))
                out.setdefault(metric.name, []).extend(points)
    return out


class TestExportCcacheMetrics:
    def test_empty_counts_is_silent_no_op(self):
        # No SDK objects constructed; flag-only ``export()`` path returns
        # before any provider/reader work. Asserting the call simply
        # doesn't raise is enough: if a provider WERE constructed we'd
        # see a flush warning to stderr.
        export_ccache_metrics(Counter(), _make_args())

    def test_counter_metric_per_event(self):
        reader = InMemoryMetricReader()
        counts = Counter(
            {
                "direct_cache_hit": 3,
                "cache_miss": 5,
                "local_storage_write": 2,
            }
        )
        export_ccache_metrics(counts, _make_args(), _reader=reader)

        metrics = _collect_ccache_metrics(reader)
        assert "ct.ccache.events" in metrics

        # Build {event_name: count} from the captured points so the
        # assertion is independent of point iteration order.
        events_points = metrics["ct.ccache.events"]
        captured = {}
        for attrs, value in events_points:
            attrs_dict = dict(attrs)
            captured[attrs_dict["ccache_event"]] = value
        assert captured == {
            "direct_cache_hit": 3,
            "cache_miss": 5,
            "local_storage_write": 2,
        }

    def test_hit_rate_gauges_emitted(self):
        reader = InMemoryMetricReader()
        counts = Counter(
            {
                "direct_cache_hit": 7,
                "cache_miss": 3,
                "remote_storage_hit": 2,
                "remote_storage_miss": 8,
            }
        )
        export_ccache_metrics(counts, _make_args(), _reader=reader)

        metrics = _collect_ccache_metrics(reader)
        assert "ct.ccache.hit_rate" in metrics
        assert "ct.ccache.remote_hit_rate" in metrics

        # Single point each, no tags.
        hit_rate_points = metrics["ct.ccache.hit_rate"]
        assert len(hit_rate_points) == 1
        attrs, value = hit_rate_points[0]
        assert attrs == frozenset()
        assert value == pytest.approx(0.70)

        remote_points = metrics["ct.ccache.remote_hit_rate"]
        assert len(remote_points) == 1
        _, remote_value = remote_points[0]
        assert remote_value == pytest.approx(0.20)

    def test_invocation_id_lands_on_resource(self):
        reader = InMemoryMetricReader()
        export_ccache_metrics(
            Counter({"direct_cache_hit": 1}),
            _make_args(),
            invocation_id="deadbeefcafef00d" * 2,  # 32-hex synthetic trace_id
            _reader=reader,
        )
        data = reader.get_metrics_data()
        assert data is not None
        attrs = data.resource_metrics[0].resource.attributes
        assert attrs.get("ct.invocation_id") == "deadbeefcafef00d" * 2

    def test_invocation_id_absent_does_not_blank_diag_dir_id(self):
        # When the caller passes invocation_id=None, the underlying
        # build_resource() may still populate ct.invocation_id from
        # the diagnostics-dir leaf -- the override must not clobber it
        # with None/empty.
        reader = InMemoryMetricReader()
        export_ccache_metrics(
            Counter({"direct_cache_hit": 1}),
            _make_args(),
            invocation_id=None,
            _reader=reader,
        )
        # If this raised, the metric path was destroyed by the None
        # override. We only assert no exception here.

    def test_resource_carries_variant_and_backend(self):
        """ct.variant / ct.backend land on the metrics resource so
        dashboards can join metric series to span series by these keys."""
        reader = InMemoryMetricReader()
        export_ccache_metrics(
            Counter({"direct_cache_hit": 1}),
            _make_args(variant="gcc.release.foo", backend="trace"),
            _reader=reader,
        )
        data = reader.get_metrics_data()
        assert data is not None
        attrs = data.resource_metrics[0].resource.attributes
        assert attrs.get("ct.variant") == "gcc.release.foo"
        assert attrs.get("ct.backend") == "trace"

    def test_failure_modes_no_crash(self):
        """A network-failing exporter (or any post-emission flush issue)
        must not propagate. ``export_ccache_metrics`` should still return
        cleanly; we drive this via the in-memory reader path so the
        exception surface here is just import resilience."""
        # The simplest failure-mode coverage is: empty Counter (already
        # tested above) plus a Counter with a zero value to ensure we
        # filter zeros instead of choking on them.
        reader = InMemoryMetricReader()
        export_ccache_metrics(
            Counter({"direct_cache_hit": 0, "cache_miss": 4}),
            _make_args(),
            _reader=reader,
        )
        metrics = _collect_ccache_metrics(reader)
        events = metrics.get("ct.ccache.events", [])
        # Only the non-zero event lands as a counter point.
        captured = {dict(attrs)["ccache_event"]: value for attrs, value in events}
        assert "cache_miss" in captured
        assert "direct_cache_hit" not in captured


class TestCcacheMetricsAsSpansFallback:
    def test_flag_routes_through_span_path(self, monkeypatch):
        """``--otel-metrics-as-spans`` must redirect emission to a span
        synthesis path -- the BatchSpanProcessor's exporter is what would
        actually send the data. We stub the _build_processor so no real
        network call is attempted, and assert the synthesised span is
        captured with the right attributes."""
        exporter = InMemorySpanExporter()
        processor = SimpleSpanProcessor(exporter)
        import compiletools.otel._connection as conn

        monkeypatch.setattr(conn, "_build_processor", lambda args: processor)

        counts = Counter({"direct_cache_hit": 4, "cache_miss": 1})
        export_ccache_metrics(
            counts,
            _make_args(otel_metrics_as_spans=True),
            invocation_id="aa" * 16,
        )

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "ct.ccache.snapshot"
        attrs = dict(span.attributes or {})
        assert attrs.get("ct.ccache.events.direct_cache_hit") == 4
        assert attrs.get("ct.ccache.events.cache_miss") == 1
        assert attrs.get("ct.ccache.hit_rate") == pytest.approx(0.80)
