"""Cache trimming utility for shared object and PCH caches.

Scans shared-objdir and shared-pchdir for stale entries and removes them,
keeping entries that match the current git state and preserving a configurable
number of recent non-current entries per source file.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time

# Object filename format: {basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o
# Greedy first group handles underscores in basenames.
_OBJ_FILENAME_RE = re.compile(
    r"^(.+)_([0-9a-f]{12})_([0-9a-f]{14})_([0-9a-f]{16})\.o$"
)

# PCH command hash directories are exactly 16 lowercase hex chars.
_PCH_COMMAND_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


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
                groups.setdefault(basename, []).append(
                    (entry.path, file_hash, st.st_mtime, st.st_size)
                )

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

            # Safety: always keep at least 1 file per basename total
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
                    try:
                        os.remove(path)
                        stats["removed"] += 1
                        stats["bytes_freed"] += size
                    except OSError as exc:
                        stats["failed"] += 1
                        if self.verbose >= 1:
                            print(f"  Failed to remove {path}: {exc}", file=sys.stderr)
                else:
                    stats["removed"] += 1
                    stats["bytes_freed"] += size

        return stats

    # ------------------------------------------------------------------
    # PCH directory trimming
    # ------------------------------------------------------------------

    def trim_pchdir(self, pchdir):
        """Trim stale precompiled header directories from a shared PCH cache.

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
        # header_dirs: {header_basename: [command_hash, ...]}
        header_dirs = {}

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
            for h in headers:
                header_dirs.setdefault(h, []).append(entry.name)

        stats["headers_found"] = len(header_dirs)
        now = time.time()

        # Phase 2: decide which directories to keep
        needed_dirs = set()
        for _header, cmd_hashes in header_dirs.items():
            # Sort by mtime descending (newest first)
            cmd_hashes_sorted = sorted(
                cmd_hashes, key=lambda ch: dir_info[ch][1], reverse=True
            )
            # Keep newest keep_count
            for ch in cmd_hashes_sorted[: self.keep_count]:
                needed_dirs.add(ch)

            # If max_age set, also keep dirs within age limit (beyond keep_count)
            if self.max_age_seconds is not None:
                cutoff = now - self.max_age_seconds
                for ch in cmd_hashes_sorted[self.keep_count :]:
                    if dir_info[ch][1] >= cutoff:
                        needed_dirs.add(ch)

        # Phase 3: remove directories not needed by any header
        for cmd_hash, (path, _mtime, total_size, _headers) in dir_info.items():
            if cmd_hash in needed_dirs:
                stats["dirs_kept"] += 1
                continue

            if self.verbose >= 1:
                action = "Would remove" if self.dry_run else "Removing"
                print(f"  {action}: {path} ({_format_size(total_size)})")

            if not self.dry_run:
                try:
                    shutil.rmtree(path)
                    stats["dirs_removed"] += 1
                    stats["bytes_freed"] += total_size
                except OSError as exc:
                    stats["failed"] += 1
                    if self.verbose >= 1:
                        print(f"  Failed to remove {path}: {exc}", file=sys.stderr)
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
