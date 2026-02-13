==========================
profile-ct-cake-worktree
==========================

------------------------------------------------------------------
Safely profile ct-cake performance using temporary git worktrees
------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
profile-ct-cake-worktree [OPTIONS]

DESCRIPTION
===========
This utility automates side-by-side profiling of ``ct-cake`` between two
branches. It creates disposable git worktrees, installs each branch into its
own virtual environment via ``uv``, runs ``ct-cake`` against representative
sample projects under ``src/compiletools/samples/``, and captures cumulative
timings with ``cProfile`` + ``pstats``.

The workflow never touches your main checkout; all edits occur in temporary
directories that are removed automatically unless ``--keep-worktrees`` is set.
Results are printed as human-readable tables highlighting per-sample regressions
and, optionally, detailed hotspot information.

OPTIONS
=======
``--baseline-branch BRANCH``
    Git branch used as the performance baseline. Defaults to ``master``.

``--current-branch BRANCH``
    Branch under test. Defaults to the branch currently checked out in the main
    worktree.

``--magic-modes {direct,cpp}``
    One or more ``ct-cake --magic`` modes to profile. Defaults to both.

``--save-profiles``
    Persist the raw ``.prof`` files under ``profiles_worktree/`` for later
    inspection with ``pstats`` or ``snakeviz``.

``--save-results FILE``
    Write the structured timing data and metadata to ``FILE`` (JSON format).

``--keep-worktrees``
    Leave the temporary worktrees on disk for manual debugging.

``--verbose``
    Print extended hotspot analysis for the preferred sample (lotsofmagic,
    factory, then simple).

OUTPUT
======
The tool prints, for each magic mode, a table that lists per-sample wall clock
time, total call counts, and whether the new branch is faster/slower. When
``--verbose`` is enabled it also surfaces the top functions contributing to the
change grouped by compiletools module, I/O, and cache behavior.

ENVIRONMENT & REQUIREMENTS
==========================
* ``git`` with worktree support.
* ``uv`` available on ``PATH`` for creating virtual environments and installing
  the package.
* Ability to install compiletools in editable mode (system compilers must be
  available).
* Optional ``profiles_worktree/`` directory will be created in the current repo
  root when saving profiles.

EXAMPLES
========

Compare ``master`` vs ``feature/magic-opt`` and save raw profiles::

    profile-ct-cake-worktree --current-branch feature/magic-opt --save-profiles

Generate JSON results for documentation without keeping temporary worktrees::

    profile-ct-cake-worktree --baseline-branch v6.1.4 --current-branch master \
        --save-results perf.json

SEE ALSO
========
``ct-cake`` (1), ``uv`` (1), ``git-worktree`` (1), ``pstats`` (1)
