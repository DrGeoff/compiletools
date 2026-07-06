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
from typing import TYPE_CHECKING, Any, ClassVar

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
        # Persist start_s/end_s for every event (including phases) so the
        # chrome-trace exporter has one shared monotonic-clock origin to
        # rebase against.  All ingest paths feed monotonic timestamps
        # (in-Python via time.monotonic(); make/slurm wall-clock values
        # are converted at ingest using the offset captured in
        # BuildTimer.__init__), so these values are directly comparable.
        if self.start_s is not None and self.end_s is not None:
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


# ---------------------------------------------------------------- rule outcomes log
#
# Per-build, per-rule outcomes log written by build backends (trace_backend
# and ct-lock-helper for ninja/make backends) when ``CT_RULE_OUTCOMES_LOG``
# is set in the environment.  One line per executed rule, tab-separated:
#
#     <target>\t<cas_kind>\t<cas_hit:0|1>\t<bytes_reused>\n
#
# Lines stay well below ``PIPE_BUF`` (4096 on Linux) so concurrent
# ``O_APPEND`` writes from parallel build workers do not interleave (POSIX
# atomicity guarantee for ``write()`` with ``O_APPEND`` when the payload
# fits in one ``PIPE_BUF``).  The exporter ingests the file once after the
# build to populate ``TimingEvent.metadata`` with ``cas.*`` keys keyed by
# target; ``_emit_event`` then lifts them onto span attributes.
#
# Best-effort: missing/empty/malformed lines leave the affected rules
# without ``cas.*`` metadata rather than failing the build.


_RULE_OUTCOMES_LOG_ENV = "CT_RULE_OUTCOMES_LOG"


def _cas_kind_for_rule_type(rule_type: str) -> str:
    """Map a BuildRule rule_type to a CAS kind tag.

    Returns one of ``obj`` / ``exe`` / ``lib`` / ``pch`` / ``pcm``, or an
    empty string when the rule type has no CAS bucket (e.g. ``mkdir``,
    ``phony``, ``symlink``, ``test``).  Empty kind is still written to the
    outcomes log so the line shape is uniform; the exporter filters them
    out at ingest by checking truthiness.
    """
    if rule_type == "compile":
        return "obj"
    if rule_type in ("link", "executable"):
        return "exe"
    if rule_type in ("static_library", "shared_library"):
        return "lib"
    if rule_type == "header_unit":
        return "pcm"
    # If a RuleType.PRECOMPILED_HEADER rule type is ever introduced, branch
    # it here to return "pch" — PCH isn't a distinct rule type today.
    return ""


