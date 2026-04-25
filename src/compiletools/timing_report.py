"""Build timing report viewer — static, interactive TUI, comparison, and Chrome trace export.

Usage::

    ct-timing-report                          # TUI (or fallback to summary)
    ct-timing-report .ct-timing.json          # TUI for specific file
    ct-timing-report --summary                # print Rich table
    ct-timing-report --compare a.json b.json  # diff two runs
    ct-timing-report --chrome-trace out.json   # export for Perfetto
"""

from __future__ import annotations

import json
import os
import sys

import compiletools.apptools
from compiletools.build_timer import BuildTimer

# ------------------------------------------------------------------ CLI


def main(argv=None) -> int:
    parser = compiletools.apptools.create_parser(
        "Analyze and display build timing reports from ct-cake --timing.",
        argv=argv,
        include_config=False,
    )
    parser.add(
        "timing_file",
        nargs="?",
        default=None,
        help="Path to .ct-timing.json (default: auto-detect in cwd)",
    )
    parser.add(
        "--summary",
        action="store_true",
        help="Print summary table to stdout (non-interactive)",
    )
    parser.add(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="Compare two timing files and show deltas",
    )
    parser.add(
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


def _find_timing_file(path: str | None, objdir: str | None = None) -> str | None:
    """Resolve timing file path, auto-detecting in cwd if not given.

    Search order:
      1. Explicit ``path`` argument (if given).
      2. ``./.ct-timing.json`` in cwd.
      3. ``{objdir}/.ct-timing.json`` if ``objdir`` is provided.
      4. Common objdir/bindir conventions: ``bin/``, ``obj/``.

    Users with a custom ``--objdir=shared-objdir/...`` should pass
    ``objdir`` so the search reaches their actual output location.
    """
    if path:
        return path
    default = ".ct-timing.json"
    if os.path.exists(default):
        return default
    candidates = []
    if objdir:
        candidates.append(os.path.join(objdir, ".ct-timing.json"))
    candidates.extend(["bin/.ct-timing.json", "obj/.ct-timing.json"])
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_and_load(args) -> BuildTimer | None:
    """Find and load the timing file, printing errors on failure."""
    objdir = getattr(args, "objdir", None)
    path = _find_timing_file(getattr(args, "timing_file", None), objdir=objdir)
    if path is None:
        print("No .ct-timing.json found. Run ct-cake --timing first.", file=sys.stderr)
        return None
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        return None
    return BuildTimer.from_json(path)


# -------------------------------------------------------- summary mode


def _print_summary(args) -> int:
    timer = _resolve_and_load(args)
    if timer is None:
        return 1
    timer.print_summary()
    return 0


# ------------------------------------------------- chrome trace export


def _export_chrome_trace(args) -> int:
    timer = _resolve_and_load(args)
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
    timer = _resolve_and_load(args)
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

    path = _find_timing_file(getattr(args, "timing_file", None), objdir=getattr(args, "objdir", None)) or ""
    app = TimingReportApp(timer, path)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
