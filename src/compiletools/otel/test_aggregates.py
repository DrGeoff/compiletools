"""Tests for compiletools.otel.aggregates (P5 cross-layer cache aggregates).

These tests use the in-memory ``BuildTimer`` API to construct synthetic
rule trees and verify the four root-span aggregates plus the per-rule
``ct.rule.cache_layer`` annotation under every documented partial-data
shape.
"""

from __future__ import annotations

from compiletools.build_timer import BuildTimer, TimingEvent
from compiletools.otel.aggregates import (
    annotate_rule_cache_layers,
    derive_build_aggregates,
    derive_rule_cache_layer,
)


# ---------------------------------------------------------------- test helpers


def _make_timer(rule_specs: list[dict]) -> BuildTimer:
    """Build a timer with one ``build_execution`` phase holding the given rules.

    Each spec is ``{"rule_type": ..., "target": ..., "source": ...,
    "cas_hit": bool | None}``.  When ``cas_hit`` is omitted the rule has
    no cas.* metadata (P2 was absent for that rule).
    """
    timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
    base = timer._root.start_s
    with timer.phase("build_execution"):
        for idx, spec in enumerate(rule_specs):
            md: dict = {}
            if "cas_hit" in spec and spec["cas_hit"] is not None:
                md["cas.hit"] = bool(spec["cas_hit"])
                md["cas.kind"] = spec.get("cas_kind", "obj")
                md["cas.bytes_reused"] = spec.get("cas_bytes_reused", 0)
            timer.record_rule(
                rule_type=spec.get("rule_type", "compile"),
                target=spec.get("target", f"obj/r{idx}.o"),
                source=spec.get("source", f"src/r{idx}.cpp"),
                elapsed_s=0.1,
                start_s=base + idx,
                end_s=base + idx + 0.1,
                metadata=md or None,
            )
    timer.finish()
    return timer


# ------------------------------------------------------------- derive_build_aggregates


