"""Cross-layer cache aggregates derived from per-rule CAS hits (P2) and
build-wide ccache event counts (P4).

P2 emits ``cas.hit`` / ``cas.kind`` / ``cas.bytes_reused`` on each rule
span when the CAS short-circuit fires.  P4 emits a build-wide histogram
of ccache events (``direct_cache_hit``, ``preprocessed_cache_hit``,
``cache_miss``, ...).  The two cache layers are nested -- a CAS hit
means the compiler never ran, so ccache never saw the TU; a CAS miss
can still be a ccache hit because compiletools' lock wrapper invokes
the real compiler under ccache.

P5 turns the two signals into a single user-facing headline -- "what
fraction of TUs did caching save?" -- so dashboards do not have to
re-derive the join every query.  No new collection happens here; P5
is a pure post-build aggregation pass.

Emitted signals
---------------

Root build span attributes (lifted via the ``timer._root.metadata``
mechanism that P4 already added to ``otel/traces.py``):

* ``ct.build.cas_avoided_count`` -- compile rules with ``cas.hit==True``.
* ``ct.build.ccache_avoided_count`` -- ccache direct + preprocessed hits.
* ``ct.build.recompiled_count`` -- compile rules where neither cache
  saved work (best-effort; see the per-rule attribution caveat below).
* ``ct.build.compile_avoided_rate`` -- ``(cas+ccache) / total`` clamped
  to ``[0, 1]``.  ``0.0`` when ``total == 0`` (avoids div-by-zero).

Per-rule span attribute (lifted via the per-rule metadata loop in
``_emit_event``):

* ``ct.rule.cache_layer`` -- ``"cas"`` for CAS hits, ``"other"`` for
  CAS misses on compile rules.

Per-rule ccache attribution caveat
----------------------------------

ccache's statslog is a build-wide event stream with no per-target
binding -- one ``cache_miss`` line means *some* TU missed, not *which*
TU.  So ``ct.rule.cache_layer`` distinguishes ``cas`` (precise: the
rule's metadata carries ``cas.hit==True``) from ``other`` (everything
else); the ``ccache`` vs ``compiled`` split is only reported at the
root-span level via ``ccache_avoided_count`` / ``recompiled_count``.
``derive_rule_cache_layer`` accepts an explicit ``ccache_match``
parameter for the trace_backend case where per-target attribution
*might* one day be wired up, but the production caller passes ``None``
for ninja/make builds.

Partial-data behaviour
----------------------

Both P2 and P4 are optional.  The four root attrs are always emitted
(even as zeros) so dashboards can distinguish "build did not aggregate"
from "build aggregated to zero".  Specifically:

* P2 data absent (no ``cas.hit`` on any rule, e.g. cmake/bazel
  backends): ``cas_avoided_count == 0``; every compile rule counts as
  "not CAS-saved" when computing ``recompiled_count``.
* P4 data absent (``--ccache-statslog`` not set): ``ccache_avoided_count
  == 0``; ``compile_avoided_rate`` reflects CAS-only savings.
* Both absent: all four attrs are ``0``/``0.0``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from compiletools.build_timer import BuildTimer, TimingEvent


# Rule categories that represent a compiler invocation (and therefore
# can be CAS- or ccache-saved).  Linking/archiving rules do not flow
# through ccache and CAS-skipping them is a different signal, so they
# are intentionally excluded from the "compile-avoided" denominator.
_COMPILE_CATEGORIES: frozenset[str] = frozenset({"compile"})

# Ccache event names that count as the compiler-output-reused outcome.
# ``preprocessed_cache_hit`` is the slow-path (preprocessor ran, then
# cache hit on the preprocessed source); both still represent "compiler
# back-end never ran" from the build-savings perspective so they sum
# into ``ccache_avoided_count``.
_CCACHE_HIT_EVENTS: tuple[str, ...] = (
    "direct_cache_hit",
    "preprocessed_cache_hit",
)


def _iter_rules(event: TimingEvent) -> list[TimingEvent]:
    """Flatten a TimingEvent subtree to its non-phase leaves.

    Mirrors the walk shape used by ``BuildTimer.print_summary``: phases
    nest arbitrarily, rules are the leaves.  Returns a list (not a
    generator) so the caller can iterate multiple times cheaply.
    """
    out: list[TimingEvent] = []

    def _walk(node: TimingEvent) -> None:
        if node.category == "phase":
            for child in node.children:
                _walk(child)
        else:
            out.append(node)

    _walk(event)
    return out


def derive_build_aggregates(
    timer: BuildTimer,
    ccache_counts: dict[str, int] | None,
) -> dict[str, Any]:
    """Compute the four root-span cross-layer cache aggregate attributes.

    Walks ``timer._root`` for ``cas.hit`` metadata on compile rules and
    cross-references the build-wide ``ccache_counts`` mapping (as
    produced by ``compiletools.ccache_stats.parse_statslog``).

    Returns a dict ready to merge into ``timer._root.metadata`` -- the
    P4 root-event metadata-lift in ``otel/traces.py:export_buildtimer``
    will then set each entry as a root-span attribute.

    Partial-data behaviour is specified in this module's docstring.
    Honest under-counting is preferred to crashing or hiding the data
    point entirely.
    """
    rules = _iter_rules(timer._root) if timer is not None and timer.enabled else []
    compile_rules = [r for r in rules if r.category in _COMPILE_CATEGORIES]

    cas_avoided = sum(1 for r in compile_rules if bool(r.metadata.get("cas.hit")))

    counts = ccache_counts or {}
    ccache_avoided = sum(int(counts.get(name, 0)) for name in _CCACHE_HIT_EVENTS)

    total = len(compile_rules)
    # ``recompiled_count`` is best-effort: anything not CAS-saved and
    # not ccache-saved.  Because ccache attribution is build-wide we
    # subtract the build-wide ccache_avoided from the build-wide CAS
    # misses; the resulting count is correct in aggregate but cannot be
    # broken back down per-rule.  Floor at zero so a degenerate
    # statslog reporting more hits than rules (e.g. user reused the
    # log across two builds) does not yield a negative count.
    cas_misses = total - cas_avoided
    recompiled = max(0, cas_misses - ccache_avoided)

    if total > 0:
        avoided_rate = (cas_avoided + ccache_avoided) / total
        # Clamp to [0, 1] in case ccache_avoided exceeds the compile-rule
        # count for the same degenerate-statslog reason above.
        if avoided_rate > 1.0:
            avoided_rate = 1.0
        elif avoided_rate < 0.0:
            avoided_rate = 0.0
    else:
        avoided_rate = 0.0

    return {
        "ct.build.cas_avoided_count": int(cas_avoided),
        "ct.build.ccache_avoided_count": int(ccache_avoided),
        "ct.build.recompiled_count": int(recompiled),
        "ct.build.compile_avoided_rate": float(avoided_rate),
    }


def derive_rule_cache_layer(
    timing_event: TimingEvent,
    ccache_attribution: bool | None = None,
) -> str:
    """Return the ``ct.rule.cache_layer`` value for one rule TimingEvent.

    * Returns ``"cas"`` when the rule's metadata has ``cas.hit == True``
      (the CAS short-circuit fired and the compiler never ran).
    * Returns ``"ccache"`` when ``ccache_attribution`` is ``True``
      (caller has resolved per-target ccache attribution -- only
      trace_backend can do this today, ninja/make pass ``None``).
    * Returns ``"compiled"`` when ``ccache_attribution`` is ``False``
      (per-target attribution available and ccache missed).
    * Returns ``"other"`` when the rule had a CAS miss and per-target
      ccache attribution is not available (``ccache_attribution is
      None``).  This is the common path for ninja/make builds.
    """
    if bool(timing_event.metadata.get("cas.hit")):
        return "cas"
    if ccache_attribution is True:
        return "ccache"
    if ccache_attribution is False:
        return "compiled"
    return "other"


def annotate_rule_cache_layers(
    timer: BuildTimer,
    ccache_attribution: dict[str, bool] | None = None,
) -> int:
    """Attach ``ct.rule.cache_layer`` to every compile rule's metadata.

    Walks ``timer._root``, computes ``derive_rule_cache_layer`` for each
    compile rule, and writes the result into ``event.metadata`` so the
    per-rule metadata-lift loop in ``otel/traces.py:_emit_event`` picks
    it up at export time -- no exporter changes needed for P5.

    ``ccache_attribution`` is an optional ``{target: hit}`` map used by
    callers who *can* attribute ccache events per-target (a future
    trace_backend enhancement).  Production callers today pass ``None``
    so every CAS-miss compile rule lands as ``"other"`` (see the module
    docstring's per-rule ccache attribution caveat).

    Returns the number of rules annotated; cheap nudge for the caller's
    verbose output without forcing a second walk of the tree.
    """
    if timer is None or not timer.enabled:
        return 0

    rules = _iter_rules(timer._root)
    annotated = 0
    for rule in rules:
        if rule.category not in _COMPILE_CATEGORIES:
            continue
        target_hit: bool | None = None
        if ccache_attribution is not None and rule.target:
            target_hit = ccache_attribution.get(rule.target)
        rule.metadata["ct.rule.cache_layer"] = derive_rule_cache_layer(rule, target_hit)
        annotated += 1
    return annotated
