=================
ct-timing-report
=================

------------------------------------------------------------
Analyze and display build timing reports
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-04-06
:Version: 8.0.3
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

When run without arguments, ct-timing-report launches an interactive
ncdu-style TUI (requires ``textual``; install with
``pip install 'compiletools[tui]'``).  If textual is not installed, it
falls back to the static summary table.

The timing file (``.ct-timing.json``) is auto-detected in the current
directory, ``bin/``, or ``obj/`` if not specified explicitly.

OPTIONS
=======
TIMING_FILE
    Path to a ``.ct-timing.json`` file.  If omitted, auto-detected in the
    current directory or common objdir locations.

--summary
    Print a static Rich summary table to stdout (non-interactive).

--compare BEFORE AFTER
    Compare two timing files and display a delta table showing time
    differences per phase and per rule (top 5 largest deltas per phase).

--chrome-trace OUTPUT
    Export timing data as Chrome Trace JSON.  Open the resulting file in
    `Perfetto <https://ui.perfetto.dev/>`_ or ``chrome://tracing``.

INTERACTIVE TUI
===============

The TUI presents a tree view of build phases and individual compile/link
rules with time, percentage, and a proportional bar chart.

Keybindings:

- **Arrows / j / k** — navigate
- **Enter / Right** — expand node
- **Left** — collapse node
- **s** — cycle sort mode (time / name)
- **q** — quit

EXAMPLES
========

Build with timing and view the report::

    ct-cake --auto --timing
    ct-timing-report .ct-timing.json

Launch the interactive TUI (auto-detects timing file)::

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