def append_rule_outcome(
    target: str,
    cas_kind: str,
    cas_hit: bool,
    bytes_reused: int,
    *,
    path: str | None = None,
) -> None:
    """Atomically append one rule outcome line to the outcomes log.

    Uses ``os.open(O_APPEND | O_CREAT | O_WRONLY)`` + a single ``os.write``
    call to leverage POSIX's guarantee that an ``O_APPEND`` write of fewer
    than ``PIPE_BUF`` bytes is atomic across concurrent writers.  Buffered
    ``open()`` would defeat that because Python's write buffer may flush in
    multiple syscalls.

    No-op when ``path`` is ``None`` (resolved from ``CT_RULE_OUTCOMES_LOG``
    if not passed) or when the line would exceed ``PIPE_BUF``.  Best-effort
    by design: a failure here must not fail a build rule.

    Writer coverage today:
      * ``ct-lock-helper`` ``cmd_compile`` / ``cmd_link`` — used by the
        ninja/make backends for the lockdir/fcntl/cifs strategies, plus
        the flock strategy when the native ``flock`` binary is missing.
      * The trace_backend path does NOT call this (Issue #2): it records
        ``metadata`` in-process via ``BuildTimer.record_rule``, which is
        the source of truth for that backend.
      * The native-flock fast-path in
        ``build_backend.wrap_compile_with_lock`` (local filesystems with
        util-linux ``flock`` available) bypasses ``ct-lock-helper`` for
        speed and so does not write outcomes — ``cas.*`` keys are
        absent for those rules.  Documented in ``README.ct-otel.rst``
        under "CAS-attribute coverage scope".
    """
    if path is None:
        path = os.environ.get(_RULE_OUTCOMES_LOG_ENV)
    if not path:
        return
    # Sanitise: tabs/newlines in the target would corrupt the format. Drop
    # such lines rather than encode them — targets with embedded
    # whitespace are pathological and exceedingly rare.
    if "\t" in target or "\n" in target:
        return
    line = f"{target}\t{cas_kind}\t{1 if cas_hit else 0}\t{int(bytes_reused)}\n"
    data = line.encode("utf-8")
    # PIPE_BUF on Linux is 4096; oversize lines lose atomicity, so drop
    # them rather than risk interleaving.
    if len(data) >= 4096:
        return
    try:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    except OSError:
        return
    try:
        os.write(fd, data)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_rule_outcomes(path: str | None) -> dict[str, dict[str, Any]]:
    """Parse a rule-outcomes log into ``{target: {cas.*: ...}}``.

    Returns an empty dict if ``path`` is None/empty/missing.  Malformed
    lines are silently skipped; the rest of the file is still ingested.
    When the same target appears multiple times (a build retried a rule),
    the last entry wins — matches ninja's last-entry-wins semantics for
    its own log.
    """
    if not path or not os.path.exists(path):
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) != 4:
                    continue
                target, cas_kind, hit_str, bytes_str = parts
                try:
                    cas_hit = bool(int(hit_str))
                    bytes_reused = int(bytes_str)
                except ValueError:
                    continue
                md: dict[str, Any] = {
                    "cas.hit": cas_hit,
                    "cas.bytes_reused": bytes_reused,
                }
                if cas_kind:
                    md["cas.kind"] = cas_kind
                out[target] = md
    except OSError:
        return {}
    return out


