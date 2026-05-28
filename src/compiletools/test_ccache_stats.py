"""Tests for ``compiletools.ccache_stats``.

Stdlib-only: this module deliberately has no OTel dependency so it can
land on installs that haven't picked up the optional ``otel`` extra.
"""

from __future__ import annotations

import os

import pytest

from compiletools import ccache_stats


def _write(path: str, body: str) -> None:
    with open(path, "w") as fh:
        fh.write(body)


class TestParseStatslog:
    def test_missing_file_returns_empty_counter(self, tmp_path):
        # No crash, no warning -- a build that never invoked the compiler
        # (e.g. fully CAS-served) is a legal outcome, not an error.
        counts = ccache_stats.parse_statslog(str(tmp_path / "nonexistent.statslog"))
        assert counts == {}

    def test_empty_file_returns_empty_counter(self, tmp_path):
        p = tmp_path / "empty.statslog"
        _write(str(p), "")
        assert ccache_stats.parse_statslog(str(p)) == {}

    def test_none_path_returns_empty(self):
        # Defensive: a caller passing None (e.g. unset env var) must not
        # raise -- enables the "log set but unset" branch in cake.py.
        assert ccache_stats.parse_statslog(None) == {}  # type: ignore[arg-type]

    def test_single_event(self, tmp_path):
        p = tmp_path / "one.statslog"
        _write(str(p), "direct_cache_hit\n")
        counts = ccache_stats.parse_statslog(str(p))
        assert counts == {"direct_cache_hit": 1}

    def test_repeats_sum(self, tmp_path):
        p = tmp_path / "repeats.statslog"
        _write(
            str(p),
            "cache_miss\ncache_miss\ndirect_cache_hit\ncache_miss\n",
        )
        counts = ccache_stats.parse_statslog(str(p))
        assert counts["cache_miss"] == 3
        assert counts["direct_cache_hit"] == 1

    def test_comment_lines_ignored(self, tmp_path):
        # `# <source-path>` marks the start of a per-call block in ccache
        # 4.x's statslog. We intentionally do NOT correlate events back
        # to sources -- that would explode metric tag cardinality.
        p = tmp_path / "comments.statslog"
        _write(
            str(p),
            "# /path/to/foo.cpp\ndirect_cache_hit\n# /path/to/bar.cpp\ncache_miss\nlocal_storage_write\n",
        )
        counts = ccache_stats.parse_statslog(str(p))
        assert counts == {
            "direct_cache_hit": 1,
            "cache_miss": 1,
            "local_storage_write": 1,
        }

    def test_blank_lines_ignored(self, tmp_path):
        p = tmp_path / "blank.statslog"
        _write(
            str(p),
            "\n\ndirect_cache_hit\n\n\ncache_miss\n",
        )
        counts = ccache_stats.parse_statslog(str(p))
        assert counts == {"direct_cache_hit": 1, "cache_miss": 1}

    def test_malformed_line_skipped(self, tmp_path):
        # A line with embedded whitespace is almost certainly the result
        # of a torn write from a concurrent appender on a fast build.
        # We must skip rather than poison the counter with "unknown".
        p = tmp_path / "torn.statslog"
        _write(
            str(p),
            "direct_cache_hit\ngarbage with spaces here\ncache_miss\n",
        )
        counts = ccache_stats.parse_statslog(str(p))
        assert counts == {"direct_cache_hit": 1, "cache_miss": 1}

    def test_unknown_event_preserved(self, tmp_path):
        # Forward-compatibility: a new ccache release might add events
        # we've never heard of. The parser must not filter them out;
        # downstream OTLP labels them with whatever name ccache wrote.
        p = tmp_path / "future.statslog"
        _write(
            str(p),
            "future_event_name\nfuture_event_name\n",
        )
        counts = ccache_stats.parse_statslog(str(p))
        assert counts == {"future_event_name": 2}