class TestDeriveBuildAggregates:
    def test_all_cas_hits(self):
        timer = _make_timer(
            [
                {"cas_hit": True},
                {"cas_hit": True},
                {"cas_hit": True},
            ]
        )
        attrs = derive_build_aggregates(timer, ccache_counts={})
        assert attrs["ct.build.cas_avoided_count"] == 3
        assert attrs["ct.build.ccache_avoided_count"] == 0
        assert attrs["ct.build.recompiled_count"] == 0
        assert attrs["ct.build.compile_avoided_rate"] == 1.0

    def test_all_misses_no_ccache(self):
        timer = _make_timer(
            [
                {"cas_hit": False},
                {"cas_hit": False},
                {"cas_hit": False},
            ]
        )
        attrs = derive_build_aggregates(timer, ccache_counts={"cache_miss": 3})
        assert attrs["ct.build.cas_avoided_count"] == 0
        assert attrs["ct.build.ccache_avoided_count"] == 0
        assert attrs["ct.build.recompiled_count"] == 3
        assert attrs["ct.build.compile_avoided_rate"] == 0.0

    def test_mixed_cas_and_ccache(self):
        # 5 compile rules: 2 CAS-hit, 3 CAS-miss; ccache reports 2 hits
        # (direct + preprocessed) and 1 miss, so 1 rule was a true recompile.
        timer = _make_timer(
            [
                {"cas_hit": True},
                {"cas_hit": True},
                {"cas_hit": False},
                {"cas_hit": False},
                {"cas_hit": False},
            ]
        )
        attrs = derive_build_aggregates(
            timer,
            ccache_counts={
                "direct_cache_hit": 1,
                "preprocessed_cache_hit": 1,
                "cache_miss": 1,
            },
        )
        assert attrs["ct.build.cas_avoided_count"] == 2
        assert attrs["ct.build.ccache_avoided_count"] == 2
        assert attrs["ct.build.recompiled_count"] == 1
        assert attrs["ct.build.compile_avoided_rate"] == 4 / 5

    def test_p2_absent_ccache_only(self):
        # cmake/bazel-style: rules exist but no cas.hit on any of them.
        timer = _make_timer(
            [
                {},
                {},
                {},
                {},
            ]
        )
        attrs = derive_build_aggregates(
            timer,
            ccache_counts={"direct_cache_hit": 2, "cache_miss": 2},
        )
        assert attrs["ct.build.cas_avoided_count"] == 0
        assert attrs["ct.build.ccache_avoided_count"] == 2
        assert attrs["ct.build.recompiled_count"] == 2  # 4 cas-miss - 2 ccache hit
        assert attrs["ct.build.compile_avoided_rate"] == 0.5

    def test_p4_absent_cas_only(self):
        # No --ccache-statslog: ccache_counts is None.
        timer = _make_timer(
            [
                {"cas_hit": True},
                {"cas_hit": True},
                {"cas_hit": False},
                {"cas_hit": False},
            ]
        )
        attrs = derive_build_aggregates(timer, ccache_counts=None)
        assert attrs["ct.build.cas_avoided_count"] == 2
        assert attrs["ct.build.ccache_avoided_count"] == 0
        assert attrs["ct.build.recompiled_count"] == 2
        assert attrs["ct.build.compile_avoided_rate"] == 0.5

    def test_both_absent_treats_all_rules_as_recompiled(self):
        # cmake/bazel build with no ccache_statslog: still emit zeros so
        # dashboards can distinguish "didn't aggregate" from "aggregated
        # to zero".
        timer = _make_timer([{}, {}, {}])
        attrs = derive_build_aggregates(timer, ccache_counts=None)
        assert attrs["ct.build.cas_avoided_count"] == 0
        assert attrs["ct.build.ccache_avoided_count"] == 0
        # All three rules were not CAS-saved and no ccache layer
        # attribution exists, so each one is a presumed recompile.
        assert attrs["ct.build.recompiled_count"] == 3
        assert attrs["ct.build.compile_avoided_rate"] == 0.0

    def test_empty_build_no_div_by_zero(self):
        # No rules at all (e.g. ct-cake invoked with no targets to build).
        timer = _make_timer([])
        attrs = derive_build_aggregates(timer, ccache_counts=None)
        assert attrs == {
            "ct.build.cas_avoided_count": 0,
            "ct.build.ccache_avoided_count": 0,
            "ct.build.recompiled_count": 0,
            "ct.build.compile_avoided_rate": 0.0,
        }

    def test_link_rules_excluded_from_denominator(self):
        # Only compile rules count toward the avoidance rate; a single
        # link rule should not show up in cas_avoided / recompiled.
        timer = _make_timer(
            [
                {"rule_type": "compile", "cas_hit": True},
                {"rule_type": "compile", "cas_hit": False},
                {"rule_type": "link", "cas_hit": False, "target": "bin/app", "source": ""},
            ]
        )
        attrs = derive_build_aggregates(timer, ccache_counts=None)
        assert attrs["ct.build.cas_avoided_count"] == 1
        assert attrs["ct.build.recompiled_count"] == 1
        assert attrs["ct.build.compile_avoided_rate"] == 0.5

    def test_disabled_timer_emits_zeros(self):
        timer = BuildTimer(enabled=False)
        attrs = derive_build_aggregates(timer, ccache_counts={"direct_cache_hit": 7})
        # A disabled timer has no rule tree to walk; nothing to attribute.
        assert attrs["ct.build.cas_avoided_count"] == 0
        assert attrs["ct.build.recompiled_count"] == 0
        assert attrs["ct.build.compile_avoided_rate"] == 0.0
        # The ccache count still flows through -- it's build-wide and
        # doesn't depend on the rule tree being populated.
        assert attrs["ct.build.ccache_avoided_count"] == 7

    def test_ccache_overcount_clamped_to_total(self):
        # Pathological: statslog reports more hits than there are compile
        # rules (e.g. reused stale statslog).  recompiled stays ≥ 0 and
        # the avoided_rate is clamped to 1.0.  The clamp also raises a
        # structured warning attribute so dashboards don't silently treat
        # the resulting 100% as a real signal.
        timer = _make_timer([{"cas_hit": False}, {"cas_hit": False}])
        attrs = derive_build_aggregates(
            timer, ccache_counts={"direct_cache_hit": 99}
        )
        assert attrs["ct.build.recompiled_count"] == 0
        assert attrs["ct.build.compile_avoided_rate"] == 1.0
        assert attrs["ct.build.aggregate_warning"] == "ccache_overcount"

    def test_no_aggregate_warning_when_counts_consistent(self):
        # In the well-formed case (ccache_avoided <= cas_misses) the
        # warning attribute must be absent so dashboards can use its
        # presence as a reliable "something is off" signal.
        timer = _make_timer(
            [
                {"cas_hit": True},
                {"cas_hit": False},
                {"cas_hit": False},
            ]
        )
        attrs = derive_build_aggregates(
            timer, ccache_counts={"direct_cache_hit": 1, "cache_miss": 1}
        )
        assert "ct.build.aggregate_warning" not in attrs


