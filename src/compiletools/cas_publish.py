"""Atomic publish of a CAS artefact at a stable user-facing path.

Replaces the prior shell recipe ``ln -f cas user 2>/dev/null || ln -sfn cas user``
which had two concrete problems:

* ``ln -f`` is not atomic — coreutils does ``unlink(target); link(source, target)``
  with a window where ``target`` does not exist (I1 in the CAS bug audit).
* ``2>/dev/null`` swallowed unrelated errors (``ENOSPC``, ``EPERM``, ``EACCES``,
  ``EROFS``, ``EMFILE``) and silently degraded to a symlink, which then breaks
  the trim_exedir hard-link-protection invariant by giving the cas entry
  ``nlink == 1`` (I2 in the audit).

The new contract:

1. ``link(cas_path, tmp)`` then ``rename(tmp, user_path)`` — POSIX-atomic
   replacement, the kernel guarantees ``user_path`` is always present (either
   the old inode or the new one) for any concurrent reader.
2. On ``EXDEV`` from step 1: fall back to ``symlink(cas_path, tmp)`` then
   ``rename(tmp, user_path)``. Same atomic-replacement pattern; just a symlink
   inode instead of a hardlink.
3. Any other ``OSError`` from step 1: re-raise visibly. Operators get a clear
   diagnostic instead of a silent symlink degradation.

Sidecar manifest at ``<cas_path>.manifest`` (C4): JSON of ``{"source_realpath": ...}``,
written best-effort after a successful link/rename. ``trim_cache`` reads it to
bucket entries by source identity instead of by basename — disambiguates
distinct executables that happen to share a basename like ``main``.

Steps 1-3 run while holding the ``<cas_path>.lock`` sidecar — the same lock
``trim_cache._safe_locked_unlink`` takes before evicting an entry, so publish
and trim are totally ordered on any given CAS entry. Trim going first leaves
nothing to publish, which surfaces as ``ConcurrentTrimError`` and exit code
``EXIT_CONCURRENT_TRIM`` rather than a raw ``FileNotFoundError``; publish going
first raises the entry's ``st_nlink`` to 2, which trim's hard-link protection
honours. Those are the only two outcomes.

This module is invoked from generated build recipes via the ``ct-cas-publish``
entry point. Keep flags minimal and the contract terse — every recipe gets
this command in its tail and a complex CLI surface would balloon the
generated build files.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sys

import compiletools.ct_lock_helper
import compiletools.filesystem_utils
import compiletools.locking

# Exit code for "the CAS entry was evicted by a concurrent trim". Distinct from
# 0 (published), 1 (generic failure) and 2 (argparse usage) so a build can tell
# the recoverable loss apart from a real error.
EXIT_CONCURRENT_TRIM = 3

# Lock-acquisition errnos that mean "this pool cannot host a lock sidecar at
# all" (read-only or permission-denied cas dir). Creating the sidecar and
# unlinking the entry both need write permission on the cas directory, so a
# trim running as this uid cannot evict what this publish cannot lock. That
# implication is per-uid: a directory some other user can write but this one
# cannot leaves their trim free to evict mid-publish, where a hardlinked
# user_path survives on the pinned inode and only the cache name is lost, but
# the EXDEV symlink fallback can be left dangling.
# Every other errno propagates — swallowing a transient EIO or ENOSPC into an
# unlocked publish would hide real trouble.
_LOCK_UNHOSTABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTSUP})


class ConcurrentTrimError(Exception):
    """The CAS entry vanished before it could be published.

    Raised when the under-lock existence re-check finds the entry gone, or
    when ``os.link`` reports ``ENOENT`` for it (the residual window on a pool
    where the lock could not be taken). Both mean the same recoverable thing:
    ``ct-trim-cache`` evicted the artefact, and rerunning the build rebuilds it.
    """

    def __init__(self, cas_path: str):
        super().__init__(f"CAS entry removed by a concurrent trim; rerun the build: {cas_path}")
        self.cas_path = cas_path


@contextlib.contextmanager
def _cas_entry_lock(cas_path: str):
    """Hold the ``<cas_path>.lock`` sidecar for the body.

    Locks the sidecar, never the entry itself: creating the entry path to lock
    it would hand a peer ``make`` an empty, mtime=now artefact (the first
    locking invariant in ``src/compiletools/CLAUDE.md``). Strategy selection
    (fcntl on GPFS, lockdir on NFS/Lustre, ...) is ``FileLock``'s job, so
    publish and trim always agree on the primitive. Lock policy comes from the
    ``CT_LOCK_*`` environment, matching every other lock a build recipe takes.
    """
    with contextlib.ExitStack() as stack:
        try:
            lock_args = compiletools.ct_lock_helper.create_args_from_env()
            stack.enter_context(compiletools.locking.FileLock(cas_path, lock_args))
        except OSError as e:
            if e.errno not in _LOCK_UNHOSTABLE_ERRNOS:
                raise
            print(
                f"Warning: publishing {cas_path} without its entry lock ({e}); this pool cannot host a lock sidecar",
                file=sys.stderr,
            )
        yield


def _freshen_cas_entry(cas_path: str) -> None:
    """Mark the entry as actively used by setting its mtime to now.

    Age-gated sweeps (``ct-trim-cache --max-age`` and the oldest-first
    ``--max-size`` budget) rank by mtime, and a CAS entry keeps the mtime it
    was created with however many builds go on publishing it. Without this an
    entry that every build still links against ages out and gets rebuilt.

    Best-effort: on a shared pool the entry usually belongs to another user and
    ``utime`` is denied, which is not a reason to fail the publish.
    """
    with contextlib.suppress(OSError):
        os.utime(cas_path, None)


def publish(cas_path: str, user_path: str, source_realpath: str | None = None) -> None:
    """Atomically publish ``cas_path`` at ``user_path``; write sidecar manifest.

    Idempotent on re-runs (the rename overwrites). Concurrent peer publishers
    racing on the same ``user_path`` produce a final state that points at one
    of their cas inputs — both are byte-equivalent when their CAS keys collide,
    so any winner is correct. Inode swap under a process holding ``user_path``
    open is harmless on POSIX (the open file descriptor pins the old inode).

    Hardlink first (preserves the cas-exedir trim hard-link-protection
    invariant by giving the cas entry ``nlink >= 2``); on ``EXDEV`` fall
    back to a symlink so cross-filesystem publishes still work. Any other
    OSError surfaces visibly instead of silently degrading.

    Sidecar errors are non-fatal: a missing/corrupt manifest just falls back
    to legacy basename bucketing in trim_exedir. The publish itself failing
    IS fatal — surface it.

    Raises ``ConcurrentTrimError`` when the entry was evicted by a concurrent
    trim; every other failure surfaces as its own OSError.
    """
    os.makedirs(os.path.dirname(user_path) or ".", exist_ok=True)

    def populate(tmp_path):
        try:
            os.link(cas_path, tmp_path)
        except AttributeError:
            # Platform lacks hardlink support entirely (e.g. Termux/Android
            # bionic doesn't expose os.link). Degrade to symlink — the
            # trim_exedir hard-link-protection invariant (nlink >= 2) is
            # unattainable here, same as the EXDEV branch below.
            os.symlink(cas_path, tmp_path)
        except FileNotFoundError:
            # Only the cas entry going missing is a trim loss; an ENOENT from
            # anything else (a vanished destination dir) keeps its own identity.
            if os.path.exists(cas_path):
                raise
            raise ConcurrentTrimError(cas_path) from None
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            os.symlink(cas_path, tmp_path)

    with _cas_entry_lock(cas_path):
        if not os.path.exists(cas_path):
            raise ConcurrentTrimError(cas_path)
        _freshen_cas_entry(cas_path)

        compiletools.filesystem_utils.atomic_replace(
            user_path,
            populate,
            tmp_prefix=os.path.basename(user_path) + ".",
            tmp_suffix=".publish.tmp",
        )

        # Sidecar manifest: best-effort. Written after the publish so a
        # publish-failed entry doesn't mislead trim_exedir into thinking it
        # has a known source identity, and under the lock so it cannot be
        # stranded beside an entry a trim is concurrently evicting.
        if source_realpath:
            manifest_path = cas_path + ".manifest"
            try:
                with open(manifest_path, "w") as f:
                    json.dump({"source_realpath": source_realpath}, f)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    from compiletools.version import __version__

    parser = argparse.ArgumentParser(
        prog="ct-cas-publish",
        description="Atomically publish a CAS artefact at a stable user-facing path.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--cas-path", required=True, help="Source path inside the CAS.")
    parser.add_argument("--user-path", required=True, help="Destination user-facing path.")
    parser.add_argument(
        "--source-realpath",
        default=None,
        help="Source file realpath; written into <cas-path>.manifest sidecar for trim bucketing.",
    )
    args = parser.parse_args(argv)
    try:
        publish(args.cas_path, args.user_path, args.source_realpath)
    except ConcurrentTrimError as e:
        print(f"ct-cas-publish: {e}", file=sys.stderr)
        return EXIT_CONCURRENT_TRIM
    return 0


if __name__ == "__main__":
    sys.exit(main())
