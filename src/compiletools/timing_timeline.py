"""Interactive Gantt-style timeline view for build timing reports.

Companion to ``timing_tui.py``'s tree view: instead of an aggregated
hierarchy, this renders every rule as a coloured box on a wall-clock
axis, with parallel rules packed into horizontal lanes so concurrent
build activity is visible at a glance.

Push from the tree view with ``v``; return with ``escape`` or ``t``.

Architecture
------------
* Pure helpers (``flatten_events``, ``pack_lanes``, ``pick_tick_interval``)
  are unit-tested independently of any UI.
* ``TimelineCanvas`` is a custom Textual widget that overrides
  ``render_line`` to draw one Strip per row using the rich
  Segment/Style API.  Reactive state (origin, zoom, selection, lane
  scroll) drives refreshes.
* ``TimelineScreen`` composes the canvas with a status panel.
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING, ClassVar

from rich.segment import Segment
from rich.style import Style
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from compiletools.build_timer import BuildTimer, TimingEvent

# ------------------------------------------------------------------- palette

# Tokyo-Night-inspired palette: pastel-on-dark, chosen so labels rendered
# in the foreground colour stay readable on top of the category-coloured
# background.  The same hex values work in both 256-colour and truecolour
# terminals (rich/textual downsample automatically).
DEFAULT_BG = "#1a1b26"
GRID_FG = "#3b4261"
AXIS_FG = "#7dcfff"
LANE_LABEL_FG = "#565f89"
PHASE_BG = "#414868"
PHASE_FG = "#c0caf5"
SELECT_FG = "#f7768e"  # rose, used for the selection border characters

CATEGORY_PALETTE: dict[str, tuple[str, str]] = {
    "compile": ("#7aa2f7", "#1a1b26"),
    "link": ("#ff9e64", "#1a1b26"),
    "static_library": ("#e0af68", "#1a1b26"),
    "shared_library": ("#bb9af7", "#1a1b26"),
    "test": ("#9ece6a", "#1a1b26"),
    "other": ("#565f89", "#c0caf5"),
}

LANE_LABEL_WIDTH = 4  # leftmost gutter "  0│"
HEADER_ROWS = 4  # axis labels, ticks, phase band, separator


# ----------------------------------------------------------- format helpers


def _format_time(seconds: float) -> str:
    """Long-form duration suitable for the status panel."""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m{s:04.1f}s"
    if seconds >= 1:
        return f"{seconds:.2f}s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds * 1e6:.0f}µs"


def _format_short(seconds: float) -> str:
    """Compact duration for axis tick labels (≤ 5 chars typical).

    When ``seconds`` is in the [1, 60) range with a non-integer fractional
    part, render with one decimal so consecutive sub-second ticks (e.g.
    1.0s, 1.2s, 1.4s) don't collapse to identical "1s" labels.
    """
    if seconds <= 0:
        return "0"
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m{int(s):02d}" if s >= 1 else f"{int(m)}m"
    if seconds >= 1:
        if abs(seconds - round(seconds)) < 1e-6:
            return f"{seconds:.0f}s"
        return f"{seconds:.1f}s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds * 1e6:.0f}us"


def _truncate(label: str, max_w: int) -> str:
    """Right-truncate with `…` if the label exceeds ``max_w`` cells."""
    if max_w <= 0:
        return ""
    if len(label) <= max_w:
        return label
    if max_w == 1:
        return "…"
    return label[: max_w - 1] + "…"


# ----------------------------------------------------------- pure logic


def flatten_events(phases: list[TimingEvent]) -> list[TimingEvent]:
    """Depth-first flat list of all non-phase events.

    Phase events are skipped because they're rendered separately as the
    header band; what we want here is leaf-level work (compiles, links,
    tests).  Rule events with their own children (rare but legal) are
    included along with their descendants.
    """
    out: list[TimingEvent] = []

    def walk(events: list[TimingEvent]) -> None:
        for e in events:
            if e.category == "phase":
                walk(e.children)
            else:
                out.append(e)
                if e.children:
                    walk(e.children)

    walk(phases)
    return out


def pack_lanes(events: list[TimingEvent]) -> list[int]:
    """Greedy first-fit lane assignment by start time.

    Returns a list of lane indices parallel to ``events``.  Two events
    share a lane iff one ends at-or-before the other starts (with a
    1ns epsilon to absorb float noise).  Lane 0 is the first to be
    filled, so the visual flows top-down by earliest start.
    """
    if not events:
        return []
    result = [0] * len(events)
    lanes_end: list[float] = []
    order = sorted(range(len(events)), key=lambda i: events[i].start_s)
    for i in order:
        e = events[i]
        end_s = e.end_s if e.end_s is not None else e.start_s
        chosen: int | None = None
        for j, le in enumerate(lanes_end):
            if le <= e.start_s + 1e-9:
                chosen = j
                break
        if chosen is None:
            chosen = len(lanes_end)
            lanes_end.append(end_s)
        else:
            lanes_end[chosen] = end_s
        result[i] = chosen
    return result


# Nice tick intervals: at most ~target_ticks visible across the viewport.
_NICE_INTERVALS = (
    0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0,
    600.0, 1800.0, 3600.0,
)


def pick_tick_interval(view_seconds: float, target_ticks: int = 8) -> float:
    """Smallest "nice" interval giving ≤ target_ticks ticks across view."""
    if view_seconds <= 0:
        return 1.0
    raw = view_seconds / max(target_ticks, 1)
    for n in _NICE_INTERVALS:
        if n >= raw:
            return n
    return _NICE_INTERVALS[-1]


def s_to_col(s: float, origin_s: float, seconds_per_col: float) -> float:
    """Convert an absolute time (s) to a fractional column index."""
    if seconds_per_col <= 0:
        return 0.0
    return (s - origin_s) / seconds_per_col


# ----------------------------------------------------------- Strip helpers


def _coalesce(cells: list[tuple[Style, str]]) -> list[Segment]:
    """Merge runs of identical Style into one Segment each.

    Strip rendering is dramatically faster when consecutive cells share
    one Segment, especially across long blank stretches between sparse
    events.  Compares by Style equality (not identity) since reactive
    rebuilds construct fresh Style objects each pass.
    """
    out: list[Segment] = []
    if not cells:
        return out
    cur_style, cur_ch = cells[0]
    buf = [cur_ch]
    for style, ch in cells[1:]:
        if style == cur_style:
            buf.append(ch)
        else:
            out.append(Segment("".join(buf), cur_style))
            cur_style = style
            buf = [ch]
    out.append(Segment("".join(buf), cur_style))
    return out


# ----------------------------------------------------------------- widgets


class StatusPanel(Static):
    """Bottom panel showing details of the currently selected event."""

    DEFAULT_CSS = """
    StatusPanel {
        height: 4;
        padding: 0 1;
        background: #16161e;
        color: #c0caf5;
        border: round #414868;
        border-title-color: #7dcfff;
    }
    """


class TimelineCanvas(Widget, can_focus=True):
    """Gantt-style canvas for a loaded BuildTimer.

    Layout (top-down):
      row 0   axis tick labels (e.g. ``0s   5s   10s``)
      row 1   axis ruler (``────┬─────┬─────┬───``)
      row 2   phase band (one-row header showing build phases)
      row 3   separator
      row 4+  lane rows; lane index N is on row HEADER_ROWS + N - lane_offset

    All rows share the same time-to-column mapping defined by
    ``origin_s`` (left edge) and ``seconds_per_col`` (zoom).  The leftmost
    LANE_LABEL_WIDTH columns are reserved for the lane number gutter.
    """

    DEFAULT_CSS = """
    TimelineCanvas {
        background: #1a1b26;
        color: #c0caf5;
    }
    TimelineCanvas:focus {
        border-left: thick #7dcfff;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("plus,equal,kp_plus", "zoom(0.5)", "Zoom in", show=True),
        Binding("minus,kp_minus", "zoom(2.0)", "Zoom out", show=True),
        Binding("0", "fit", "Fit", show=True),
        Binding("left,h", "pan(-0.25)", "Pan ←", show=False),
        Binding("right,l", "pan(0.25)", "Pan →", show=False),
        Binding("up,k", "select_dir(0,-1)", "Up", show=False),
        Binding("down,j", "select_dir(0,1)", "Down", show=False),
        Binding("n", "select_dir(1,0)", "Next", show=True),
        Binding("p", "select_dir(-1,0)", "Prev", show=True),
        Binding("home,g", "select_first", "Start", show=False),
        Binding("end,G", "select_last", "End", show=False),
    ]

    selected_idx: reactive[int] = reactive(-1)
    seconds_per_col: reactive[float] = reactive(1.0)
    origin_s: reactive[float] = reactive(0.0)
    lane_offset: reactive[int] = reactive(0)

    def __init__(
        self,
        timer: BuildTimer,
        status: StatusPanel | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.timer = timer
        self.status = status
        self.events = flatten_events(timer.phases)
        self.events.sort(key=lambda e: e.start_s)
        self.lanes = pack_lanes(self.events)
        self.num_lanes = (max(self.lanes) + 1) if self.lanes else 0
        # Use the actual first/last event timestamps as the absolute axis
        # bounds rather than the BuildTimer root: phase wrappers sometimes
        # extend slightly past the last rule (root.finish() runs after the
        # final rule), and using event bounds keeps the visible content
        # tight against the right edge.
        starts = [e.start_s for e in self.events]
        ends = [e.end_s for e in self.events if e.end_s is not None]
        phase_starts = [p.start_s for p in timer.phases]
        phase_ends = [p.end_s for p in timer.phases if p.end_s is not None]
        self.t_start = min(starts + phase_starts) if (starts or phase_starts) else 0.0
        self.t_end = max(ends + phase_ends) if (ends or phase_ends) else self.t_start + max(timer.total_elapsed_s, 1e-3)
        self.duration = max(self.t_end - self.t_start, 1e-6)
        self._needs_fit = True

    # -- mount

    def on_mount(self) -> None:
        if self.events:
            self.selected_idx = 0

    def on_resize(self) -> None:
        # Fit-to-width can only run once we have a real size: action_fit
        # divides duration by self._avail(), and the canvas is mounted
        # with size 0 before its first layout pass.  A one-shot fit on the
        # first non-zero resize keeps subsequent user-driven zooms intact.
        if self._needs_fit and self.size.width > LANE_LABEL_WIDTH:
            self.action_fit()
            self._needs_fit = False

    # -- size hint

    def get_content_height(self, container, viewport, width):
        del container, viewport, width
        return HEADER_ROWS + max(self.num_lanes, 1)

    # -- viewport actions

    def _avail(self) -> int:
        return max(1, self.size.width - LANE_LABEL_WIDTH)

    def action_fit(self) -> None:
        avail = self._avail()
        self.seconds_per_col = self.duration / avail
        self.origin_s = self.t_start
        self.lane_offset = 0
        self.refresh()

    def action_zoom(self, factor: float) -> None:
        """Zoom by ``factor`` (``0.5``=in, ``2.0``=out) keeping focus stable.

        Anchors the zoom to the selected event's left edge if there is a
        selection, otherwise to the centre of the viewport — matches the
        Perfetto/Chrome trace UX.  Clamped between ``fit`` and ~1000x in.
        """
        avail = self._avail()
        if 0 <= self.selected_idx < len(self.events):
            focus_s = self.events[self.selected_idx].start_s
        else:
            focus_s = self.origin_s + avail * self.seconds_per_col / 2

        old_spc = self.seconds_per_col
        focus_col = (focus_s - self.origin_s) / old_spc if old_spc > 0 else 0

        max_spc = self.duration / avail  # fully zoomed out (= fit)
        min_spc = max_spc / 1000.0  # 1000x zoom in cap
        new_spc = max(min_spc, min(self.seconds_per_col * factor, max_spc))

        # Re-anchor so focus_s stays at the same on-screen column.
        self.origin_s = focus_s - focus_col * new_spc
        self.seconds_per_col = new_spc
        self._clamp_origin()
        self.refresh()

    def action_pan(self, fraction: float) -> None:
        avail = self._avail()
        self.origin_s = self.origin_s + fraction * avail * self.seconds_per_col
        self._clamp_origin()
        self.refresh()

    def _clamp_origin(self) -> None:
        """Prevent panning the trace entirely off-screen.

        Allows up to half a viewport of empty space at either end so the
        first/last events can be centred, but no further.
        """
        avail = self._avail()
        view_s = avail * self.seconds_per_col
        min_origin = self.t_start - view_s / 2
        max_origin = self.t_end - view_s / 2
        self.origin_s = max(min_origin, min(self.origin_s, max_origin))

    # -- selection actions

    def action_select_dir(self, dx: int, dy: int) -> None:
        """Move selection by ``dx`` along time order, ``dy`` across lanes."""
        if not self.events:
            return
        if self.selected_idx < 0:
            self.selected_idx = 0
            self._ensure_visible()
            return
        if dx != 0:
            self.selected_idx = max(0, min(len(self.events) - 1, self.selected_idx + dx))
        elif dy != 0:
            cur = self.events[self.selected_idx]
            target_lane = self.lanes[self.selected_idx] + dy
            if 0 <= target_lane < self.num_lanes:
                # Pick the event in target_lane whose start is closest
                # to the current selection's start time.
                best_idx = -1
                best_d = float("inf")
                for i in range(len(self.events)):
                    if self.lanes[i] != target_lane:
                        continue
                    d = abs(self.events[i].start_s - cur.start_s)
                    if d < best_d:
                        best_d = d
                        best_idx = i
                if best_idx >= 0:
                    self.selected_idx = best_idx
        self._ensure_visible()

    def action_select_first(self) -> None:
        if self.events:
            self.selected_idx = 0
            self._ensure_visible()

    def action_select_last(self) -> None:
        if self.events:
            self.selected_idx = len(self.events) - 1
            self._ensure_visible()

    def _ensure_visible(self) -> None:
        if not (0 <= self.selected_idx < len(self.events)):
            return
        e = self.events[self.selected_idx]
        avail = self._avail()
        margin = 4
        col_start = s_to_col(e.start_s, self.origin_s, self.seconds_per_col)
        end_s = e.end_s if e.end_s is not None else e.start_s
        col_end = s_to_col(end_s, self.origin_s, self.seconds_per_col)
        if col_start < margin:
            self.origin_s += (col_start - margin) * self.seconds_per_col
        elif col_end > avail - margin:
            self.origin_s += (col_end - (avail - margin)) * self.seconds_per_col
        self._clamp_origin()
        # Lane scroll
        lane = self.lanes[self.selected_idx]
        view_lanes = max(1, self.size.height - HEADER_ROWS)
        if lane < self.lane_offset:
            self.lane_offset = lane
        elif lane >= self.lane_offset + view_lanes:
            self.lane_offset = lane - view_lanes + 1
        self.refresh()

    # -- mouse

    def on_click(self, event) -> None:
        x, y = event.x, event.y
        if y < HEADER_ROWS:
            return
        if x < LANE_LABEL_WIDTH:
            return
        lane = self.lane_offset + (y - HEADER_ROWS)
        if not (0 <= lane < self.num_lanes):
            return
        col = x - LANE_LABEL_WIDTH
        t = self.origin_s + col * self.seconds_per_col
        # Hit-test: pick the event in this lane that contains t, or
        # nearest by start time if none does (so clicks on gutters still
        # select the closest box).
        best = -1
        best_d = float("inf")
        for i, e in enumerate(self.events):
            if self.lanes[i] != lane:
                continue
            end_s = e.end_s if e.end_s is not None else e.start_s
            if e.start_s <= t <= end_s:
                best = i
                break
            d = min(abs(e.start_s - t), abs(end_s - t))
            if d < best_d:
                best_d = d
                best = i
        if best >= 0:
            self.selected_idx = best

    # -- reactive watchers

    def watch_selected_idx(self, _: int, new: int) -> None:
        if self.status is None:
            return
        if 0 <= new < len(self.events):
            self.status.update(self._format_status(self.events[new]))
        else:
            self.status.update("")

    def watch_seconds_per_col(self) -> None:
        self.refresh()

    def watch_origin_s(self) -> None:
        self.refresh()

    def watch_lane_offset(self) -> None:
        self.refresh()

    def _format_status(self, e: TimingEvent) -> str:
        cat = e.category
        target = e.target or e.name
        elapsed = _format_time(e.elapsed_s)
        start_rel = e.start_s - self.t_start
        end_s = e.end_s if e.end_s is not None else e.start_s
        end_rel = end_s - self.t_start
        bg, _ = CATEGORY_PALETTE.get(cat, CATEGORY_PALETTE["other"])
        line1 = (
            f"[bold #c0caf5]{target}[/]    "
            f"[{bg}]█ {cat.replace('_', ' ')}[/]    "
            f"[bold #e0af68]{elapsed}[/]    "
            f"[#7dcfff]start[/] {_format_short(start_rel)}    "
            f"[#7dcfff]end[/] {_format_short(end_rel)}    "
            f"[#565f89]lane {self.lanes[self.selected_idx]}[/]"
        )
        if e.source:
            line2 = f"[#565f89]{e.source}[/]"
        else:
            line2 = ""
        return f"{line1}\n{line2}"

    # -- rendering

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if width <= 0:
            return Strip.blank(0)
        if y == 0:
            return self._render_axis_labels()
        if y == 1:
            return self._render_axis_ruler()
        if y == 2:
            return self._render_phase_band()
        if y == 3:
            return self._render_separator(width)
        lane_idx = self.lane_offset + (y - HEADER_ROWS)
        if 0 <= lane_idx < self.num_lanes:
            return self._render_lane(lane_idx)
        return Strip([Segment(" " * width, Style(bgcolor=DEFAULT_BG))])

    def _tick_positions(self, avail: int) -> list[tuple[int, float]]:
        view_s = avail * self.seconds_per_col
        interval = pick_tick_interval(view_s)
        # First tick at-or-after origin
        first = math.ceil(self.origin_s / interval) * interval
        ticks: list[tuple[int, float]] = []
        t = first
        end = self.origin_s + view_s
        # Bound the loop: avoid infinite loop if interval underflows
        max_iter = avail + 4
        i = 0
        while t <= end and i < max_iter:
            col = round((t - self.origin_s) / self.seconds_per_col)
            if 0 <= col < avail:
                ticks.append((col, t - self.t_start))
            t += interval
            i += 1
        return ticks

    def _render_axis_labels(self) -> Strip:
        avail = self._avail()
        ticks = self._tick_positions(avail)
        line = [" "] * avail
        for col, t in ticks:
            label = _format_short(t)
            for k, ch in enumerate(label):
                x = col + k
                if 0 <= x < avail:
                    line[x] = ch
        gutter = Segment(" " * LANE_LABEL_WIDTH, Style(bgcolor=DEFAULT_BG))
        body = Segment("".join(line), Style(color=AXIS_FG, bgcolor=DEFAULT_BG))
        return Strip([gutter, body])

    def _render_axis_ruler(self) -> Strip:
        avail = self._avail()
        ticks = {col for col, _ in self._tick_positions(avail)}
        chars = ["─"] * avail  # ─
        for col in ticks:
            if 0 <= col < avail:
                chars[col] = "┬"  # ┬
        gutter = Segment(" " * LANE_LABEL_WIDTH, Style(bgcolor=DEFAULT_BG))
        body = Segment("".join(chars), Style(color=AXIS_FG, bgcolor=DEFAULT_BG))
        return Strip([gutter, body])

    def _render_phase_band(self) -> Strip:
        avail = self._avail()
        cells: list[tuple[Style, str]] = [
            (Style(bgcolor=DEFAULT_BG), " ") for _ in range(avail)
        ]
        for phase in self.timer.phases:
            if phase.end_s is None:
                continue
            cs = s_to_col(phase.start_s, self.origin_s, self.seconds_per_col)
            ce = s_to_col(phase.end_s, self.origin_s, self.seconds_per_col)
            cs_i = max(0, round(cs))
            ce_i = min(avail, max(cs_i + 1, round(ce)))
            if cs_i >= avail or ce_i <= 0:
                continue
            inner_w = ce_i - cs_i
            label = phase.name.replace("_", " ")
            content_w = max(0, inner_w - 2)
            shown = _truncate(label, content_w)
            disp = (" " + shown + " ").center(inner_w)[:inner_w] if inner_w >= 4 else " " * inner_w
            style = Style(bgcolor=PHASE_BG, color=PHASE_FG, bold=True)
            for x in range(cs_i, ce_i):
                rel = x - cs_i
                ch = disp[rel] if rel < len(disp) else " "
                cells[x] = (style, ch)
        gutter = Segment(" " * LANE_LABEL_WIDTH, Style(bgcolor=DEFAULT_BG))
        return Strip([gutter] + _coalesce(cells))

    def _render_separator(self, width: int) -> Strip:
        return Strip(
            [Segment("─" * width, Style(color=GRID_FG, bgcolor=DEFAULT_BG))]
        )

    def _render_lane(self, lane_idx: int) -> Strip:
        avail = self._avail()
        cells: list[tuple[Style, str]] = [
            (Style(bgcolor=DEFAULT_BG), " ") for _ in range(avail)
        ]
        for i, e in enumerate(self.events):
            if self.lanes[i] != lane_idx:
                continue
            cs = s_to_col(e.start_s, self.origin_s, self.seconds_per_col)
            end_s = e.end_s if e.end_s is not None else e.start_s
            ce = s_to_col(end_s, self.origin_s, self.seconds_per_col)
            cs_i_raw = round(cs)
            ce_i_raw = round(ce)
            # Clip to viewport but remember whether either edge was clipped
            # so we can draw arrow indicators.
            clipped_left = cs_i_raw < 0
            clipped_right = ce_i_raw > avail
            cs_i = max(0, cs_i_raw)
            ce_i = min(avail, max(cs_i_raw + 1, ce_i_raw))
            if cs_i >= avail or ce_i <= 0:
                continue
            inner_w = ce_i - cs_i
            bg, fg = CATEGORY_PALETTE.get(e.category, CATEGORY_PALETTE["other"])
            is_sel = i == self.selected_idx
            style = Style(
                bgcolor=bg,
                color=fg,
                bold=is_sel,
                reverse=False,
            )
            label = (os.path.basename(e.source) if e.source else "") or e.target or e.name
            label = os.path.basename(label) if "/" in label else label
            content_w = max(0, inner_w - 2)
            shown = _truncate(label, content_w)
            disp = (" " + shown).ljust(inner_w)[:inner_w]
            for x in range(cs_i, ce_i):
                rel = x - cs_i
                ch = disp[rel] if rel < len(disp) else " "
                cells[x] = (style, ch)
            # Edge indicators for clipped events: a chevron painted in
            # the rose accent so the user notices content extends past
            # the viewport in that direction.
            if clipped_left and cs_i < avail:
                cells[cs_i] = (Style(bgcolor=bg, color=SELECT_FG, bold=True), "‹")  # noqa: RUF001
            if clipped_right and ce_i - 1 >= 0:
                cells[ce_i - 1] = (Style(bgcolor=bg, color=SELECT_FG, bold=True), "›")  # noqa: RUF001
            # Selection underline: paint the colour-cells on the next
            # render row by setting underline style.  Single-row widget
            # so we use a corner-bracket suffix instead.
            if is_sel and inner_w >= 2:
                cells[cs_i] = (Style(bgcolor=bg, color=SELECT_FG, bold=True), "┃")
                cells[ce_i - 1] = (Style(bgcolor=bg, color=SELECT_FG, bold=True), "┃")
        # Lane gutter label
        lane_text = f"{lane_idx:>2d}│ "[:LANE_LABEL_WIDTH]
        lane_text = lane_text.ljust(LANE_LABEL_WIDTH)
        gutter = Segment(lane_text, Style(color=LANE_LABEL_FG, bgcolor=DEFAULT_BG))
        return Strip([gutter] + _coalesce(cells))


# ---------------------------------------------------------------- screen


class TimelineScreen(Screen):
    """Modal screen presenting the Gantt timeline.

    Composes a TimelineCanvas with a status panel; ``escape`` or ``t``
    pops back to the tree view.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,t", "app.pop_screen", "Tree", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        Binding("question_mark", "help", "Help", show=True),
    ]

    DEFAULT_CSS = """
    TimelineScreen {
        background: #1a1b26;
    }
    #ct-canvas {
        height: 1fr;
        background: #1a1b26;
    }
    #ct-status {
        height: 4;
        margin: 0 1;
    }
    """

    def __init__(self, timer: BuildTimer) -> None:
        super().__init__()
        self.timer = timer

    def compose(self) -> ComposeResult:
        total = self.timer.total_elapsed_s
        title = f"Timeline — {_format_time(total)} total"
        if self.timer.variant:
            title += f"  [{self.timer.variant}]"
        if self.timer.backend:
            title += f"  ({self.timer.backend})"
        self.title = title
        self.sub_title = "Gantt view"

        yield Header(show_clock=False)
        status = StatusPanel("", id="ct-status")
        status.border_title = "Selected"
        canvas = TimelineCanvas(self.timer, status=status, id="ct-canvas")
        yield canvas
        yield status
        yield Footer()

    def on_mount(self) -> None:
        canvas = self.query_one(TimelineCanvas)
        canvas.focus()

    def action_help(self) -> None:
        self.app.notify(
            "Pan: ←/→ or h/l   Zoom: +/-   Fit: 0   "
            "Move sel: n/p (time)  ↑/↓ (lane)   "
            "Home/End: first/last   Tree view: t / esc",
            title="Timeline help",
            timeout=10,
        )
