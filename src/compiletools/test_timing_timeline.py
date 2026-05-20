"""Tests for timing_timeline module."""

from __future__ import annotations

import asyncio

import pytest

from compiletools.build_timer import BuildTimer, TimingEvent
from compiletools.timing_timeline import (
    _coalesce,
    _format_short,
    _format_time,
    _truncate,
    flatten_events,
    pack_lanes,
    pick_tick_interval,
    s_to_col,
)

# ---------------------------------------------------------------- pure helpers


class TestFlattenEvents:
    def test_empty(self):
        assert flatten_events([]) == []

    def test_single_phase_with_rules(self):
        rule_a = TimingEvent(name="a.o", category="compile", start_s=1.0, end_s=2.0)
        rule_b = TimingEvent(name="b.o", category="compile", start_s=2.0, end_s=3.0)
        phase = TimingEvent(name="build", category="phase", start_s=0.0, end_s=4.0, children=[rule_a, rule_b])
        result = flatten_events([phase])
        assert result == [rule_a, rule_b]

    def test_nested_phases(self):
        rule = TimingEvent(name="x", category="compile", start_s=0.0, end_s=1.0)
        inner = TimingEvent(name="inner", category="phase", start_s=0.0, end_s=1.0, children=[rule])
        outer = TimingEvent(name="outer", category="phase", start_s=0.0, end_s=1.0, children=[inner])
        assert flatten_events([outer]) == [rule]

    def test_skips_phase_keeps_rules_with_children(self):
        # Rare: a rule with sub-rule children — both should be flattened.
        leaf = TimingEvent(name="leaf", category="compile", start_s=0.5, end_s=0.8)
        rule = TimingEvent(name="parent", category="link", start_s=0.0, end_s=1.0, children=[leaf])
        phase = TimingEvent(name="p", category="phase", start_s=0.0, end_s=1.0, children=[rule])
        assert flatten_events([phase]) == [rule, leaf]


class TestPackLanes:
    def test_empty(self):
        assert pack_lanes([]) == []

    def test_single(self):
        e = TimingEvent(name="a", category="compile", start_s=0.0, end_s=1.0)
        assert pack_lanes([e]) == [0]

    def test_sequential_share_lane(self):
        a = TimingEvent(name="a", category="compile", start_s=0.0, end_s=1.0)
        b = TimingEvent(name="b", category="compile", start_s=1.0, end_s=2.0)
        c = TimingEvent(name="c", category="compile", start_s=2.0, end_s=3.0)
        assert pack_lanes([a, b, c]) == [0, 0, 0]

    def test_overlapping_use_separate_lanes(self):
        a = TimingEvent(name="a", category="compile", start_s=0.0, end_s=2.0)
        b = TimingEvent(name="b", category="compile", start_s=0.5, end_s=1.5)
        c = TimingEvent(name="c", category="compile", start_s=1.0, end_s=3.0)
        # All three overlap somewhere → 3 distinct lanes.
        result = pack_lanes([a, b, c])
        assert len(set(result)) == 3

    def test_reuse_freed_lane(self):
        a = TimingEvent(name="a", category="compile", start_s=0.0, end_s=2.0)
        b = TimingEvent(name="b", category="compile", start_s=0.5, end_s=1.0)
        c = TimingEvent(name="c", category="compile", start_s=1.5, end_s=2.5)
        # b finishes at 1.0, c starts at 1.5 → c can reuse b's lane.
        # a still in lane 0 over [0,2], so c lands on lane 1 (= b's old lane).
        result = pack_lanes([a, b, c])
        assert result == [0, 1, 1]

    def test_results_are_parallel_to_input(self):
        # pack_lanes processes events in start-time order internally but
        # must return lanes parallel to the *input* order.
        b = TimingEvent(name="b", category="compile", start_s=2.0, end_s=3.0)
        a = TimingEvent(name="a", category="compile", start_s=0.0, end_s=1.0)
        result = pack_lanes([b, a])
        # Both run at non-overlapping times, share lane 0.
        assert result == [0, 0]


