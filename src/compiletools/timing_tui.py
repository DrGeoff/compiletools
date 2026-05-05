"""Interactive ncdu-style TUI for build timing reports.

Requires textual (``pip install 'compiletools[tui]'``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

if TYPE_CHECKING:
    from compiletools.build_timer import BuildTimer, TimingEvent


# ------------------------------------------------------------------ helpers


def _bar(fraction: float, width: int = 25) -> str:
    """Render a proportional bar using Unicode block characters."""
    filled = int(fraction * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_time(seconds: float) -> str:
    """Format seconds to a human-readable string."""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m{s:04.1f}s"
    return f"{seconds:.2f}s"


def _label(event: TimingEvent, total: float) -> str:
    """Format a tree node label: time, percentage, bar, name."""
    pct = (event.elapsed_s / total * 100) if total > 0 else 0
    bar = _bar(event.elapsed_s / total if total > 0 else 0)
    name = event.source or event.target or event.name.replace("_", " ").title()
    if event.source:
        name = os.path.basename(event.source)
    return f"{_format_time(event.elapsed_s):>9s}  {pct:5.1f}%  {bar}  {name}"


# ----------------------------------------------------------- sort modes

SORT_MODES = ["time", "name"]


def _sort_key(mode: str):
    """Return a sort key function for the given mode."""
    if mode == "name":
        return lambda e: (e.source or e.target or e.name).lower()
    # Default: sort by time descending (negate for reverse)
    return lambda e: -e.elapsed_s


# ---------------------------------------------------------- TUI widgets


class TimingReportApp(App):
    """ncdu-style interactive timing report viewer."""

    TITLE = "ct-timing-report"
    CSS = """
    Tree {
        background: $surface;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("v", "show_timeline", "Timeline"),
        Binding("?", "help", "Help"),
    ]

    def __init__(self, timer: BuildTimer, path: str = "") -> None:
        super().__init__()
        self._timer = timer
        self._path = path
        self._sort_mode_idx = 0

    def compose(self) -> ComposeResult:
        total = self._timer.total_elapsed_s
        title = f"Build: {_format_time(total)} total"
        if self._timer.variant:
            title += f"  [{self._timer.variant}]"
        if self._timer.backend:
            title += f"  ({self._timer.backend})"

        yield Header()
        tree: Tree[TimingEvent] = Tree(title)
        tree.root.expand()
        self._populate(tree.root, self._timer.phases, total)
        yield tree
        yield Footer()

    def _populate(
        self,
        parent: TreeNode,
        events: list[TimingEvent],
        total: float,
    ) -> None:
        """Recursively populate tree nodes from timing events."""
        mode = SORT_MODES[self._sort_mode_idx]
        sorted_events = sorted(events, key=_sort_key(mode))

        for event in sorted_events:
            lbl = _label(event, total)
            if event.children:
                node = parent.add(lbl, expand=event.category == "phase")
                self._populate(node, event.children, total)
            else:
                parent.add_leaf(lbl)

    def action_cycle_sort(self) -> None:
        self._sort_mode_idx = (self._sort_mode_idx + 1) % len(SORT_MODES)
        mode = SORT_MODES[self._sort_mode_idx]
        self.notify(f"Sort: {mode}")
        # Rebuild the tree
        tree = self.query_one(Tree)
        tree.clear()
        total = self._timer.total_elapsed_s
        self._populate(tree.root, self._timer.phases, total)
        tree.root.expand()

    def action_show_timeline(self) -> None:
        from compiletools.timing_timeline import TimelineScreen

        self.push_screen(TimelineScreen(self._timer))

    def action_help(self) -> None:
        self.notify(
            "Navigation: arrows/j/k  Expand: Enter/Right  Collapse: Left\n"
            "Sort: [s]  Timeline: [v]  Quit: [q]",
            title="Help",
            timeout=8,
        )
