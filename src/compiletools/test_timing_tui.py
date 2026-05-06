"""Parity tests: the Textual TUI tree must surface the same per-category
Wall / CPU / parallelism numbers as the static summary table.  Both are
fed by ``BuildTimer.aggregate_by_category``; this test exercises the
populated tree to catch label-format drift between the two views.

These tests stub the TreeNode interface rather than running a full
Textual app, so they don't need pytest-asyncio."""

from __future__ import annotations

import pytest

from compiletools.build_timer import BuildTimer, TimingEvent

pytest.importorskip("textual")  # skip whole module if textual not installed


# ----------------------------------------------------------- TreeNode stub


class StubNode:
    """Records ``add``/``add_leaf`` calls so we can inspect what the
    populator emitted without instantiating a full Textual app."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.children: list[StubNode] = []

    def add(self, label, *_args, **_kwargs) -> StubNode:
        child = StubNode(str(label))
        self.children.append(child)
        return child

    def add_leaf(self, label, *_args, **_kwargs) -> StubNode:
        child = StubNode(str(label))
        self.children.append(child)
        return child


def _flatten_labels(node: StubNode) -> list[str]:
    out = []
    for child in node.children:
        out.append(child.label)
        out.extend(_flatten_labels(child))
    return out


# ----------------------------------------------------------- fixtures


def _timer_with_overlap() -> BuildTimer:
    """Two overlapping compiles + one link.

    Compile: a in [0,2], b in [1,3] -> Wall=3, CPU=4, parallelism=4/3 ≈ 1.3×
    Link:    app in [3,4]           -> Wall=1, CPU=1, parallelism=1.0×
    """
    timer = BuildTimer(enabled=True)
    phase = TimingEvent(name="build_execution", category="phase", start_s=0.0, end_s=4.0)
    phase.children.append(
        TimingEvent(name="a", category="compile", start_s=0.0, end_s=2.0, target="a.o", source="a.cpp")
    )
    phase.children.append(
        TimingEvent(name="b", category="compile", start_s=1.0, end_s=3.0, target="b.o", source="b.cpp")
    )
    phase.children.append(
        TimingEvent(name="app", category="link", start_s=3.0, end_s=4.0, target="app")
    )
    timer._root.children.append(phase)
    timer._root.end_s = 4.0
    return timer


def _populate_stub(timer: BuildTimer) -> StubNode:
    """Drive TimingReportApp._populate against a StubNode root."""
    from compiletools.timing_tui import TimingReportApp

    app = TimingReportApp.__new__(TimingReportApp)
    app._timer = timer
    app._sort_mode_idx = 0
    root = StubNode()
    app._populate(root, timer.phases, timer.total_elapsed_s)
    return root


# ----------------------------------------------------------- tests


def test_tree_inserts_category_aggregation_nodes():
    timer = _timer_with_overlap()
    labels = _flatten_labels(_populate_stub(timer))
    compile_rows = [line for line in labels if "Compile" in line and "CPU" in line]
    link_rows = [line for line in labels if "Link" in line and "CPU" in line]
    assert compile_rows, f"no Compile aggregation row in: {labels}"
    assert link_rows, f"no Link aggregation row in: {labels}"


def test_tree_aggregation_numbers_match_summary():
    """The numbers rendered in TUI category nodes must equal what
    aggregate_by_category returns -- same source of truth as the static
    summary table."""
    timer = _timer_with_overlap()
    rows = timer.aggregate_by_category(timer.phases[0])
    labels = _flatten_labels(_populate_stub(timer))

    for cat, wall, cpu, parallelism, _ in rows:
        cat_label = next(
            line for line in labels
            if cat.replace("_", " ").title() in line and "CPU" in line
        )
        assert f"{wall:.2f}s" in cat_label, f"Wall mismatch in {cat_label!r}"
        assert f"{cpu:.2f}s" in cat_label, f"CPU mismatch in {cat_label!r}"
        assert f"{parallelism:.1f}×" in cat_label, (
            f"parallelism mismatch in {cat_label!r}"
        )


def test_individual_rules_still_appear_under_categories():
    timer = _timer_with_overlap()
    labels = _flatten_labels(_populate_stub(timer))
    assert any("a.cpp" in line for line in labels)
    assert any("b.cpp" in line for line in labels)
    assert any("app" in line for line in labels)


def test_non_aggregating_phase_keeps_old_behaviour():
    """build_graph (and other non-aggregating phases) should not gain
    category nodes -- their children appear directly under the phase as
    before."""
    timer = BuildTimer(enabled=True)
    phase = TimingEvent(name="build_graph", category="phase", start_s=0.0, end_s=1.0)
    phase.children.append(
        TimingEvent(name="x", category="compile", start_s=0.0, end_s=1.0, target="x.o", source="x.cpp")
    )
    timer._root.children.append(phase)
    timer._root.end_s = 1.0

    labels = _flatten_labels(_populate_stub(timer))
    # No aggregation row for this phase
    assert not any("Compile" in line and "CPU" in line for line in labels)
    # Individual rule still present
    assert any("x.cpp" in line for line in labels)
