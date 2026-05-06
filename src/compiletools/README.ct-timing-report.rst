=================
ct-timing-report
=================

------------------------------------------------------------
Analyze and display build timing reports
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-04-06
:Version: 8.3.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-timing-report [TIMING_FILE] [--summary] [--compare BEFORE AFTER] [--chrome-trace OUTPUT]

DESCRIPTION
===========

ct-timing-report displays build timing data collected by ``ct-cake --timing``.
It supports four output modes: an interactive TUI, a static summary table,
a comparison between two runs, and Chrome Trace export for visualization
in Perfetto.

When run without arguments, ct-timing-report launches an interactive TUI
with two views — a tree view (default) and a Gantt timeline view — that
can be switched between in-app.  Requires ``textual`` (install with
``pip install 'compiletools[tui]'``); falls back to the static summary
table if textual is not installed.

The timing file (``.ct-timing.json``) is auto-detected in the current
directory, ``bin/``, or ``obj/`` if not specified explicitly.

OPTIONS
=======
TIMING_FILE
    Path to a ``.ct-timing.json`` file.  If omitted, auto-detected in the
    current directory or common objdir locations.

--summary
    Print a static Rich summary table to stdout (non-interactive).
    See `SUMMARY TABLE`_ for column semantics.

--compare BEFORE AFTER
    Compare two timing files and display a delta table showing time
    differences per phase and per rule (top 5 largest deltas per phase).

--chrome-trace OUTPUT
    Export timing data as Chrome Trace JSON.  Open the resulting file in
    `Perfetto <https://ui.perfetto.dev/>`_ or ``chrome://tracing``.

INTERACTIVE TUI
===============

The TUI has two views.  Press **v** in the tree view to open the
timeline; press **t** or **escape** in the timeline to return to the
tree.

Tree view (ncdu-style)
----------------------

A hierarchical view of build phases, per-rule-type aggregations, and
individual rules.  Top-level rows are phases (``Build Execution``,
``Test Execution``, ...).  Inside the parallelizable phases (build and
test execution), an intermediate level groups rules by category
(``Compile``, ``Link``, ``Test``, ...) and shows the same Wall / CPU /
parallelism numbers as the static summary table.  Individual rules
appear as leaves under their category.

Keybindings:

- **Arrows / j / k** — navigate
- **Enter / Right** — expand node
- **Left** — collapse node
- **s** — cycle sort mode (time / name)
- **v** — switch to timeline view
- **q** — quit

Timeline view (Gantt)
---------------------

A swimlane-style view of every rule plotted on a wall-clock axis with
parallel rules packed into separate lanes.  Phases run as a coloured
header band above the lanes; rules are coloured by category (compile,
link, static_library, shared_library, test).  Selected events are
outlined with rose ``┃`` brackets; chevrons (``‹`` / ``›``) flag bars
that extend past the viewport.  A status panel at the bottom shows the
selected event's target, category, elapsed time, start/end relative to
build start, lane, and source path.

Keybindings:

- **+ / -** — zoom in / out (anchored on selection)
- **0** — fit entire build to width
- **← / → / h / l** — pan
- **↑ / ↓ / k / j** — switch lanes (selection follows)
- **n / p** — next / previous event in time order
- **Home / End** — first / last event
- **Mouse click** — select event under cursor
- **t / escape** — return to tree view
- **q** — quit
- **?** — help

SUMMARY TABLE
=============

The static summary (printed inline by ``ct-cake --timing`` and on demand
by ``ct-timing-report --summary``) has four columns:

================== =========================================================
Column             Meaning
================== =========================================================
Phase              Phase name (top-level rows) or rule category (indented).
Wall (s)           Wall-clock elapsed.  For phases, the phase span.  For
                   indented sub-rows, the union of intervals during which
                   any rule of that category was running -- i.e. real
                   elapsed time spent on the category, accounting for
                   parallel overlap.
CPU (s)            Sum of per-rule durations within the category (total
                   work performed across all rules).  Blank for phase
                   rows, which are not parallel-aggregated.
% / parallelism    Phase rows: share of total build wall-clock (``99.2%``).
                   Indented rows: parallelism factor = CPU ÷ Wall
                   (``15.2×`` means that category averaged 15.2 cores
                   busy).  The multiplier is reported instead of a
                   percentage because CPU/wall ratios on ``-j N`` builds
                   routinely exceed 10×, and percentages above 100% are
                   not informative.
================== =========================================================

Below the table, ``Slowest compilations`` and ``Slowest tests`` list the
ten longest individual rule durations from the run.

EXAMPLES
========

Build with timing and view the report::

    ct-cake --auto --timing
    ct-timing-report .ct-timing.json

Launch the interactive TUI (auto-detects timing file; press **v** for
the Gantt timeline view)::

    ct-timing-report

Print a static summary table::

    ct-timing-report --summary

Compare two builds::

    ct-timing-report --compare before.json after.json

Export for Perfetto visualization::

    ct-timing-report --chrome-trace out.json

SEE ALSO
========
``ct-cake`` (1), ``compiletools`` (1)
