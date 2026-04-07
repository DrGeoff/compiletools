"""Build timing data collection, serialization, and reporting.

BuildTimer collects hierarchical timing events during a build and
produces structured JSON output, Chrome Trace exports, and Rich
summary tables.  When disabled (the default), all methods are no-ops
with negligible overhead.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from compiletools.build_graph import BuildGraph


@dataclass
class TimingEvent:
    """A single timed span in the build."""

    name: str
    category: str  # "phase", "compile", "link", "static_library", ...
    start_s: float  # time.monotonic() value
    end_s: float | None = None  # None while running
    target: str = ""
    source: str = ""
    children: list[TimingEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        if self.end_s is None:
            return time.monotonic() - self.start_s
        return self.end_s - self.start_s

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "elapsed_s": round(self.elapsed_s, 6),
        }
        if self.category != "phase":
            d["rule_type"] = self.category
        if self.target:
            d["target"] = self.target
        if self.source:
            d["source"] = self.source
        if self.metadata:
            d["metadata"] = self.metadata
        # Include start_s/end_s relative to parent for rule events
        if self.category != "phase" and self.start_s is not None and self.end_s is not None:
            d["start_s"] = round(self.start_s, 6)
            d["end_s"] = round(self.end_s, 6)
        if self.children:
            if self.category == "phase":
                d["rules"] = [c.to_dict() for c in self.children]
            else:
                d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TimingEvent:
        category = d.get("rule_type", "phase")
        children_key = "rules" if category == "phase" else "children"
        children_data = d.get(children_key, [])
        return cls(
            name=d["name"],
            category=category,
            start_s=d.get("start_s", 0.0),
            end_s=d.get("end_s", d.get("elapsed_s", 0.0)),
            target=d.get("target", ""),
            source=d.get("source", ""),
            children=[TimingEvent.from_dict(c) for c in children_data],
            metadata=d.get("metadata", {}),
        )


# -- JSON format version --
_FORMAT_VERSION = 1


class BuildTimer:
    """Collects hierarchical timing events during a build.

    When ``enabled=False`` (the default), all methods are no-ops.
    """

    def __init__(
        self,
        enabled: bool = False,
        variant: str = "",
        backend: str = "",
    ) -> None:
        self.enabled = enabled
        self.variant = variant
        self.backend = backend
        self._lock = threading.Lock()

        # Root event spans the entire build
        self._root = TimingEvent(name="total", category="phase", start_s=time.monotonic())
        self._phase_stack: list[TimingEvent] = [self._root]

    # ---------------------------------------------------------------- public API

    @property
    def total_elapsed_s(self) -> float:
        """Total build time in seconds."""
        return self._root.elapsed_s

    @property
    def phases(self) -> list[TimingEvent]:
        """Top-level phase events."""
        return self._root.children

    # ------------------------------------------------------------------ phases

    @contextlib.contextmanager
    def phase(self, name: str):
        """Context manager that records a timed build phase."""
        if not self.enabled:
            yield
            return
        event = TimingEvent(name=name, category="phase", start_s=time.monotonic())
        self._phase_stack[-1].children.append(event)
        self._phase_stack.append(event)
        try:
            yield
        finally:
            event.end_s = time.monotonic()
            self._phase_stack.pop()

    # --------------------------------------------------------- per-rule events

    def record_rule(
        self,
        rule_type: str,
        target: str,
        source: str,
        elapsed_s: float,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> None:
        """Record a completed compile/link/library rule.

        Thread-safe for use from Shake backend's thread pool.
        """
        if not self.enabled:
            return
        if start_s is None:
            start_s = 0.0
        if end_s is None:
            end_s = start_s + elapsed_s
        event = TimingEvent(
            name=os.path.basename(source) if source else os.path.basename(target),
            category=rule_type,
            start_s=start_s,
            end_s=end_s,
            target=target,
            source=source,
        )
        with self._lock:
            if self._phase_stack:
                self._phase_stack[-1].children.append(event)

    # ------------------------------------------------- ninja log parsing

    @staticmethod
    def _build_graph_lookups(
        graph: BuildGraph | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Build source and rule-type lookup dicts from a BuildGraph."""
        source_for_output: dict[str, str] = {}
        type_for_output: dict[str, str] = {}
        if graph is not None:
            for rule in graph.rules:
                type_for_output[rule.output] = rule.rule_type
                if rule.inputs:
                    source_for_output[rule.output] = rule.inputs[0]
        return source_for_output, type_for_output

    def record_rules_from_ninja_log(
        self,
        log_path: str,
        offset: int = 0,
        graph: BuildGraph | None = None,
    ) -> None:
        """Parse .ninja_log entries appended after *offset* bytes.

        The .ninja_log format (v5) is tab-separated::

            # ninja log v5
            <start_ms>\\t<end_ms>\\t<mtime_ms>\\t<output>\\t<hash>

        When the same output appears multiple times, only the last
        entry is kept (ninja appends without truncating).
        """
        if not self.enabled:
            return
        if not os.path.exists(log_path):
            return

        source_for_output, type_for_output = self._build_graph_lookups(graph)

        entries: dict[str, tuple[int, int, str, str]] = {}
        with open(log_path, encoding="utf-8", errors="replace") as f:
            if offset > 0:
                f.seek(offset)
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                try:
                    start_ms = int(parts[0])
                    end_ms = int(parts[1])
                except ValueError:
                    continue
                output = parts[3]
                # Keep last entry for each output
                source = source_for_output.get(output, "")
                rule_type = type_for_output.get(output, _classify_output(output))
                entries[output] = (start_ms, end_ms, source, rule_type)

        for output, (start_ms, end_ms, source, rule_type) in entries.items():
            elapsed_s = (end_ms - start_ms) / 1000.0
            self.record_rule(
                rule_type=rule_type,
                target=output,
                source=source,
                elapsed_s=elapsed_s,
                start_s=start_ms / 1000.0,
                end_s=end_ms / 1000.0,
            )

    # ------------------------------------------------- make timing parsing

    def record_rules_from_make_timing(
        self,
        log_path: str,
        graph: BuildGraph | None = None,
    ) -> None:
        """Parse .ct-make-timing.jsonl written by timing-instrumented recipes.

        Each line is a JSON object::

            {"target": "obj/foo.o", "start_ns": 1234567890, "end_ns": 1234567999}
        """
        if not self.enabled:
            return
        if not os.path.exists(log_path):
            return

        source_for_output, type_for_output = self._build_graph_lookups(graph)

        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                target = entry.get("target", "")
                start_ns = entry.get("start_ns", 0)
                end_ns = entry.get("end_ns", 0)
                elapsed_s = (end_ns - start_ns) / 1_000_000_000.0
                source = source_for_output.get(target, "")
                rule_type = type_for_output.get(target, _classify_output(target))
                self.record_rule(
                    rule_type=rule_type,
                    target=target,
                    source=source,
                    elapsed_s=elapsed_s,
                    start_s=start_ns / 1_000_000_000.0,
                    end_s=end_ns / 1_000_000_000.0,
                )

    # --------------------------------------------------------- serialization

    def finish(self) -> None:
        """Mark the root event as finished."""
        if self.enabled and self._root.end_s is None:
            self._root.end_s = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        self.finish()
        return {
            "version": _FORMAT_VERSION,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_elapsed_s": round(self._root.elapsed_s, 6),
            "variant": self.variant,
            "backend": self.backend,
            "phases": [child.to_dict() for child in self._root.children],
        }

    def to_json(self, path: str) -> None:
        """Write timing data to a JSON file."""
        from compiletools.filesystem_utils import atomic_output_file

        data = self.to_dict()
        with atomic_output_file(path, mode="w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BuildTimer:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        timer = cls(enabled=True, variant=data.get("variant", ""), backend=data.get("backend", ""))
        timer._root = TimingEvent(
            name="total",
            category="phase",
            start_s=0.0,
            end_s=data.get("total_elapsed_s", 0.0),
            children=[TimingEvent.from_dict(p) for p in data.get("phases", [])],
        )
        timer._phase_stack = [timer._root]
        return timer

    @classmethod
    def from_json(cls, path: str) -> BuildTimer:
        """Load timing data from a JSON file."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    # ------------------------------------------------- chrome trace export

    def to_chrome_trace(self) -> list[dict[str, Any]]:
        """Convert to Chrome Trace Event Format (Perfetto-compatible).

        Returns a list of trace events suitable for wrapping in
        ``{"traceEvents": [...]}`` and viewing in chrome://tracing
        or https://ui.perfetto.dev/.
        """
        self.finish()
        events: list[dict[str, Any]] = []
        self._chrome_trace_walk(self._root, events, tid=0, pid=1)
        return events

    def _chrome_trace_walk(
        self,
        event: TimingEvent,
        events: list[dict[str, Any]],
        tid: int,
        pid: int,
    ) -> None:
        ts_us = event.start_s * 1_000_000
        dur_us = event.elapsed_s * 1_000_000
        trace_event: dict[str, Any] = {
            "name": event.name,
            "cat": event.category,
            "ph": "X",  # complete event
            "ts": ts_us,
            "dur": dur_us,
            "pid": pid,
            "tid": tid,
        }
        if event.target:
            trace_event.setdefault("args", {})["target"] = event.target
        if event.source:
            trace_event.setdefault("args", {})["source"] = event.source
        events.append(trace_event)

        # Give each child rule its own "thread" for parallel visualization
        for i, child in enumerate(event.children):
            child_tid = tid if child.category == "phase" else i + 1
            self._chrome_trace_walk(child, events, tid=child_tid, pid=pid)

    # --------------------------------------------------------- summary table

    def summary_table(self):
        """Return a Rich Table summarizing the build timing.

        Returns None if rich is not available.
        """
        self.finish()
        try:
            from rich.table import Table
        except ImportError:
            return None

        table = Table(title=f"Build Timing Report ({self._root.elapsed_s:.1f}s total)")
        table.add_column("Phase", style="cyan", no_wrap=True)
        table.add_column("Time (s)", justify="right", style="magenta")
        table.add_column("%", justify="right")

        total = self._root.elapsed_s or 1.0

        for phase in self._root.children:
            pct = (phase.elapsed_s / total) * 100
            table.add_row(
                phase.name.replace("_", " ").title(),
                f"{phase.elapsed_s:.2f}",
                f"{pct:.1f}%",
            )
            # Sub-aggregate rules by type within build_execution
            if phase.children and phase.name in ("build_execution", "test_execution"):
                type_totals: dict[str, float] = {}
                for child in phase.children:
                    cat = child.category
                    type_totals[cat] = type_totals.get(cat, 0.0) + child.elapsed_s
                for cat, cat_time in sorted(type_totals.items(), key=lambda x: -x[1]):
                    cat_pct = (cat_time / total) * 100
                    table.add_row(
                        f"  {cat.replace('_', ' ').title()}",
                        f"{cat_time:.2f}",
                        f"{cat_pct:.1f}%",
                    )

        return table

    def print_summary(self) -> None:
        """Print the summary table and top slowest compilations."""
        table = self.summary_table()
        if table is None:
            return
        try:
            from rich.console import Console

            console = Console(stderr=True)
            console.print(table)

            # Print slowest compilations
            all_rules = self._collect_rules()
            compiles = sorted(
                [r for r in all_rules if r.category == "compile"],
                key=lambda r: -r.elapsed_s,
            )
            if compiles:
                console.print("\n[bold]Slowest compilations:[/bold]")
                for rule in compiles[:10]:
                    label = rule.source or rule.target
                    console.print(f"  {rule.elapsed_s:6.1f}s  {label}")
        except ImportError:
            pass

    def _collect_rules(self) -> list[TimingEvent]:
        """Collect all non-phase events recursively."""
        result: list[TimingEvent] = []
        self._collect_rules_walk(self._root, result)
        return result

    def _collect_rules_walk(self, event: TimingEvent, result: list[TimingEvent]) -> None:
        for child in event.children:
            if child.category != "phase":
                result.append(child)
            self._collect_rules_walk(child, result)


def get_timer(context) -> BuildTimer | None:
    """Safely retrieve an enabled BuildTimer from a context object.

    Returns None if the context has no timer, the timer is not a
    BuildTimer instance (e.g. a MagicMock in tests), or the timer
    is disabled.
    """
    timer = getattr(context, "timer", None)
    if isinstance(timer, BuildTimer) and timer.enabled:
        return timer
    return None


def _classify_output(output: str) -> str:
    """Guess rule type from output file extension."""
    if output.endswith((".o", ".obj")):
        return "compile"
    elif output.endswith(".a"):
        return "static_library"
    elif output.endswith((".so", ".dylib", ".dll")):
        return "shared_library"
    else:
        return "link"
