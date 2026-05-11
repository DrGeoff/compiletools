"""Build timing report viewer — static, interactive TUI, comparison, and Chrome trace export.

Usage::

    ct-timing-report                          # TUI (or fallback to summary)
    ct-timing-report timing.json              # TUI for specific file
    ct-timing-report --summary                # print Rich table
    ct-timing-report --compare a.json b.json  # diff two runs
    ct-timing-report --chrome-trace out.json   # export for Perfetto
"""

from __future__ import annotations

import json
import os
import sys

import compiletools.apptools
import compiletools.configutils
from compiletools.build_timer import BuildTimer
from compiletools.diagnostics import INVOCATION_ID_RE

# ------------------------------------------------------------------ CLI


def main(argv=None) -> int:
    parser = compiletools.apptools.create_parser(
        "Analyze and display build timing reports from ct-cake --timing.",
        argv=argv,
        include_config=True,
    )
    # ct-cake writes timing.json under <bindir>/diagnostics/<invocation-id>/.
    # Registering --bindir / --diagnostics-dir lets ct-timing-report participate
    # in the same configargparse layering (CLI > env > ct.conf > default), so
    # an orchestrator that already exports DIAGNOSTICS_DIR for ct-cake gets
    # auto-discovery here for free.
    variant = compiletools.configutils.extract_variant(argv=argv)
    compiletools.apptools.add_base_arguments(parser, argv=argv, variant=variant)
    compiletools.apptools.add_output_directory_arguments(parser, variant=variant)
    parser.add_argument(
        "--diagnostics-dir",
        default=None,
        help=(
            "Parent directory for per-invocation diagnostic artifacts. "
            "When set, ct-timing-report looks for the newest "
            "<diagnostics-dir>/<invocation-id>/timing.json. Defaults to "
            "<bindir>/diagnostics/. Also settable via the DIAGNOSTICS_DIR "
            "environment variable or 'diagnostics-dir = <path>' in any "
            "ct.conf file."
        ),
    )
    parser.add_argument(
        "timing_file",
        nargs="?",
        default=None,
        help="Path to timing.json (default: auto-detect in cwd / diagnostics-dir / bindir)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary table to stdout (non-interactive)",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="Compare two timing files and show deltas",
    )
    parser.add_argument(
        "--chrome-trace",
        metavar="OUTPUT",
        help="Export Chrome Trace JSON for Perfetto (https://ui.perfetto.dev/)",
    )
    args = parser.parse_args(argv)

    if args.chrome_trace:
        return _export_chrome_trace(args)
    elif args.compare:
        return _run_comparison(args)
    elif args.summary:
        return _print_summary(args)
    else:
        return _run_tui(args)


# -------------------------------------------------------- file loading


def _newest_invocation_timing(diagnostics_dir: str) -> str | None:
    """Return ``<diagnostics_dir>/<newest-invocation>/timing.json`` or None.

    The "newest invocation" is the entry in ``diagnostics_dir`` whose name
    matches ``INVOCATION_ID_RE`` (the ``YYYYMMDDTHHMMSS-PID`` format from
    ``diagnostics.invocation_id()``) with the greatest ``(timestamp, pid)``
    key. Non-matching entries (stray files, tmp dirs, etc.) are ignored.
    Returns None if the dir doesn't exist, has no matching entries, or the
    newest entry has no timing.json yet.
    """
    if not os.path.isdir(diagnostics_dir):
        return None
    try:
        entries = os.listdir(diagnostics_dir)
    except OSError:
        return None
    invocations = [
        name for name in entries if INVOCATION_ID_RE.match(name) and os.path.isdir(os.path.join(diagnostics_dir, name))
    ]
    if not invocations:
        return None

    # Sort by (timestamp_str, int(pid)): within the same wall-clock second,
    # lex sort orders '-1000' before '-999' because '1' < '9'. PID must be
    # compared numerically. INVOCATION_ID_RE guarantees one '-' separator.
    def _key(name: str) -> tuple[str, int]:
        ts, pid = name.rsplit("-", 1)
        return (ts, int(pid))

    invocations.sort(key=_key)
    candidate = os.path.join(diagnostics_dir, invocations[-1], "timing.json")
    return candidate if os.path.exists(candidate) else None