def _union_span(events: list[TimingEvent]) -> float:
    """Total wall-clock time during which any of ``events`` was running.

    Merges overlapping [start_s, end_s) intervals.  Events still in
    flight (end_s is None) are skipped.
    """
    intervals = sorted((e.start_s, e.end_s) for e in events if e.end_s is not None)
    if not intervals:
        return 0.0
    total = 0.0
    cur_start, cur_end = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_end:
            cur_end = max(cur_end, e)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = s, e
    total += cur_end - cur_start
    return total


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
        # Set True by from_dict/from_json to forbid further phase/record
        # mutations on a loaded snapshot.
        self._loaded = False

        # Capture the wall-to-monotonic offset once at startup so ingest
        # paths that record wall-clock timestamps (make recipe wrappers
        # via bash $EPOCHREALTIME, Slurm sacct ISO timestamps) can
        # convert their values into the same monotonic clock domain that
        # the in-Python record_rule callers use.  A single calibration is
        # accurate to within a few microseconds for the duration of a
        # build, which is well below per-rule timing noise.  Falls back
        # to 0 if the wall clock has been stepped between the two reads
        # (extremely unlikely within a few µs window).
        self._wall_to_monotonic_offset = time.monotonic() - time.time()

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
        if self._loaded:
            raise RuntimeError(
                "BuildTimer was loaded from a serialized snapshot and is read-only; cannot record new phases on it."
            )
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
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a completed compile/link/library rule.

        Thread-safe for use from Shake backend's thread pool.

        ``metadata`` is an opt-in producer-side dict that round-trips
        through ``to_dict`` / ``from_dict`` and is lifted onto span
        attributes by the OTel exporter (``otel/traces.py:_emit_event``).
        Used today for ``cas.hit`` / ``cas.kind`` / ``cas.bytes_reused``;
        the lift is generic so new keys flow through without exporter
        changes. A shallow copy is taken so callers can reuse a single
        dict across rules without worrying about cross-contamination.
        """
        if not self.enabled:
            return
        if self._loaded:
            raise RuntimeError(
                "BuildTimer was loaded from a serialized snapshot and is read-only; cannot record new rules on it."
            )
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
            metadata=dict(metadata) if metadata else {},
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
        build_start_mono: float | None = None,
    ) -> None:
        """Parse .ninja_log entries appended after *offset* bytes.

        The .ninja_log format (v5) is tab-separated::

            # ninja log v5
            <start_ms>\\t<end_ms>\\t<mtime_ms>\\t<output>\\t<hash>

        When the same output appears multiple times, only the last
        entry is kept (ninja appends without truncating).

        ``build_start_mono`` is the value of ``time.monotonic()`` captured
        immediately before invoking ninja.  ninja's log records build-
        relative milliseconds (start_ms = 0 at ninja's launch); folding
        ``build_start_mono`` onto each timestamp anchors the rule events
        on the same monotonic timeline as in-Python phase events, so the
        Chrome trace lays them out coherently.  If omitted, ninja rules
        will appear ~50 years before phase events in the trace.
        """
        if not self.enabled:
            return
        if not os.path.exists(log_path):
            return

        source_for_output, type_for_output = self._build_graph_lookups(graph)
        anchor = build_start_mono or 0.0

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
                start_s=start_ms / 1000.0 + anchor,
                end_s=end_ms / 1000.0 + anchor,
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

        The recipe wrapper writes wall-clock timestamps (bash
        ``$EPOCHREALTIME``, falling back to ``date +%s%N``) because no
        portable shell builtin exposes ``CLOCK_MONOTONIC``.  Convert
        them into Python's monotonic clock domain here, using the
        wall-to-monotonic offset captured at BuildTimer init, so every
        rule event in the trace shares one clock regardless of which
        ingest path produced it.  Doing the calibration in Python keeps
        the recipe a pure bash builtin (no extra fork per compile)."""
        if not self.enabled:
            return
        if not os.path.exists(log_path):
            return

        source_for_output, type_for_output = self._build_graph_lookups(graph)
        offset = self._wall_to_monotonic_offset

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
                    start_s=start_ns / 1_000_000_000.0 + offset,
                    end_s=end_ns / 1_000_000_000.0 + offset,
                )

    # --------------------------------------------------- rule outcomes merge

    def set_root_metadata(self, attributes: dict[str, Any]) -> None:
        """Merge *attributes* into the root build event's metadata.

        The root metadata is lifted onto the root build span by
        ``otel/traces.py:export_buildtimer`` and serialised into
        ``timing.json`` by ``to_json``, so writing here BEFORE either of
        those means offline tooling sees the same attributes the OTel
        spans do. Public wrapper so callers don't reach into
        ``timer._root.metadata`` directly.
        """
        self._root.metadata.update(attributes)

    def merge_rule_outcomes(self, outcomes: dict[str, dict[str, Any]]) -> int:
        """Merge an ``{target: metadata}`` map into existing rule events.

        Walks the event tree once and, for each non-phase event whose
        target appears in *outcomes*, updates the event's ``metadata``
        dict with the supplied keys (existing keys are overwritten — the
        outcomes log is authoritative for ``cas.*``).  Returns the count
        of events that received metadata, for diagnostics.

        Safe to call after ``finish()`` and before ``export_buildtimer``;
        not safe to call concurrently with ``record_rule``.
        """
        if not outcomes:
            return 0
        merged = 0
        stack: list[TimingEvent] = [self._root]
        while stack:
            ev = stack.pop()
            stack.extend(ev.children)
            if ev.category == "phase" or not ev.target:
                continue
            md = outcomes.get(ev.target)
            if md is None:
                continue
            ev.metadata.update(md)
            merged += 1
        return merged

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
            # Anchor for to_chrome_trace: every event's start_s is on the
            # same monotonic clock (rule ingest paths normalize at write
            # time), so the chrome trace just subtracts this origin.
            "start_s": round(self._root.start_s, 6),
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
        """Deserialize from a dict (e.g. loaded from JSON).

        The returned timer is marked read-only: ``phase()`` and
        ``record_rule()`` will raise ``RuntimeError`` to prevent callers
        from mutating a historical snapshot (which would silently corrupt
        the on-disk JSON if it were re-saved).
        """
        timer = cls(enabled=True, variant=data.get("variant", ""), backend=data.get("backend", ""))
        root_start_s = data.get("start_s", 0.0)
        timer._root = TimingEvent(
            name="total",
            category="phase",
            start_s=root_start_s,
            end_s=root_start_s + data.get("total_elapsed_s", 0.0),
            children=[TimingEvent.from_dict(p) for p in data.get("phases", [])],
        )
        timer._phase_stack = [timer._root]
        timer._loaded = True
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
        ``{"traceEvents": [...]}`` and viewing in chrome://tracing or
        https://ui.perfetto.dev/.

        Every event's ``start_s`` is on the same monotonic clock —
        in-Python recorders use ``time.monotonic()`` directly, and the
        make/Slurm ingest paths convert wall-clock timestamps using the
        offset captured at ``BuildTimer.__init__``.  Subtract the root's
        ``start_s`` once to rebase the whole trace to ``ts=0``.
        """
        self.finish()
        events: list[dict[str, Any]] = []
        tid_counter = [1]
        self._chrome_trace_walk(self._root, events, tid=0, pid=1, tid_counter=tid_counter, origin_s=self._root.start_s)
        return events

    def _chrome_trace_walk(
        self,
        event: TimingEvent,
        events: list[dict[str, Any]],
        tid: int,
        pid: int,
        tid_counter: list[int],
        origin_s: float,
    ) -> None:
        ts_us = (event.start_s - origin_s) * 1_000_000
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
        if event.metadata:
            trace_event.setdefault("args", {}).update(event.metadata)

        # M0 attribution: render gate-wait as its own span immediately before
        # the rule span on the same lane, so the occupancy timeline separates
        # "parked on the concurrency gate" from "actually running". start_s is
        # the post-acquire timestamp, so the wait span ends exactly where the
        # rule span begins.
        queue_wait_s = event.metadata.get("queue_wait_s")
        if isinstance(queue_wait_s, (int, float)) and queue_wait_s > 0 and event.category != "phase":
            events.append(
                {
                    "name": f"{event.name} (queued)",
                    "cat": "queue_wait",
                    "ph": "X",
                    "ts": ts_us - queue_wait_s * 1_000_000,
                    "dur": queue_wait_s * 1_000_000,
                    "pid": pid,
                    "tid": tid,
                }
            )
        events.append(trace_event)

        # Phase children stay on the parent's lane; each rule child gets
        # its own tid from the global counter so parallel compiles don't
        # visually overlap in Perfetto.
        for child in event.children:
            child_tid = tid if child.category == "phase" else tid_counter[0]
            if child.category != "phase":
                tid_counter[0] += 1
            self._chrome_trace_walk(child, events, tid=child_tid, pid=pid, tid_counter=tid_counter, origin_s=origin_s)

    # ----------------------------------------------- per-category aggregation

    # Tests run inside build_execution (category="test"); there is no
    # separate test_execution phase.
    AGGREGATING_PHASES: ClassVar[frozenset[str]] = frozenset({"build_execution"})

    def aggregate_by_category(self, phase: TimingEvent) -> list[tuple[str, float, float, float, list[TimingEvent]]]:
        """Group a phase's child rules by category and aggregate them.

        Returns ``(category, wall, cpu, parallelism, events)`` tuples
        sorted by descending CPU, where:
          - ``wall`` is the union of intervals during which any rule of
            this category was running (real elapsed, accounting for
            parallel overlap).
          - ``cpu`` is the sum of per-rule durations (total work).
          - ``parallelism`` is ``cpu / wall`` (0 when ``wall`` is 0).

        Returns an empty list for phases not in ``AGGREGATING_PHASES``
        or with no children — callers should treat that as "no
        aggregation row to render".
        """
        if phase.name not in self.AGGREGATING_PHASES or not phase.children:
            return []
        by_cat: dict[str, list[TimingEvent]] = {}
        for child in phase.children:
            by_cat.setdefault(child.category, []).append(child)
        rows = []
        for cat, events in by_cat.items():
            cpu = sum(e.elapsed_s for e in events)
            wall = _union_span(events)
            parallelism = cpu / wall if wall > 0 else 0.0
            rows.append((cat, wall, cpu, parallelism, events))
        rows.sort(key=lambda r: -r[2])
        return rows

    # --------------------------------------------------------- summary table

    def summary_table(self):
        """Return a Rich Table summarizing the build timing.

        Phase rows show wall-clock elapsed time (Wall column) and the
        phase's share of total build wall-clock (% column).  CPU column
        is blank for phases.

        Sub-rows (per-rule-type) show:
          - Wall: union of intervals during which any rule of this
            category was running (real elapsed time spent on this
            category, accounting for parallel overlap).
          - CPU: sum of per-rule durations (total work performed).
          - %/parallelism: parallelism factor = CPU ÷ Wall, e.g.
            ``15.1×`` means the category averaged 15.1 cores busy.

        Returns None if rich is not available.
        """
        self.finish()
        try:
            from rich.table import Table
        except ImportError:
            return None

        table = Table(
            title=f"Build Timing Report ({self._root.elapsed_s:.1f}s total)",
            caption=(
                "Phase rows: wall-clock & % of total build. "
                "Indented rows: Wall = union of category intervals; "
                "CPU = sum of rule durations; parallelism = CPU ÷ Wall."
            ),
        )
        table.add_column("Phase", style="cyan", no_wrap=True)
        table.add_column("Wall (s)", justify="right", style="magenta")
        table.add_column("CPU (s)", justify="right", style="magenta")
        table.add_column("% / parallelism", justify="right")

        total = self._root.elapsed_s or 1.0

        for phase in self._root.children:
            pct = (phase.elapsed_s / total) * 100
            table.add_row(
                phase.name.replace("_", " ").title(),
                f"{phase.elapsed_s:.2f}",
                "",
                f"{pct:.1f}%",
            )
            for cat, wall, cpu, parallelism, _events in self.aggregate_by_category(phase):
                table.add_row(
                    f"  {cat.replace('_', ' ').title()}",
                    f"{wall:.2f}",
                    f"{cpu:.2f}",
                    f"{parallelism:.1f}×",
                )

        return table

    def print_summary(self) -> None:
        """Print the summary table and top slowest compilations.

        Skips rendering if no phases or rules were recorded so failure-path
        callers (e.g. `cake.py`'s ``finally`` block) don't print empty
        zero-time tables that obscure the real exception.
        """
        # Skip noisy zero-time output when nothing was recorded
        phases = self._root.children
        if not phases:
            return
        if not any(p.children for p in phases):
            return

        table = self.summary_table()
        if table is None:
            return
        try:
            import sys

            from rich.console import Console

            # Don't leak ANSI codes into non-TTY stderr (CI logs).
            force_terminal = False if not sys.stderr.isatty() else None
            console = Console(stderr=True, force_terminal=force_terminal)
            console.print(table)

            # Print slowest compilations and tests
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

            tests = sorted(
                [r for r in all_rules if r.category == "test"],
                key=lambda r: -r.elapsed_s,
            )
            if tests:
                console.print("\n[bold]Slowest tests:[/bold]")
                for rule in tests[:10]:
                    label = rule.target or rule.source
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
    """Guess rule type from output file extension.

    Buckets:
      - ``compile``        : ``.o``, ``.obj``
      - ``static_library`` : ``.a``, ``.lib`` (Windows static lib)
      - ``shared_library`` : ``.so``, ``.dylib``, ``.dll``
      - ``link``           : ``.exe`` (Windows executable) and bare names
                             (no extension; the Unix executable convention)
      - ``other``          : anything not matching the above (e.g. ``.gch``,
                             ``.pch``, generated headers).  Returned instead
                             of mis-bucketing as ``link`` so dashboards
                             can flag unknown artefacts explicitly.
    """
    # Use os.path.splitext so paths with dots in directory names work.
    # Lowercase the suffix so .EXE, .Lib, etc. classify correctly.
    suffix = os.path.splitext(output)[1].lower()
    if suffix in (".o", ".obj"):
        return "compile"
    if suffix in (".a", ".lib"):
        return "static_library"
    if suffix in (".so", ".dylib", ".dll"):
        return "shared_library"
    if suffix == ".exe" or suffix == "":
        return "link"
    return "other"
