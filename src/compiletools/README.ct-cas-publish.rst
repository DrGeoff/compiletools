==============
ct-cas-publish
==============

------------------------------------------------------------------------
Atomically publish a CAS artefact at a stable user-facing path
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 10.0.8
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cas-publish --cas-path=PATH --user-path=PATH [--source-realpath=PATH]

DESCRIPTION
===========

``ct-cas-publish`` is a small helper invoked from generated build recipes
(Make, Ninja, Shake, Slurm) to publish a content-addressable linker
artefact at the stable user-facing ``bin/<variant>/<name>`` (or
``bin/<variant>/lib<name>.{a,so}``) path. It is not normally run by
hand.

Given a producer rule that has just written a binary into
``cas-exedir`` (e.g.
``cas-exedir/<linkkey[:2]>/<basename>_<linkkey>.exe``), the helper
publishes that file at ``--user-path`` using a POSIX-atomic
``link()`` + ``rename()`` pair. The kernel guarantees ``--user-path``
is always present (either the previous inode or the new one) for any
concurrent reader, so a parallel build cannot observe a missing target
during a publish.

If ``link()`` fails with ``EXDEV`` — the user path lives on a different
filesystem from the cas entry — the helper falls back to
``symlink()`` + ``rename()``. Any other ``OSError`` (``ENOSPC``,
``EPERM``, ``EROFS``, ``EMFILE``) is re-raised visibly. The previous
shell recipe (``ln -f cas user 2>/dev/null || ln -sfn cas user``)
swallowed those errors and silently downgraded to a symlink, which
would then break ``trim_exedir``'s hard-link protection by leaving
``nlink == 1`` on the cas entry.

After a successful publish, the helper writes a best-effort sidecar
manifest at ``<cas-path>.manifest`` containing
``{"source_realpath": ...}``. ``ct-trim-cache --cas-exedir-only``
reads this manifest to bucket entries by source identity rather than
basename, which disambiguates distinct executables that happen to
share a basename like ``main``. Sidecar errors are non-fatal: a
missing or corrupt manifest just falls back to legacy basename
bucketing.

The publish itself failing IS fatal — the helper exits non-zero and
the caller (a build rule) fails the build.

OPTIONS
=======

``--cas-path PATH`` (required)
    Source path inside the CAS — the file the link or ar rule just
    wrote. Typically of the form
    ``<cas-exedir>/<linkkey[:2]>/<basename>_<linkkey>.{exe,a,so}``.

``--user-path PATH`` (required)
    Destination user-facing path. Typically
    ``<bindir>/<basename>`` for executables or
    ``<bindir>/lib<basename>.{a,so}`` for libraries. The parent
    directory is created with ``os.makedirs(..., exist_ok=True)`` if
    it does not yet exist.

``--source-realpath PATH``
    Resolved realpath of the source ``.cpp`` (executable) or library
    target. Written into the ``<cas-path>.manifest`` sidecar so
    ``ct-trim-cache`` can bucket by source identity rather than
    basename. Optional but recommended; omitting it leaves the
    sidecar absent and trim falls back to basename bucketing.

ATOMICITY CONTRACT
==================

1. ``link(cas_path, tmp)`` then ``rename(tmp, user_path)`` — POSIX-
   atomic replacement. Concurrent readers always see a consistent
   inode at ``user_path``; concurrent peer publishers racing on the
   same path produce a final state that points at one of their cas
   inputs, all byte-equivalent because their CAS keys collided.
2. On ``EXDEV``: ``symlink(cas_path, tmp)`` then ``rename(tmp,
   user_path)``. Same atomic-replacement pattern.
3. Any other ``OSError``: re-raise visibly (no silent symlink
   degradation).
4. Inode swap under a process holding ``user_path`` open is harmless
   on POSIX — the open file descriptor pins the old inode.

EXIT CODES
==========

0
    Success — ``user_path`` now points (via hardlink or symlink
    fallback) at the byte-equivalent CAS entry, and the sidecar
    manifest has been written if ``--source-realpath`` was supplied.
1
    Failure — propagates argparse error or any unrecovered ``OSError``
    from ``link()`` / ``symlink()`` / ``rename()``. The ``user_path``
    is never left in a partial state.

CONCURRENCY
===========

Idempotent on re-runs: the rename overwrites cleanly. Two parallel
build invocations targeting the same ``user_path`` race safely —
whichever rename wins is correct (both are publishing byte-equivalent
artefacts because their cas-exedir keys collided).

The companion lock-aware delete in ``ct-trim-cache --cas-exedir-only``
re-stats ``nlink`` under the producer's lock to close the
scan-to-unlink TOCTOU window: a peer publish that elevates ``nlink``
mid-trim aborts the unlink.

EXAMPLES
========

Generated Make recipe (typical caller; not user-invoked)::

    bin/blank/myapp: cas-exedir/ab/myapp_abcd1234ef567890.exe
        ct-cas-publish \
            --cas-path=cas-exedir/ab/myapp_abcd1234ef567890.exe \
            --user-path=bin/blank/myapp \
            --source-realpath=/home/user/proj/src/myapp.cpp

Manual invocation for diagnostic / cache-priming use::

    ct-cas-publish \
        --cas-path=$GIT_ROOT/cas-exedir/de/util_deadbeefcafe1234.exe \
        --user-path=$GIT_ROOT/bin/blank/util

SEE ALSO
========

``ct-cake`` (1) -- generates the recipes that invoke this helper

``ct-trim-cache`` (1) -- reads the sidecar manifests this helper writes;
documents the bucketing and hard-link-protection invariants

``ct-cache-report`` (1) -- consumes the same ``.manifest`` sidecars to
group exedir entries by ``source_realpath`` when reporting duplication

``ct-backends`` (7) -- "MTIME VS CAS REBUILD MODE" and the linker-
artefact discussion in CONTENT-ADDRESSABLE OUTPUTS