class TestHitRates:
    def test_hit_rate_zero_on_empty(self):
        assert ccache_stats.hit_rate({}) == 0.0  # type: ignore[arg-type]

    def test_hit_rate_no_div_by_zero_on_secondary_only(self):
        # Secondary events present but no cacheable calls -- e.g. a build
        # that never invoked the compiler but emitted some remote-storage
        # bookkeeping. hit_rate must NOT throw.
        counts = {"remote_storage_miss": 5}
        assert ccache_stats.hit_rate(counts) == 0.0  # type: ignore[arg-type]

    def test_hit_rate_typical(self):
        counts = {
            "direct_cache_hit": 60,
            "preprocessed_cache_hit": 10,
            "cache_miss": 30,
        }
        assert ccache_stats.hit_rate(counts) == pytest.approx(0.70)  # type: ignore[arg-type]

    def test_remote_hit_rate_zero_on_empty(self):
        assert ccache_stats.remote_hit_rate({}) == 0.0  # type: ignore[arg-type]
        assert ccache_stats.remote_hit_rate({"cache_miss": 5}) == 0.0  # type: ignore[arg-type]

    def test_remote_hit_rate_typical(self):
        counts = {"remote_storage_hit": 3, "remote_storage_miss": 7}
        assert ccache_stats.remote_hit_rate(counts) == pytest.approx(0.30)  # type: ignore[arg-type]


class TestSummaryAttributes:
    def test_attribute_keys_are_ot_namespaced(self):
        # Every key must start with `ct.ccache.` -- attached directly to
        # the root build span via `span.set_attribute(k, v)`.
        attrs = ccache_stats.summary_attributes({"direct_cache_hit": 1})  # type: ignore[arg-type]
        assert all(k.startswith("ct.ccache.") for k in attrs)

    def test_values_are_scalars(self):
        # OTel set_attribute only accepts primitives (no Counter values).
        attrs = ccache_stats.summary_attributes({"direct_cache_hit": 1, "cache_miss": 2})  # type: ignore[arg-type]
        for v in attrs.values():
            assert isinstance(v, (int, float))

    def test_empty_counts_zero_aggregates(self):
        attrs = ccache_stats.summary_attributes({})  # type: ignore[arg-type]
        assert attrs["ct.ccache.cacheable_calls"] == 0
        assert attrs["ct.ccache.hit_rate"] == 0.0
        assert attrs["ct.ccache.remote_hit_rate"] == 0.0


def test_parse_statslog_against_realistic_ccache_4x_output(tmp_path):
    """Round-trip test using a faithful slice of ccache 4.x statslog output."""
    body = (
        "# /home/user/proj/src/foo.cpp\n"
        "direct_cache_hit\n"
        "local_storage_hit\n"
        "# /home/user/proj/src/bar.cpp\n"
        "cache_miss\n"
        "local_storage_miss\n"
        "local_storage_write\n"
        "remote_storage_miss\n"
        "remote_storage_write\n"
        "# /home/user/proj/src/baz.cpp\n"
        "preprocessed_cache_hit\n"
        "local_storage_hit\n"
    )
    p = tmp_path / "realistic.statslog"
    _write(str(p), body)
    counts = ccache_stats.parse_statslog(str(p))
    assert counts == {
        "direct_cache_hit": 1,
        "preprocessed_cache_hit": 1,
        "cache_miss": 1,
        "local_storage_hit": 2,
        "local_storage_miss": 1,
        "local_storage_write": 1,
        "remote_storage_miss": 1,
        "remote_storage_write": 1,
    }
    assert ccache_stats.hit_rate(counts) == pytest.approx(2 / 3)
    assert ccache_stats.remote_hit_rate(counts) == 0.0


def test_unreadable_file_returns_empty(tmp_path):
    """Permission error on open() must surface as empty, not raise.

    Skipped when running as root (typical CI privilege) because chmod
    cannot revoke read permission from uid 0.
    """
    if os.geteuid() == 0:
        pytest.skip("permission-denied path is unreachable for root")
    p = tmp_path / "noperm.statslog"
    _write(str(p), "direct_cache_hit\n")
    os.chmod(p, 0o000)
    try:
        assert ccache_stats.parse_statslog(str(p)) == {}
    finally:
        os.chmod(p, 0o600)
