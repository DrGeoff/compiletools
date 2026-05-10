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

This module is invoked from generated build recipes via the ``ct-cas-publish``
entry point. Keep flags minimal and the contract terse — every recipe gets
this command in its tail and a complex CLI surface would balloon the
generated build files.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import tempfile


def publish(cas_path: str, user_path: str, source_realpath: str | None = None) -> None:
    """Atomically publish ``cas_path`` at ``user_path``; write sidecar manifest.

    Idempotent on re-runs (the rename overwrites). Concurrent peer publishers
    racing on the same ``user_path`` produce a final state that points at one
    of their cas inputs — both are byte-equivalent when their CAS keys collide,
    so any winner is correct. Inode swap under a process holding ``user_path``
    open is harmless on POSIX (the open file descriptor pins the old inode).

    Sidecar errors are non-fatal: a missing/corrupt manifest just falls back
    to legacy basename bucketing in trim_exedir. The publish itself failing
    IS fatal — surface it.
    """
    user_dir = os.path.dirname(user_path) or "."
    os.makedirs(user_dir, exist_ok=True)

    # Per-process tmp name in the destination directory so the rename is
    # always within-fs (no EXDEV at the rename step).
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(user_path) + ".",
        suffix=".publish.tmp",
        dir=user_dir,
    )
    # We need the path, not the fd — close the descriptor and unlink the
    # placeholder so link() can target the same name.
    os.close(tmp_fd)
    os.unlink(tmp_path)

    try:
        try:
            os.link(cas_path, tmp_path)
        except OSError as e:
            if e.errno == errno.EXDEV:
                os.symlink(cas_path, tmp_path)
            else:
                raise
        # POSIX-atomic replacement — concurrent readers see either the
        # previous inode or the new one, never a missing path.
        os.replace(tmp_path, user_path)
    except BaseException:
        # Best-effort cleanup of the tmp on any failure path so we don't
        # litter user_dir with stale .publish.tmp files.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Sidecar manifest: best-effort. Written after the publish so a
    # publish-failed entry doesn't mislead trim_exedir into thinking it
    # has a known source identity.
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
    publish(args.cas_path, args.user_path, args.source_realpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
