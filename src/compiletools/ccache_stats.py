"""Parse ``CCACHE_STATSLOG`` files into per-event counts.

ccache's ``CCACHE_STATSLOG`` env var causes the compiler wrapper to
append one event name per cache lookup to a log file -- e.g.::

    # /path/to/source.cpp
    direct_cache_hit
    # /path/to/other.cpp
    cache_miss
    local_storage_miss
    local_storage_write
    remote_storage_miss

Each compile produces exactly one *primary outcome*
(``direct_cache_hit`` / ``preprocessed_cache_hit`` / ``cache_miss``)
plus zero-or-more *secondary events* describing what the local/remote
storage layers did. ``#`` lines mark the start of a per-invocation block
and are ignored -- we deliberately do not break out per-source counts
because it would explode metric tag cardinality.

Stdlib-only by design so this module is testable without the OTel
extra and reusable by a future ``ct-ccache-report`` tool.
"""

from __future__ import annotations

import os
from collections import Counter

# Primary outcome counters. Exactly one of these is emitted per compile
# invocation in ccache 4.x, so summing them yields the cacheable-call count.
_PRIMARY_OUTCOMES = (
    "direct_cache_hit",
    "preprocessed_cache_hit",
    "cache_miss",
)

# Secondary signals -- multiple per call. Useful for spotting whether
# the remote backend is actually being read/written.
_SECONDARY_EVENTS = (
    "local_storage_hit",
    "local_storage_miss",
    "local_storage_write",
    "remote_storage_hit",
    "remote_storage_miss",
    "remote_storage_write",
    "remote_storage_error",
    "remote_storage_timeout",
)

# Convenience: all event names we explicitly recognise. ``parse_statslog``
# does NOT filter to this set -- new ccache versions add events freely and
# we want them to flow through unchanged. The constant is exported only so
# callers (e.g. hit-rate computations) can iterate the well-known set.
ALL_KNOWN_EVENTS = _PRIMARY_OUTCOMES + _SECONDARY_EVENTS


def parse_statslog(path: str) -> Counter[str]:
    """Return ``{event_name: count}`` from a ccache stats log.

    Missing or unreadable file returns an empty ``Counter`` (ccache may
    simply not have run -- e.g. a fully-CAS-served build, or a build that
    aborted before any compile fired). Malformed lines (e.g. one exceeding
    the local FS line buffer due to a concurrent partial write) are
    silently skipped: a single bad line must not destroy the rest of the
    aggregation. ``#`` comment lines marking per-source blocks are
    ignored.
    """
    counts: Counter[str] = Counter()
    if not path or not os.path.exists(path):
        return counts
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Event names are single tokens (ASCII identifier chars).
                # A line containing whitespace or unexpected characters is
                # almost certainly a partial write from a concurrent
                # appender; skip rather than poison the counter.
                if any(c.isspace() for c in line):
                    continue
                counts[line] += 1
    except OSError:
        # Best-effort: a read error mid-build still produces whatever we
        # managed to consume above, but if open() itself fails we return
        # the (possibly empty) accumulator unchanged.
        pass
    return counts


def hit_rate(counts: Counter[str]) -> float:
    """Return the local hit ratio in ``[0.0, 1.0]``.

    Defined as ``(direct_cache_hit + preprocessed_cache_hit) / cacheable``
    where ``cacheable`` is the sum of the three primary outcomes. Returns
    ``0.0`` when the build did no cacheable compiles (avoids div-by-zero).
    """
    direct = counts.get("direct_cache_hit", 0)
    preproc = counts.get("preprocessed_cache_hit", 0)
    miss = counts.get("cache_miss", 0)
    cacheable = direct + preproc + miss
    if cacheable <= 0:
        return 0.0
    return (direct + preproc) / cacheable


def remote_hit_rate(counts: Counter[str]) -> float:
    """Return the remote-backend hit ratio in ``[0.0, 1.0]``.

    Defined as ``remote_storage_hit / (remote_storage_hit + remote_storage_miss)``.
    Returns ``0.0`` when no remote lookups happened (no remote configured,
    or every call was served from local storage before reaching the remote).
    """
    hits = counts.get("remote_storage_hit", 0)
    misses = counts.get("remote_storage_miss", 0)
    total = hits + misses
    if total <= 0:
        return 0.0
    return hits / total


def summary_attributes(counts: Counter[str]) -> dict[str, int | float]:
    """Return a flat ``{ct.ccache.<key>: value}`` dict for span attributes.

    Suitable for ``span.set_attribute(k, v)`` in a loop -- all values are
    OTel-compatible scalars (int/float). Keys mirror the in-process
    ``build_stats_payload`` field set for query continuity across the two
    publishing paths (UDP-to-InfluxDB and OTLP-to-collector).
    """
    direct = counts.get("direct_cache_hit", 0)
    preproc = counts.get("preprocessed_cache_hit", 0)
    miss = counts.get("cache_miss", 0)
    cacheable = direct + preproc + miss
    return {
        "ct.ccache.cacheable_calls": cacheable,
        "ct.ccache.direct_hits": direct,
        "ct.ccache.preprocessed_hits": preproc,
        "ct.ccache.misses": miss,
        "ct.ccache.local_writes": counts.get("local_storage_write", 0),
        "ct.ccache.remote_hits": counts.get("remote_storage_hit", 0),
        "ct.ccache.remote_misses": counts.get("remote_storage_miss", 0),
        "ct.ccache.remote_writes": counts.get("remote_storage_write", 0),
        "ct.ccache.remote_errors": counts.get("remote_storage_error", 0),
        "ct.ccache.remote_timeouts": counts.get("remote_storage_timeout", 0),
        "ct.ccache.hit_rate": hit_rate(counts),
        "ct.ccache.remote_hit_rate": remote_hit_rate(counts),
    }