def _find_timing_file(
    path: str | None,
    *,
    objdir: str | None = None,
    bindir: str | None = None,
    diagnostics_dir: str | None = None,
) -> str | None:
    """Resolve timing file path, auto-detecting if not given.

    Search order:
      1. Explicit ``path`` argument (if given).
      2. ``./timing.json`` in cwd (the new no-leading-dot filename).
      3. ``./.ct-timing.json`` in cwd (legacy name, kept for one release).
      4. Newest ``<diagnostics-dir>/<invocation-id>/timing.json``. If
         ``diagnostics_dir`` is provided, use it directly; otherwise
         derive ``<bindir>/diagnostics/`` if ``bindir`` is provided.
      5. ``{objdir}/.ct-timing.json`` if ``objdir`` is provided (legacy).
      6. ``bin/.ct-timing.json``, ``obj/.ct-timing.json`` (legacy).

    A cwd hit short-circuits the diagnostics-dir lookup, so a stale
    ``./timing.json`` will outrank a fresh diagnostics-dir entry.
    """
    if path:
        return path
    if os.path.exists("timing.json"):
        return "timing.json"
    if os.path.exists(".ct-timing.json"):
        return ".ct-timing.json"
    diag_dir = diagnostics_dir
    if diag_dir is None and bindir:
        diag_dir = os.path.join(bindir, "diagnostics")
    if diag_dir:
        found = _newest_invocation_timing(diag_dir)
        if found is not None:
            return found
    candidates = []
    if objdir:
        candidates.append(os.path.join(objdir, ".ct-timing.json"))
    candidates.extend(["bin/.ct-timing.json", "obj/.ct-timing.json"])
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_and_load(args) -> tuple[BuildTimer | None, str | None]:
    """Find and load the timing file, returning ``(timer, path)``.

    Returns the path the timer was loaded from alongside the timer so callers
    that need both (e.g. the TUI title) don't have to re-run ``_find_timing_file``.
    A second lookup would also open a TOCTOU window: a peer ``ct-cake``
    invocation can write a NEWER ``<diagnostics-dir>/<invocation-id>/`` between
    the two calls, leaving the loaded timer and the displayed path pointing at
    different invocations.

    On failure, prints to stderr and returns ``(None, None)``.
    """
    path = _find_timing_file(
        getattr(args, "timing_file", None),
        objdir=getattr(args, "cas_objdir", None),
        bindir=getattr(args, "bindir", None),
        diagnostics_dir=getattr(args, "diagnostics_dir", None),
    )
    if path is None:
        print("No timing.json found. Run ct-cake --timing first.", file=sys.stderr)
        return None, None
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        return None, None
    return BuildTimer.from_json(path), path


# -------------------------------------------------------- summary mode


def _print_summary(args) -> int:
    timer, _ = _resolve_and_load(args)
    if timer is None:
        return 1
    timer.print_summary()
    return 0


# ------------------------------------------------- chrome trace export


def _export_chrome_trace(args) -> int:
    timer, _ = _resolve_and_load(args)
    if timer is None:
        return 1
    events = timer.to_chrome_trace()
    with open(args.chrome_trace, "w", encoding="utf-8") as f:
        json.dump({"traceEvents": events}, f, indent=2)
        f.write("\n")
    print(f"Chrome trace written to {args.chrome_trace}")
    print("Open in https://ui.perfetto.dev/ or chrome://tracing")
    return 0


# -------------------------------------------------------- comparison mode


def _styled(text: str, style: str) -> str:
    """Wrap ``text`` in Rich markup, or return it bare when no style applies.

    Empty markup tags ('[]…[/]') raise rich.errors.MarkupError, so the empty
    style must short-circuit rather than render.
    """
    return f"[{style}]{text}[/]" if style else text