class TestPickTickInterval:
    def test_zero_duration(self):
        assert pick_tick_interval(0.0) > 0  # never returns 0

    @pytest.mark.parametrize(
        ("duration_s", "expected"),
        [
            pytest.param(0.5, 0.1, id="short-view"),
            pytest.param(60.0, 10.0, id="minute-view"),
            pytest.param(360_000.0, 3600.0, id="capped-at-max"),
        ],
    )
    def test_picks_nice_value(self, duration_s, expected):
        assert pick_tick_interval(duration_s) == expected


class TestSToCol:
    @pytest.mark.parametrize(
        ("seconds", "origin_s", "seconds_per_col", "expected"),
        [
            pytest.param(5.0, 0.0, 1.0, 5.0, id="basic"),
            pytest.param(5.0, 2.0, 1.0, 3.0, id="with-origin"),
            pytest.param(5.0, 0.0, 0.5, 10.0, id="with-zoom"),
            pytest.param(5.0, 0.0, 0.0, 0.0, id="zero-seconds-per-col"),
        ],
    )
    def test_s_to_col(self, seconds, origin_s, seconds_per_col, expected):
        assert s_to_col(seconds, origin_s=origin_s, seconds_per_col=seconds_per_col) == expected


class TestTruncate:
    @pytest.mark.parametrize(
        ("text", "max_width", "expected"),
        [
            pytest.param("hello", 10, "hello", id="short-unchanged"),
            pytest.param("hello", 5, "hello", id="exact-fit"),
            pytest.param("hello world", 7, "hello …"[:7], id="ellipsis"),
            pytest.param("hello", 1, "…", id="max-width-one"),
            pytest.param("hello", 0, "", id="max-width-zero"),
        ],
    )
    def test_truncate(self, text, max_width, expected):
        assert _truncate(text, max_width) == expected


class TestFormatHelpers:
    @pytest.mark.parametrize(
        ("seconds", "expected_parts"),
        [
            pytest.param(1.5, ("1.50", "s"), id="seconds"),
            pytest.param(0.05, ("ms",), id="milliseconds"),
            pytest.param(0.0001, ("µs",), id="microseconds"),
        ],
    )
    def test_format_time_contains_unit(self, seconds, expected_parts):
        formatted = _format_time(seconds)
        for part in expected_parts:
            assert part in formatted

    def test_format_time_minutes(self):
        # 90s → 1m30.0s
        assert _format_time(90.0).startswith("1m")

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            pytest.param(5.0, "5s", id="seconds"),
            pytest.param(0.05, "50ms", id="milliseconds"),
            pytest.param(0.0, "0", id="zero"),
        ],
    )
    def test_format_short(self, seconds, expected):
        assert _format_short(seconds) == expected


class TestCoalesce:
    def test_empty(self):
        assert _coalesce([]) == []

    def test_merges_identical_styles(self):
        from rich.style import Style

        s = Style(color="red")
        cells = [(s, "a"), (s, "b"), (s, "c")]
        result = _coalesce(cells)
        assert len(result) == 1
        assert result[0].text == "abc"

    def test_splits_on_style_change(self):
        from rich.style import Style

        s1 = Style(color="red")
        s2 = Style(color="blue")
        cells = [(s1, "a"), (s2, "b"), (s2, "c"), (s1, "d")]
        result = _coalesce(cells)
        assert len(result) == 3
        assert result[0].text == "a"
        assert result[1].text == "bc"
        assert result[2].text == "d"


# ----------------------------------------------------------- screen smoke test


