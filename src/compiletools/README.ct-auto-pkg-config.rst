====================
ct-auto-pkg-config
====================

-------------------------------------------------------------------------------------------
Report the pkg-config packages that auto-inference attributes to includes
-------------------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-09-02
:Copyright: Copyright (C) 2011-2026 Zomojo Pty Ltd
:Version: 13.1.4
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-auto-pkg-config [--style {pretty,json,magic}] [--fail-on-ambiguous]
                   [-v] [-q] [--variant VARIANT] filename [filename ...]

DESCRIPTION
===========
``ct-auto-pkg-config`` parses each requested file through the production
``ct-magicflags`` pipeline with ``--auto-pkg-config`` forced on, and reports
the verdicts of the inference layer: which ``#include`` was attributed to
which pkg-config package, and — for every include that was *not* attributed —
the reason (standard header, project header, covered by an explicit flag,
ambiguous, no package exports it, the declared package that owns the header
is unavailable, ...). It is a diagnostic: it changes
nothing on disk, prints nothing for files with no inferences (under
``--style magic``), and exits 0 unless ``--fail-on-ambiguous`` is given and
some include is ambiguous.

The tool forces the feature on by prepending ``--auto-pkg-config`` to the
command line, so a conf-file ``auto-pkg-config = false`` or
``CT_AUTO_PKG_CONFIG=0`` cannot turn the report into a silent no-op. It
also prepends ``--pkg-config-errors=warn`` so a strict conf/env policy
(``pkg-config-errors = error``) cannot abort the parse on the first
ambiguous include before the later files are reported; an explicit
``--pkg-config-errors=error`` on the command line still wins and restores
the strict abort — the run then exits 1 on the first ambiguity, before any
report is emitted.

OPTIONS
=======

--style {pretty,json,magic}
                    Output style (default: pretty).

                    ``pretty`` prints one line per header:
                    ``<file>: <header> -> <pkg> (via <resolver>)`` for a
                    resolved include, ``<file>: <header> unresolved: <reason>``
                    otherwise.

                    ``json`` prints one machine-readable object:
                    ``{"schema": 1, "files": {<file>: [{header, header_path,
                    package, resolver, candidates, reason}, ...]}}``.

                    ``magic`` prints, for each file with at least one inferred
                    package, a ``// <file>`` comment line followed by sorted,
                    deduped ``//#PKG-CONFIG=<pkg>`` lines — ready to paste.

--fail-on-ambiguous
                    Exit 1 when any include verdict is ambiguous. Under the
                    forced ``--pkg-config-errors=warn`` the parse itself
                    continues and the ambiguity is reported (with the
                    candidates and the ``auto-pkg-config-map`` remedy);
                    an explicit ``--pkg-config-errors=error`` makes the
                    parse abort on the ambiguity instead.

All other flags are the standard ``ct-magicflags`` surface (``--variant``,
``--pkg-config``, ``--auto-pkg-config-map``, ``--auto-pkg-config-ignore``,
``--auto-pkg-config-resolvers``, verbosity, ...).

THE FREEZE WORKFLOW
===================
Teams that share one content-addressable cache across hosts with different
devel packages installed want host-independent cache keys. Freeze the
inferred packages into explicit annotations:

.. code-block:: console

    ct-auto-pkg-config --style magic src/*.cpp

and paste the emitted ``//#PKG-CONFIG=<pkg>`` lines into the sources (or feed
the output through a formatter and commit it). A subsequent build with
``--no-auto-pkg-config`` produces the identical flags, so the cache key no
longer depends on which packages happen to be installed on the build host.

EXAMPLES
========

* ``ct-auto-pkg-config src/main.cpp``
* ``ct-auto-pkg-config --style=json src/*.cpp | jq .``
* ``ct-auto-pkg-config --style=magic --fail-on-ambiguous src/*.cpp``

SEE ALSO
========
``compiletools`` (1), ``ct-magicflags`` (1), ``ct-cake`` (1)
