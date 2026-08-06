==============
ct-cas-publish
==============

------------------------------------------------------------------------
Atomically publish a CAS artefact at a stable user-facing path
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 12.1.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cas-publish --cas-path=PATH --user-path=PATH [--source-realpath=PATH]

DESCRIPTION
===========

``ct-cas-publish`` is a small helper invoked from generated build recipes
(Make, Ninja, Shake) to publish a content-addressable linker
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

The whole sequence runs while holding the ``<cas-path>.lock`` sidecar,
the same lock ``ct-trim-cache`` takes before evicting an entry, so a
publish and a concurrent trim of the same CAS entry are totally
ordered. Trim first leaves nothing to publish: the helper reports
``CAS entry missing at publish (<path>): removed by a concurrent trim,
or never produced; rerun the build`` and exits ``3``, and the next
build rebuilds the artefact. Publish first raises
the entry's ``nlink`` to 2, which trim's hard-link protection honours.

Those two are the only outcomes on the hardlink path. A publish that
fell back to ``symlink()`` leaves the entry at ``nlink == 1``, which
``trim_exedir``'s hard-link protection deliberately does not cover —
see the "Hard-link safety" rule documented under ``ct-trim-cache``.
Such a publish reports success and is still ordered against a
concurrent trim, but a later trim can evict the entry and leave the
published path a dangling symlink. The lock buys ordering for both
paths; only the hard link buys protection after the fact, and the
recovery in either case is to rebuild.

Publishing also freshens the CAS entry's mtime (best-effort; another
user's entry on a shared pool is not ours to touch). Age-gated sweeps
— ``ct-trim-cache --max-age`` and the oldest-first ``--max-size``
budget — rank by mtime, and without this an entry that every build
still publishes would age out on the timestamp it was created with.

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

1. Take the ``<cas-path>.lock`` sidecar (never the entry itself —
   locking the artefact path would create an empty, ``mtime=now``
   file that a peer ``make`` reads as up-to-date), then re-verify the
   entry still exists.
2. ``link(cas_path, tmp)`` then ``rename(tmp, user_path)`` — POSIX-
   atomic replacement. Concurrent readers always see a consistent
   inode at ``user_path``; concurrent peer publishers racing on the
   same path produce a final state that points at one of their cas
   inputs, all byte-equivalent because their CAS keys collided.
3. On ``EXDEV``: ``symlink(cas_path, tmp)`` then ``rename(tmp,
   user_path)``. Same atomic-replacement pattern.
4. Any other ``OSError``: re-raise visibly (no silent symlink
   degradation).
5. Inode swap under a process holding ``user_path`` open is harmless
   on POSIX — the open file descriptor pins the old inode.

If the lock sidecar cannot be created at all (``EACCES``, ``EPERM``,
``EROFS``, ``ENOTSUP`` — a read-only or permission-denied pool), the
helper warns and publishes unlocked. That is safe against any trim
that hits the same failure: ``ct-trim-cache`` refuses to delete an
entry whose lock it cannot take, so nothing evicts from a pool nothing
can lock. ``EROFS`` and ``ENOTSUP`` are properties of the pool and
hold for every peer — ``ENOTSUP`` from ``lockf`` on a perfectly
writable directory is the case that shows the guarantee rests on
trim's refusal rather than on directory permissions. ``EACCES`` and
``EPERM`` can instead be specific to this uid: on a shared pool whose
directory another user can write and lock, that user's trim can still
evict mid-publish. A hardlinked
``user_path`` survives it — ``nlink`` pins the inode, so only the
cache name is lost — but a publish that fell back to ``symlink()``
under ``EXDEV`` can be left dangling. Any other lock error propagates
rather than being hidden behind a silently unlocked publish.

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
3
    The CAS entry was missing when the publish tried to link it. A
    concurrent ``ct-trim-cache`` eviction is the reachable cause, and it
    is recoverable: rerun the build and the artefact is relinked into
    the cache. A producer rule that exits 0 without writing its output
    lands here too, which a rerun will not fix, so the message names
    both. Distinct from ``1`` so a wrapper can retry this case without
    retrying real errors.

CONCURRENCY
===========

Idempotent on re-runs: the rename overwrites cleanly. Two parallel
build invocations targeting the same ``user_path`` race safely —
whichever rename wins is correct (both are publishing byte-equivalent
artefacts because their cas-exedir keys collided).

Publish and trim are serialised on the ``<cas-path>.lock`` sidecar.
Both sides take the same lock through ``locking.FileLock``, so both
get the filesystem-appropriate strategy (``fcntl`` on GPFS,
``lockdir`` on NFS and Lustre, and so on) and neither can be inside
its critical section while the other is. Under the lock the publisher
re-verifies the entry, freshens its mtime, and links; ``ct-trim-cache
--cas-exedir-only`` re-stats ``nlink`` and aborts the unlink when it
finds the entry has been published. Without the publish-side lock the
``link()`` can land between trim's ``nlink`` re-check and its
``remove()``, and the publish reports success on an entry that is
about to be deleted. That ``nlink`` re-stat only ever sees a hardlink
publish; a symlink-fallback publish is serialised by the lock like any
other but leaves nothing for a later trim to notice.

The lock nests below the caller's own: the Shake backend holds
``<user-path>.lock`` across the whole publish rule while this helper
takes ``<cas-path>.lock`` inside it. No code path takes those two in
the opposite order.

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