def _make_timer() -> BuildTimer:
    """Build a small but realistic timing fixture with parallel rules."""
    timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
    with timer.phase("config_resolution"):
        pass
    # Manually craft phase-with-rules to control timestamps directly
    # without the with-block's monotonic anchoring (which would conflict
    # with our test-supplied start_s values).
    with timer.phase("build_execution"):
        timer.record_rule("compile", "a.o", "a.cpp", elapsed_s=1.0, start_s=0.0, end_s=1.0)
        timer.record_rule("compile", "b.o", "b.cpp", elapsed_s=1.0, start_s=0.0, end_s=1.0)  # parallel with a
        timer.record_rule("compile", "c.o", "c.cpp", elapsed_s=0.5, start_s=1.0, end_s=1.5)  # sequential after a
        timer.record_rule("link", "app", "", elapsed_s=0.2, start_s=1.5, end_s=1.7)
    timer.finish()
    return timer


def _run_async(coro):
    """Run an async function from a sync test without needing pytest-asyncio.

    Textual's ``App.run_test()`` is an async context manager, but the
    project doesn't depend on pytest-asyncio.  asyncio.run() is enough
    for our smoke tests and keeps the dependency surface unchanged.
    """
    asyncio.run(coro())


class TestTimelineScreen:
    def test_screen_mounts_and_renders(self):
        pytest.importorskip("textual")
        from textual.app import App

        from compiletools.timing_timeline import TimelineCanvas, TimelineScreen

        timer = _make_timer()

        class _Host(App):
            def on_mount(self):
                self.push_screen(TimelineScreen(timer))

        async def go():
            app = _Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                canvas = app.screen.query_one(TimelineCanvas)
                assert canvas.num_lanes == 2  # a and b run in parallel
                assert len(canvas.events) == 4
                assert canvas.selected_idx == 0

        _run_async(go)

    def test_zoom_and_fit_actions(self):
        pytest.importorskip("textual")
        from textual.app import App

        from compiletools.timing_timeline import TimelineCanvas, TimelineScreen

        timer = _make_timer()

        class _Host(App):
            def on_mount(self):
                self.push_screen(TimelineScreen(timer))

        async def go():
            app = _Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                canvas = app.screen.query_one(TimelineCanvas)
                initial_spc = canvas.seconds_per_col
                await pilot.press("plus")
                await pilot.pause()
                assert canvas.seconds_per_col < initial_spc
                await pilot.press("0")
                await pilot.pause()
                assert canvas.seconds_per_col == pytest.approx(initial_spc, rel=0.01)

        _run_async(go)

    def test_selection_movement(self):
        pytest.importorskip("textual")
        from textual.app import App

        from compiletools.timing_timeline import TimelineCanvas, TimelineScreen

        timer = _make_timer()

        class _Host(App):
            def on_mount(self):
                self.push_screen(TimelineScreen(timer))

        async def go():
            app = _Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                canvas = app.screen.query_one(TimelineCanvas)
                assert canvas.selected_idx == 0
                await pilot.press("n")
                await pilot.pause()
                assert canvas.selected_idx == 1
                await pilot.press("p")
                await pilot.pause()
                assert canvas.selected_idx == 0
                await pilot.press("end")
                await pilot.pause()
                assert canvas.selected_idx == len(canvas.events) - 1

        _run_async(go)


class TestEmptyTimer:
    def test_screen_handles_empty_timer(self):
        """A timer with no rules should still mount without crashing."""
        pytest.importorskip("textual")
        from textual.app import App

        from compiletools.timing_timeline import TimelineCanvas, TimelineScreen

        timer = BuildTimer(enabled=True)
        with timer.phase("config_resolution"):
            pass
        timer.finish()

        class _Host(App):
            def on_mount(self):
                self.push_screen(TimelineScreen(timer))

        async def go():
            app = _Host()
            async with app.run_test() as pilot:
                await pilot.pause()
                canvas = app.screen.query_one(TimelineCanvas)
                assert canvas.num_lanes == 0
                assert canvas.events == []
                await pilot.press("n")
                await pilot.pause()

        _run_async(go)