def _format_pct(b_time: float, delta: float) -> str:
    """Format a percentage-change cell honestly when the baseline is zero.

    The naive ``delta / b_time * 100 if b_time > 0 else 0`` form silently
    returns 0% when a phase is *new* (b_time == 0, delta > 0), hiding a real
    regression. Use ``(new)`` for that case and an em-dash for genuine no-ops.
    """
    if b_time > 0:
        return f"{(delta / b_time * 100):+.1f}%"
    return "(new)" if delta > 0 else "—"


def _run_comparison(args) -> int:
    before_path, after_path = args.compare
    for p in (before_path, after_path):
        if not os.path.exists(p):
            print(f"File not found: {p}", file=sys.stderr)
            return 1

    before = BuildTimer.from_json(before_path)
    after = BuildTimer.from_json(after_path)

    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print("rich is required for comparison mode.", file=sys.stderr)
        return 1

    console = Console(stderr=True)

    before_total = before.total_elapsed_s
    after_total = after.total_elapsed_s
    delta_total = after_total - before_total

    delta_str = f"{delta_total:+.1f}s / {_format_pct(before_total, delta_total)}"
    table = Table(
        title=f"Build Comparison: {os.path.basename(before_path)} ({before_total:.1f}s) "
        f"vs {os.path.basename(after_path)} ({after_total:.1f}s) [{delta_str}]"
    )
    table.add_column("Phase / Rule", style="cyan", no_wrap=True)
    table.add_column("Before (s)", justify="right")
    table.add_column("After (s)", justify="right")
    table.add_column("Delta", justify="right")
    table.add_column("Change", justify="right")

    # Compare phases
    before_phases = {p.name: p for p in before.phases}
    after_phases = {p.name: p for p in after.phases}
    all_phase_names = list(dict.fromkeys(list(before_phases) + list(after_phases)))

    for name in all_phase_names:
        bp = before_phases.get(name)
        ap = after_phases.get(name)
        b_time = bp.elapsed_s if bp else 0.0
        a_time = ap.elapsed_s if ap else 0.0
        delta = a_time - b_time

        delta_style = "green" if delta < 0 else ("red" if delta > 0 else "")
        table.add_row(
            name.replace("_", " ").title(),
            f"{b_time:.2f}",
            f"{a_time:.2f}",
            _styled(f"{delta:+.2f}", delta_style),
            _styled(_format_pct(b_time, delta), delta_style),
        )

        # Compare rules within phases
        if bp and ap:
            before_rules = {r.target or r.name: r for r in bp.children}
            after_rules = {r.target or r.name: r for r in ap.children}
            all_rule_keys = list(dict.fromkeys(list(before_rules) + list(after_rules)))
            rule_deltas = []
            for key in all_rule_keys:
                br = before_rules.get(key)
                ar = after_rules.get(key)
                bt = br.elapsed_s if br else 0.0
                at = ar.elapsed_s if ar else 0.0
                rule_deltas.append((key, bt, at, at - bt))

            # Show top 5 largest deltas
            rule_deltas.sort(key=lambda x: abs(x[3]), reverse=True)
            for key, bt, at, rd in rule_deltas[:5]:
                if abs(rd) < 0.01:
                    continue
                rs = "green" if rd < 0 else "red"
                label = os.path.basename(key) if "/" in key else key
                table.add_row(
                    f"  {label}",
                    f"{bt:.2f}",
                    f"{at:.2f}",
                    _styled(f"{rd:+.2f}", rs),
                    _styled(_format_pct(bt, rd), rs),
                )

    console.print(table)
    return 0


# -------------------------------------------------------------- TUI mode


def _run_tui(args) -> int:
    timer, path = _resolve_and_load(args)
    if timer is None:
        return 1

    try:
        from compiletools.timing_tui import TimingReportApp
    except ImportError:
        print(
            "Textual is not installed. Falling back to summary output.\n"
            "Install with: pip install 'compiletools[tui]'\n",
            file=sys.stderr,
        )
        return _print_summary(args)

    app = TimingReportApp(timer, path or "")
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
