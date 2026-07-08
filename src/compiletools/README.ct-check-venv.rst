================
ct-check-venv
================

-------------------------------------------------------------------------
Verify that the ct-cake on PATH imports the same compiletools as the venv
-------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 10.2.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-check-venv

DESCRIPTION
===========
``ct-check-venv`` answers a single question: does the ``ct-cake`` on
``PATH`` import the same ``compiletools`` package as the venv that
``ct-check-venv`` was launched from?

It exists because editable installs (``pip install -e .`` /
``uv pip install -e .``) record the *path the install was performed from*.
A venv created in one worktree (e.g. ``compiletools/master/``) keeps
importing ``compiletools`` from that worktree's ``src/`` even after you
``cd`` into a sibling worktree (e.g. ``compiletools/my-feature/``) and
activate the same venv. Subprocess-driven tools like ``ct-cake`` and the
e2e test suites would then silently exercise the wrong code.

``ct-check-venv`` flags this mismatch up-front so you don't waste time
chasing a phantom regression.

WHEN TO RUN
===========
- After creating or activating a venv in a new worktree.
- After ``git worktree add`` -- the new worktree needs its own
  ``uv pip install -e .`` (or ``pip install -e .``) before
  subprocess-driven tools work correctly.
- When ``ct-cake`` "behaves like an old version" or fails in ways that
  don't match the source you're editing.
- At the start of CI runs that exercise e2e markers.

USAGE
=====
::

    # From inside the worktree's activated venv:
    ct-check-venv

EXIT CODES
==========
0
    ``ct-cake`` on PATH and the calling Python both import
    ``compiletools`` from the same install root. Prints an ``ok:`` line
    naming that root.

1
    Mismatch detected, or ``ct-cake`` was not found / not introspectable.
    Prints an actionable diagnostic on stderr -- typically the two
    diverging paths and a hint to run ``uv pip install -e .`` from this
    worktree.

HOW IT WORKS
============
1. Locates ``ct-cake`` on ``PATH`` via ``shutil.which``.
2. Reads its shebang line to find the Python interpreter the venv
   installed it for. Falls back gracefully when shebang parsing isn't
   possible (e.g. native-binary launchers).
3. Runs that interpreter with ``-c "import compiletools; ..."`` to print
   the realpath of the install root the script would actually use.
4. Compares that realpath to the install root the calling Python sees.

If the two roots match, exit 0. Otherwise, exit 1 with a one-line
diagnostic naming both paths and the fix.

PROGRAMMATIC USE
================
The same probe is exposed for the test suite:

- ``compiletools.check_venv.venv_mismatch_reason(expected_src_root)``
  returns ``None`` on match or a human-readable string on mismatch.
- ``compiletools.check_venv.cached_venv_mismatch_reason`` is the LRU-
  cached form used by ``compiletools.testhelper.skipif_e2e_unavailable``
  to skip e2e markers with a clear message instead of failing
  mysteriously when the venv points at a stale worktree.

EXAMPLES
========
**Healthy venv**::

    $ ct-check-venv
    ok: ct-cake ('/path/to/.venv/bin/ct-cake') and ct-check-venv both
    resolve compiletools to '/home/me/code/compiletools/master/src'

**Mismatched venv** (created in master, used from a feature worktree)::

    $ ct-check-venv
    venv mismatch: ct-cake imports compiletools from
    '/home/me/code/compiletools/master/src', but the caller expects
    '/home/me/code/compiletools/my-feature/src'. The venv's editable
    install points at a different worktree, so e2e tests / ct-cake
    invocations would exercise the wrong code. Fix with
    `uv pip install -e .` (or `pip install -e .`) from this worktree.
    $ echo $?
    1

SEE ALSO
========
``compiletools`` (1), ``ct-cake`` (1)
