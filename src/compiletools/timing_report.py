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


def _find_timing_file(path: str | None) -> str | None:
    """Resolve timing file path, auto-detecting in cwd if not given."""
    if path:
        return path
    default = ".ct-timing.json"
    if os.path.exists(default):
        return default
    # Try common objdir locations
    for candidate in ["bin/.ct-timing.json", "obj/.ct-timing.json"]:
        if os.path.exists(candidate):
            return candidate
    return None


def _resolve_and_load(args) -> BuildTimer | None:
    """Find and load the timing file, printing errors on failure."""
    path = _find_timing_file(getattr(args, "timing_file", None))
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
    pct_total = (delta_total / before_total * 100) if before_total > 0 else 0

    delta_str = f"{delta_total:+.1f}s / {pct_total:+.1f}%"
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
        pct = (delta / b_time * 100) if b_time > 0 else 0

        delta_style = "green" if delta < 0 else ("red" if delta > 0 else "")
        table.add_row(
            name.replace("_", " ").title(),
            f"{b_time:.2f}",
            f"{a_time:.2f}",
            f"[{delta_style}]{delta:+.2f}[/]",
            f"[{delta_style}]{pct:+.1f}%[/]",
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
                rpct = (rd / bt * 100) if bt > 0 else 0
                rs = "green" if rd < 0 else "red"
                label = os.path.basename(key) if "/" in key else key
                table.add_row(
                    f"  {label}",
                    f"{bt:.2f}",
                    f"{at:.2f}",
                    f"[{rs}]{rd:+.2f}[/]",
                    f"[{rs}]{rpct:+.1f}%[/]",
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

    path = _find_timing_file(getattr(args, "timing_file", None)) or ""
    app = TimingReportApp(timer, path)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
