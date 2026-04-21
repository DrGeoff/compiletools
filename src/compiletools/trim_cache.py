"""Cache trimming utility for shared object and PCH caches.

Scans shared-objdir and shared-pchdir for stale entries and removes them,
keeping entries that match the current git state and preserving a configurable
number of recent non-current entries per source file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time

# Object filename format: {basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o
# Anchored from the END (the three hash fields have fixed widths) so the
# basename can contain anything — including embedded substrings that look
# like our hash fields. Per-trailing-component matching avoids the regex
# backtracking quirks of a greedy ``(.+)`` first group (M-B1).
_OBJ_FILENAME_RE = re.compile(
    r"^(?P<basename>.+)_(?P<file>[0-9a-f]{12})_(?P<dep>[0-9a-f]{14})_(?P<macro>[0-9a-f]{16})\.o$"
)

# PCH command hash directories are exactly 16 lowercase hex chars.
_PCH_COMMAND_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def _load_pch_manifest(cmd_hash_dir: str) -> dict | None:
    """Read a PCH cmd_hash dir's sidecar manifest.

    Returns ``None`` for legacy entries (no manifest) or unreadable / corrupt
    files. Callers must treat ``None`` as "fall back to legacy behavior" so
    the trim path keeps working during the manifest-rollout window.
    """
    path = os.path.join(cmd_hash_dir, "manifest.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def parse_object_filename(filename):
    """Parse a content-addressable object filename into its components.

    Args:
        filename: Object filename (not path), e.g.
            ``my_module_aabbccddeeff_11223344556677_0011223344556677.o``

    Returns:
        Tuple of (basename, file_hash_12, dep_hash_14, macro_state_hash_16)
        or None if the filename does not match the expected format.
    """
    m = _OBJ_FILENAME_RE.match(filename)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return None


def build_current_hash_set(context):
    """Build the set of 12-char file hash prefixes for all git-tracked files.

    The 12-char (48-bit) prefix is deliberately loose: the failure mode
    of a collision is over-retention (treating a non-current entry as
    current and keeping it), never under-deletion. With ~80k tracked
    files the birthday probability of a collision is non-trivial but
    benign (M-B3).

    Args:
        context: BuildContext with loaded file hashes.

    Returns:
        set of 12-character hex strings.
    """
    from compiletools.global_hash_registry import get_tracked_files

    tracked = get_tracked_files(context)
    return {sha[:12] for sha in tracked.values()}


class CacheTrimmer:
    """Trims stale entries from shared-objdir and shared-pchdir caches."""

    def __init__(self, args):
        self.dry_run = getattr(args, "dry_run", False)
        self.verbose = getattr(args, "verbose", 1)
        self.keep_count = getattr(args, "keep_count", 1)
        max_age_days = getattr(args, "max_age", None)
        self.max_age_seconds = max_age_days * 86400 if max_age_days is not None else None

    # ------------------------------------------------------------------
    # Object directory trimming
    # ------------------------------------------------------------------

    def trim_objdir(self, objdir, current_hashes):
        """Trim stale object files from a shared object directory.

        Note on ``max_age``: "aged" means "old since written" (mtime), NOT
        "old since last accessed" (atime). A heavily-used cache entry from
        months ago will still be evicted because we cannot rely on atime
        (most production filesystems mount with ``noatime``).

        Args:
            objdir: Path to the shared object directory.
            current_hashes: Set of 12-char hex strings representing current
                file hashes (from build_current_hash_set).

        Returns:
            dict with statistics: total_scanned, basenames_found,
            current_kept, noncurrent_kept, removed, failed, bytes_freed.
        """
        stats = {
            "total_scanned": 0,
            "basenames_found": 0,
            "current_kept": 0,
            "noncurrent_kept": 0,
            "removed": 0,
            "failed": 0,
            "bytes_freed": 0,
        }

        if not os.path.isdir(objdir):
            if self.verbose >= 1:
                print(f"Object directory does not exist: {objdir}")
            return stats

        # Phase 1: scan and parse
        groups = {}  # basename -> list of (path, file_hash, mtime, size)
        try:
            entries = os.scandir(objdir)
        except OSError as exc:
            print(f"Error scanning {objdir}: {exc}", file=sys.stderr)
            return stats

        with entries:
            for entry in entries:
                if entry.name.endswith(".lockdir"):
                    continue
                if not entry.name.endswith(".o"):
                    continue
                parsed = parse_object_filename(entry.name)
                if parsed is None:
                    continue
                stats["total_scanned"] += 1
                basename, file_hash, _dep_hash, _macro_hash = parsed
                try:
                    st = entry.stat()
                except OSError:
                    continue
                groups.setdefault(basename, []).append((entry.path, file_hash, st.st_mtime, st.st_size))

        stats["basenames_found"] = len(groups)
        now = time.time()

        # Phase 2: decide per basename
        for _basename, files in groups.items():
            current = [(p, fh, mt, sz) for p, fh, mt, sz in files if fh in current_hashes]
            noncurrent = [(p, fh, mt, sz) for p, fh, mt, sz in files if fh not in current_hashes]

            stats["current_kept"] += len(current)

            # Sort non-current by mtime descending (newest first)
            noncurrent.sort(key=lambda x: x[2], reverse=True)

            to_keep = noncurrent[: self.keep_count]
            candidates = noncurrent[self.keep_count :]

            # Safety: always keep at least 1 file per basename total.
            # Only fires when keep_count=0 AND no current entry exists; if
            # the basename has zero non-current entries it stays absent (no
            # file to retain — nothing is silently lost, there is nothing
            # to keep).
            if not current and not to_keep and candidates:
                to_keep.append(candidates.pop(0))

            # Apply max_age filter: only remove candidates older than max_age
            if self.max_age_seconds is not None:
                cutoff = now - self.max_age_seconds
                to_remove = [f for f in candidates if f[2] < cutoff]
            else:
                to_remove = candidates

            stats["noncurrent_kept"] += len(to_keep) + (len(candidates) - len(to_remove))

            for path, _fh, _mt, size in to_remove:
                if self.verbose >= 1:
                    action = "Would remove" if self.dry_run else "Removing"
                    print(f"  {action}: {path} ({_format_size(size)})")
                if not self.dry_run:
                    if _safe_locked_unlink(path):
                        stats["removed"] += 1
                        stats["bytes_freed"] += size
                    else:
                        stats["failed"] += 1
                        if self.verbose >= 1:
                            print(f"  Failed to remove {path}", file=sys.stderr)
                else:
                    stats["removed"] += 1
                    stats["bytes_freed"] += size

        return stats

    # ------------------------------------------------------------------
    # PCH directory trimming
    # ------------------------------------------------------------------

    def trim_pchdir(self, pchdir):
        """Trim stale precompiled header directories from a shared PCH cache.

        Each ``<pchdir>/<cmd_hash>/`` directory is one unique compile
        configuration (compiler + flags + header realpath). The trim
        policy treats each cmd_hash dir as an independent unit:

        * Sort all cmd_hash dirs by mtime, newest first.
        * Keep the newest ``keep_count`` overall.
        * If ``max_age_seconds`` is set, also keep anything younger
          than that even beyond ``keep_count``.

        Bucketing-by-header-basename was tried in v8.0.2 but caused
        cache thrash (I-B5): two unrelated projects both using
        ``stdafx.h`` evicted each other at the default ``keep_count=1``.
        cmd_hash dirs are content-addressable, so per-dir bucketing is
        the correct partitioning. Per-realpath bucketing would be
        ideal (group cross-variant builds of the same header together)
        but the realpath is not stored on disk; see NOTES.md for the
        deferred sidecar-manifest follow-up.

        Note on ``max_age``: "aged" means "old since written" (the
        cmd_hash dir's mtime), NOT "old since last accessed". A
        heavily-used PCH cmd_hash dir from months ago is still evicted
        if it falls outside ``keep_count`` and ``max_age``. atime is
        unreliable on noatime-mounted filesystems.

        Note on cache-key composition: the cmd_hash captures the
        immediate header's realpath but NOT the content of headers it
        transitively includes. GCC's PCH stamp is the backstop — if a
        transitive header changes, the .gch is silently rejected at
        consume time and the user pays a slow rebuild. TODO(M-B6):
        write a sidecar manifest with transitive-header content hashes
        so the trim path can pre-evict known-stale entries.

        Args:
            pchdir: Path to the shared PCH directory.

        Returns:
            dict with statistics: total_dirs_scanned, headers_found,
            dirs_kept, dirs_removed, failed, bytes_freed.
        """
        stats = {
            "total_dirs_scanned": 0,
            "headers_found": 0,
            "dirs_kept": 0,
            "dirs_removed": 0,
            "failed": 0,
            "bytes_freed": 0,
        }

        if not os.path.isdir(pchdir):
            if self.verbose >= 1:
                print(f"PCH directory does not exist: {pchdir}")
            return stats

        # Phase 1: scan command_hash directories
        # dir_info: {command_hash: (path, mtime, total_size, [header_basenames])}
        dir_info = {}
        unique_headers: set[str] = set()

        try:
            entries = list(os.scandir(pchdir))
        except OSError as exc:
            print(f"Error scanning {pchdir}: {exc}", file=sys.stderr)
            return stats

        for entry in entries:
            if not entry.is_dir():
                continue
            if not _PCH_COMMAND_HASH_RE.match(entry.name):
                continue
            stats["total_dirs_scanned"] += 1

            headers = []
            total_size = 0
            try:
                for gch_entry in os.scandir(entry.path):
                    if gch_entry.name.endswith(".gch") and gch_entry.is_file():
                        header_base = gch_entry.name[:-4]  # strip .gch
                        headers.append(header_base)
                        try:
                            total_size += gch_entry.stat().st_size
                        except OSError:
                            pass
            except OSError:
                continue

            if not headers:
                continue

            try:
                dir_mtime = entry.stat().st_mtime
            except OSError:
                continue

            dir_info[entry.name] = (entry.path, dir_mtime, total_size, headers)
            unique_headers.update(headers)

        stats["headers_found"] = len(unique_headers)
        now = time.time()

        # Phase 2: rank all cmd_hash dirs globally by mtime; keep the newest
        # ``keep_count`` plus anything within ``max_age_seconds``.
        all_dirs_sorted = sorted(dir_info.keys(), key=lambda ch: dir_info[ch][1], reverse=True)
        needed_dirs = set(all_dirs_sorted[: self.keep_count])
        if self.max_age_seconds is not None:
            cutoff = now - self.max_age_seconds
            for ch in all_dirs_sorted[self.keep_count :]:
                if dir_info[ch][1] >= cutoff:
                    needed_dirs.add(ch)

        # Phase 3: remove directories not needed
        for cmd_hash, (path, _mtime, total_size, _headers) in dir_info.items():
            if cmd_hash in needed_dirs:
                stats["dirs_kept"] += 1
                continue

            if self.verbose >= 1:
                action = "Would remove" if self.dry_run else "Removing"
                print(f"  {action}: {path} ({_format_size(total_size)})")

            if not self.dry_run:
                # I-B4: lock each .gch file before removing the cmd_hash
                # dir. If a build is currently generating one of the .gch
                # files, we block until it releases — never deleting a
                # file a peer is mid-write to. Best-effort: filesystems
                # that don't support our lock strategies fall through to
                # plain rmtree (the lock acquisition is wrapped to ignore
                # errors so we don't fail trims on unlocked filesystems).
                if _safe_locked_rmtree(path):
                    stats["dirs_removed"] += 1
                    stats["bytes_freed"] += total_size
                else:
                    stats["failed"] += 1
                    if self.verbose >= 1:
                        print(f"  Failed to remove {path}", file=sys.stderr)
            else:
                stats["dirs_removed"] += 1
                stats["bytes_freed"] += total_size

        return stats

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def print_summary(self, objdir_stats=None, pchdir_stats=None):
        """Print a formatted summary of trimming results."""
        total_freed = 0
        print()
        print("=" * 60)
        print("Cache trim complete")

        if objdir_stats is not None:
            total_freed += objdir_stats["bytes_freed"]
            print("  Object files:")
            print(f"    Total scanned:   {objdir_stats['total_scanned']}")
            print(f"    Basenames found: {objdir_stats['basenames_found']}")
            print(f"    Current (kept):  {objdir_stats['current_kept']}")
            print(f"    Non-current kept:{objdir_stats['noncurrent_kept']}")
            removed_str = f"    Removed:         {objdir_stats['removed']}"
            if objdir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(objdir_stats['bytes_freed'])} freed)"
            print(removed_str)
            if objdir_stats["failed"]:
                print(f"    Failed:          {objdir_stats['failed']}")

        if pchdir_stats is not None:
            total_freed += pchdir_stats["bytes_freed"]
            print("  PCH directories:")
            print(f"    Total scanned:   {pchdir_stats['total_dirs_scanned']}")
            print(f"    Headers found:   {pchdir_stats['headers_found']}")
            print(f"    Kept:            {pchdir_stats['dirs_kept']}")
            removed_str = f"    Removed:         {pchdir_stats['dirs_removed']}"
            if pchdir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(pchdir_stats['bytes_freed'])} freed)"
            print(removed_str)
            if pchdir_stats["failed"]:
                print(f"    Failed:          {pchdir_stats['failed']}")

        if objdir_stats is not None and pchdir_stats is not None:
            print(f"  Total space freed: {_format_size(total_freed)}")
        print("=" * 60)


def _safe_locked_unlink(path):
    """Unlink path after acquiring the build lock for it.

    Used by ct-trim-cache to avoid deleting an .o file that a concurrent
    build is currently writing. Returns True on success, False on failure.

    If the lock subsystem cannot acquire a lock (filesystem unsupported,
    permissions, etc.), this function REFUSES to delete the file —
    deleting unlocked would defeat the entire purpose of the wrapper
    and could clobber an in-flight write from a peer build. The caller
    sees False and reports the file as failed; a future trim run can
    retry once the underlying lock issue is resolved.
    """
    from types import SimpleNamespace

    from compiletools.locking import FileLock

    lock_args = SimpleNamespace(
        file_locking=True,
        verbose=0,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.1,
        sleep_interval_flock_fallback=0.1,
    )
    try:
        with FileLock(path, lock_args):
            try:
                os.remove(path)
                return True
            except FileNotFoundError:
                return True  # peer already removed
            except OSError:
                return False
    except OSError as exc:
        # Lock acquisition failed — refuse to delete unlocked. Deleting
        # without a lock could clobber a peer build that is mid-write.
        print(
            f"  Refusing to remove {path}: lock unavailable ({exc})",
            file=sys.stderr,
        )
        return False


def _safe_locked_rmtree(dir_path):
    """Remove dir_path after acquiring a build lock on each contained file.

    Used by ct-trim-cache to avoid deleting files that a concurrent build
    is currently writing. Returns True on success, False on failure.

    Lock-unavailable safety: if any file's lock cannot be acquired, this
    function REFUSES to remove the directory and returns False. Deleting
    unlocked would defeat the wrapper's purpose and risk clobbering a
    peer build's in-flight write.

    TOCTOU safety: after locks are acquired, the directory is re-scanned.
    If any new file appeared between the initial scan and lock window
    (a peer build creating a fresh .gch), we abort the removal so the
    new file is not deleted unlocked. Caller sees False and the dir is
    naturally retried on the next trim pass.
    """
    from types import SimpleNamespace

    lock_args = SimpleNamespace(
        file_locking=True,
        verbose=0,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.1,
        sleep_interval_flock_fallback=0.1,
    )

    files_to_lock = []
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file():
                files_to_lock.append(entry.path)
    except OSError:
        # Dir vanished — nothing to do
        return True

    locks = []
    try:
        from compiletools.locking import FileLock

        for path in files_to_lock:
            try:
                fl = FileLock(path, lock_args)
                fl.__enter__()
                locks.append(fl)
            except OSError as exc:
                # Lock acquisition failed — refuse to delete unlocked.
                # Deleting without a lock could clobber a peer build
                # that is mid-write to one of these files.
                print(
                    f"  Refusing to remove {dir_path}: lock unavailable for {path} ({exc})",
                    file=sys.stderr,
                )
                return False

        # Re-scan inside the lock window to catch files that appeared
        # between the initial scan and lock acquisition. A peer build
        # creating a fresh .gch in this dir is unlocked from our side,
        # so removing it would be unsafe — abort instead.
        try:
            current_files = {entry.path for entry in os.scandir(dir_path) if entry.is_file()}
        except OSError:
            # Dir vanished between scan and re-scan; nothing more to do
            return True

        locked_set = set(files_to_lock)
        new_files = current_files - locked_set
        if new_files:
            print(
                f"  Refusing to remove {dir_path}: {len(new_files)} new file(s) "
                f"appeared after scan (peer build active)",
                file=sys.stderr,
            )
            return False

        try:
            shutil.rmtree(dir_path)
            return True
        except OSError:
            return False
    finally:
        for fl in locks:
            try:
                fl.__exit__(None, None, None)
            except OSError:
                pass


def _format_size(size_bytes):
    """Format a byte count as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