# -------------------------------------------------------------- derive_rule_cache_layer


class TestDeriveRuleCacheLayer:
    def _evt(self, cas_hit: bool | None) -> TimingEvent:
        md: dict = {}
        if cas_hit is not None:
            md["cas.hit"] = cas_hit
        return TimingEvent(
            name="r",
            category="compile",
            start_s=0.0,
            end_s=0.1,
            target="obj/r.o",
            source="src/r.cpp",
            metadata=md,
        )

    def test_cas_hit_returns_cas(self):
        assert derive_rule_cache_layer(self._evt(True), ccache_attribution=None) == "cas"

    def test_cas_hit_takes_precedence_over_ccache_attribution(self):
        # If the CAS short-circuit fired, ccache never ran -- even if the
        # caller (incorrectly) passes a True attribution, we still report
        # "cas" since that's the layer that actually saved the work.
        assert derive_rule_cache_layer(self._evt(True), ccache_attribution=True) == "cas"

    def test_cas_miss_no_ccache_attribution_returns_other(self):
        assert (
            derive_rule_cache_layer(self._evt(False), ccache_attribution=None)
            == "other"
        )

    def test_no_cas_metadata_returns_other(self):
        # Missing metadata is treated like CAS-miss (P2 absent for this rule).
        assert (
            derive_rule_cache_layer(self._evt(None), ccache_attribution=None)
            == "other"
        )

    def test_cas_miss_ccache_hit_returns_ccache(self):
        assert (
            derive_rule_cache_layer(self._evt(False), ccache_attribution=True)
            == "ccache"
        )

    def test_cas_miss_ccache_miss_returns_compiled(self):
        assert (
            derive_rule_cache_layer(self._evt(False), ccache_attribution=False)
            == "compiled"
        )


# ----------------------------------------------------------- annotate_rule_cache_layers


class TestAnnotateRuleCacheLayers:
    def test_annotates_every_compile_rule(self):
        timer = _make_timer(
            [
                {"cas_hit": True},
                {"cas_hit": False},
                {"rule_type": "link", "cas_hit": False, "target": "bin/app", "source": ""},
            ]
        )
        n = annotate_rule_cache_layers(timer, ccache_attribution=None)
        # 2 compile rules annotated; link rule skipped.
        assert n == 2
        compile_rules = [
            r
            for phase in timer._root.children
            for r in phase.children
            if r.category == "compile"
        ]
        assert compile_rules[0].metadata["ct.rule.cache_layer"] == "cas"
        assert compile_rules[1].metadata["ct.rule.cache_layer"] == "other"
        # The link rule should not have been touched.
        link_rules = [
            r
            for phase in timer._root.children
            for r in phase.children
            if r.category == "link"
        ]
        assert "ct.rule.cache_layer" not in link_rules[0].metadata

    def test_per_target_ccache_attribution_resolves_ccache_vs_compiled(self):
        timer = _make_timer(
            [
                {"cas_hit": False, "target": "obj/a.o"},
                {"cas_hit": False, "target": "obj/b.o"},
            ]
        )
        annotate_rule_cache_layers(
            timer,
            ccache_attribution={"obj/a.o": True, "obj/b.o": False},
        )
        compile_rules = [
            r
            for phase in timer._root.children
            for r in phase.children
        ]
        assert compile_rules[0].metadata["ct.rule.cache_layer"] == "ccache"
        assert compile_rules[1].metadata["ct.rule.cache_layer"] == "compiled"

    def test_disabled_timer_is_noop(self):
        timer = BuildTimer(enabled=False)
        assert annotate_rule_cache_layers(timer, ccache_attribution=None) == 0
