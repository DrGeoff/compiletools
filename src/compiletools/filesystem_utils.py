"""Filesystem detection and compatibility utilities.

This module provides filesystem type detection and policy decisions for:
1. File locking strategies (makefile.py)
2. Memory mapping safety (file_analyzer.py)
3. Filesystem-specific performance tuning
"""

import contextlib
import errno
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path


def _umask_default_file_mode() -> int:
    """Return the file mode a normal ``open(path, 'w')`` would create now.

    ``tempfile.mkstemp`` deliberately bypasses umask and always creates
    0o600 as a security feature; callers that want the conventional
    umask-respecting mode have to recompute it. Reading umask requires a
    round-trip set-then-restore — there is no read-only API.
    """
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _resolve_target_mode_and_gid(target_path: str, preserve_permissions: bool) -> tuple[int, int | None]:
    """Return ``(mode, gid)`` for an atomic temp file destined to replace ``target_path``.

    When ``preserve_permissions`` and the target exists, copies its mode
    and group. Otherwise (and on any stat error) falls back to the
    umask-derived mode with no group change — needed because
    ``tempfile.mkstemp`` would otherwise leave the file at 0o600 forever
    once ``os.replace`` propagates it to the target.
    """
    if preserve_permissions and os.path.exists(target_path):
        try:
            stat_info = os.stat(target_path)
        except FileNotFoundError:
            pass  # Race: target deleted between exists() and stat(); treat as first-create.
        else:
            return stat_info.st_mode & 0o777, stat_info.st_gid
    return _umask_default_file_mode(), None


def atomic_replace(
    dst: str,
    populate: Callable[[str], None],
    *,
    tmp_prefix: str | None = None,
    tmp_suffix: str | None = None,
) -> None:
    """Atomically install at ``dst`` whatever ``populate`` puts at the
    temp path it is handed.

    The temp file is created in ``dst``'s directory (so the final
    ``os.replace`` is always within-fs, never ``EXDEV``), its placeholder
    is unlinked, and ``populate(tmp_path)`` is called to claim that
    path — typically via ``os.link``, ``os.symlink``, or by writing
    bytes. The result is then ``os.replace``'d onto ``dst``, atomic
    against peer readers and tolerant of a read-only existing target
    (``os.replace`` only needs write permission on the parent dir).

    Concurrent peers each ``mkstemp`` a unique tmp (process-local) and
    each ``os.replace`` independently; the last replace wins. Both
    final-states are valid provided ``populate`` produces byte-equivalent
    output for the same caller intent (the contract callers must uphold).

    ``tmp_prefix`` / ``tmp_suffix`` override the temp filename pattern
    for callers whose orphan-cleanup tooling scans for specific names
    (e.g. ``cas_publish`` looks for ``*.publish.tmp``).
    """
    dst_dir = os.path.dirname(dst) or "."
    if tmp_prefix is None:
        tmp_prefix = f".tmp.{os.path.basename(dst)}."
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dst_dir, prefix=tmp_prefix, suffix=tmp_suffix or "")
    os.close(tmp_fd)
    os.unlink(tmp_path)
    try:
        populate(tmp_path)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def atomic_copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` atomically; hardlink fast path.

    Tries ``os.link`` first (O(1) inode share on the same filesystem)
    and falls back to ``shutil.copy2`` on ``EXDEV``. Inode sharing on
    the fast path means a subsequent ``os.path.samefile(src, dst)``
    returns True, so idempotent callers can skip re-publishing on no-op
    reruns.

    .. warning::
       On the hardlink fast path, ``src`` and ``dst`` share an inode.
       Callers must not *in-place* modify either side — an in-place
       write would corrupt the other. All current callers write via
       temp+rename (atomic_replace, atomic_compile, etc.), which always
       produces a fresh inode for the write target, so the sharing is
       safe. New callers that ``open(dst, 'r+')`` or ``truncate(dst)``
       must use a different helper.
    """

    def populate(tmp_path: str) -> None:
        try:
            os.link(src, tmp_path)
        except AttributeError:
            # Platform lacks hardlink support entirely (e.g. Termux/Android
            # bionic doesn't expose os.link). Fall back to copy as for EXDEV.
            shutil.copy2(src, tmp_path)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            shutil.copy2(src, tmp_path)

    atomic_replace(dst, populate)


@lru_cache(maxsize=128)
def get_filesystem_type(path: str) -> str:
    """Detect filesystem type for given path.

    Returns: filesystem type string (e.g., 'ext4', 'gpfs', 'nfs', 'cifs')
             or 'unknown' if cannot be determined

    Caches results by resolved path for efficiency.
    """
    try:
        # Linux: Parse /proc/mounts
        path = os.path.realpath(path)
        mounts = []

        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mountpoint, fstype = parts[1], parts[2]
                    # Unescape octal sequences in mount paths (spaces, etc)
                    mountpoint = mountpoint.replace("\\040", " ")
                    mounts.append((mountpoint, fstype))

        # Sort by length descending to find most specific mount
        mounts.sort(key=lambda x: len(x[0]), reverse=True)

        # Find matching mount point
        path_obj = Path(path)
        for mountpoint, fstype in mounts:
            if path_obj.is_relative_to(mountpoint):
                return fstype

    except (FileNotFoundError, PermissionError, OSError):
        # /proc/mounts not available, try fallback
        pass

    # Fallback: try stat command (for non-Linux Unix)
    try:
        result = subprocess.run(["stat", "-f", "-c", "%T", path], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    return "unknown"


def get_lock_strategy(fstype: str) -> str:
    """Determine file locking strategy for filesystem type (for makefile.py).

    Returns:
        'fcntl' - Use fcntl.lockf() (GPFS: cross-node, kernel-managed)
        'lockdir' - Use mkdir-based locking (NFS/Lustre)
        'cifs' - Use exclusive file creation (CIFS/SMB specific)
        'flock' - Use POSIX flock (standard local filesystems)
    """
    fstype_lower = fstype.lower()

    # GPFS: fcntl.lockf() works cross-node (unlike flock which is node-local)
    if "gpfs" in fstype_lower:
        return "fcntl"

    # NFS/Lustre: mkdir-based locking
    if any(fs in fstype_lower for fs in ["lustre", "nfs"]):
        return "lockdir"

    # CIFS/SMB requires exclusive file creation
    if any(fs in fstype_lower for fs in ["cifs", "smb"]):
        return "cifs"

    # Standard POSIX flock
    return "flock"


# Filesystems whose per-file ``stat()`` is a high-latency metadata round-trip
# (cluster / network filesystems) and whose metadata servers service concurrent
# requests well. On these, fanning the trim scan's stat calls across threads is
# a large win — ``stat()`` releases the GIL, so threads overlap the network
# latency. Local-disk filesystems (ext4/xfs/btrfs/tmpfs/zfs/overlay) get no
# benefit (cheap stat served from the page cache) and pay thread-pool overhead,
# so they — and any *unknown* filesystem, which is almost always local disk —
# stay single-threaded.
_PARALLEL_SCAN_FILESYSTEMS: tuple[str, ...] = ("gpfs", "lustre", "nfs", "cifs", "smb", "panfs", "beegfs")


def should_parallelize_scan(fstype: str) -> bool:
    """Whether a metadata-bound directory scan (e.g. ct-trim-cache) should
    fan its ``stat()`` calls out across threads on this filesystem.

    True only for high-latency network/cluster filesystems (GPFS, Lustre,
    NFS, CIFS/SMB, PanFS, BeeGFS), where parallel ``stat()`` overlaps the
    metadata round-trip. False for local-disk and *unknown* filesystems —
    the latter are almost always local disk, so staying serial preserves the
    historical single-threaded behavior with no thread overhead.

    This is only the *gate*: the actual worker count is the caller's
    ``--parallel`` / ``-j`` value (``jobs.py``), which already honours CPU
    affinity, cgroups, and slurm allocations. This function never decides a
    thread count.
    """
    fstype_lower = fstype.lower()
    return any(fs in fstype_lower for fs in _PARALLEL_SCAN_FILESYSTEMS)


def supports_mmap_safely(fstype: str) -> bool:
    """Determine if filesystem supports mmap reliably (for file_analyzer.py).

    Returns:
        True if mmap is known to be safe on this filesystem
        False if mmap has known issues
    """
    fstype_lower = fstype.lower()

    # Known problematic filesystems
    unsafe_filesystems = ["gpfs", "cifs", "smb", "smbfs", "afs"]
    if any(fs in fstype_lower for fs in unsafe_filesystems):
        return False

    # NFS v4 usually works, but has had issues historically. FUSE varies by
    # implementation. Unknown or local filesystems are assumed safe.
    return True


def get_lockdir_sleep_interval(fstype: str) -> float:
    """Get recommended sleep interval for lockdir polling (for makefile.py).

    Returns:
        Sleep interval in seconds for lock acquisition retries
    """
    fstype_lower = fstype.lower()

    if "lustre" in fstype_lower:
        return 0.01  # Lustre is fast parallel filesystem
    elif "nfs" in fstype_lower:
        return 0.1  # NFS has network latency
    else:  # Other filesystems (GPFS uses fcntl, not lockdir polling)
        return 0.05  # Default middle ground


def atomic_write(target_path, content, binary=False, preserve_permissions=True):
    """Atomically write content to file via temp file + os.replace().

    Prevents SIGBUS for concurrent readers with memory-mapped files.
    On POSIX, os.replace() is atomic and existing mmaps continue to reference
    the old inode until they close/remap.

    Works correctly on all filesystem types (NFS, GPFS, Lustre, CIFS, local)
    as long as temp and target are on the same filesystem (ensured by creating
    temp in target's directory).

    Args:
        target_path: Final destination path
        content: Content to write (str or bytes)
        binary: If True, write as binary; if False, encode as UTF-8
        preserve_permissions: If True and target exists, preserve its mode/group

    Raises:
        OSError: If write or replace fails
    """
    import tempfile

    target_dir = os.path.dirname(target_path) or "."
    target_name = os.path.basename(target_path)

    # Ensure target directory exists
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    target_mode, target_gid = _resolve_target_mode_and_gid(target_path, preserve_permissions)

    # Create temp file in same directory (ensures same filesystem)
    fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=f".tmp.{target_name}.", suffix=f".{os.getpid()}")

    try:
        # Write content
        if binary:
            if isinstance(content, str):
                content = content.encode("utf-8")
            os.write(fd, content)
        else:
            if isinstance(content, bytes):
                os.write(fd, content)
            else:
                os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None

        os.chmod(temp_path, target_mode)
        if target_gid is not None:
            try:
                os.chown(temp_path, -1, target_gid)
            except PermissionError:
                pass  # Can't change group, not fatal (same as locking.py)

        # Atomic replace
        os.replace(temp_path, target_path)

    except Exception:
        # Clean up on error
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def atomic_write_if_changed(
    target_path: str,
    content: str | bytes,
    *,
    binary: bool = False,
    preserve_permissions: bool = True,
    encoding: str = "utf-8",
) -> bool:
    """Atomically write *content* to *target_path* iff it differs from on-disk.

    Returns True when a write happened, False when the target was byte-identical
    and was left untouched (preserving its inode and mtime — important for
    consumers like clangd / `find -newer` that treat any mtime change as cache
    invalidation).

    A missing target is treated as "different" and triggers the write.
    Read failures (corrupt file, permission glitch) fall through to the
    unconditional write so a transient error never leaves a stale file.
    """
    new_bytes = content.encode(encoding) if (not binary and isinstance(content, str)) else content
    try:
        with open(target_path, "rb") as f:
            if f.read() == new_bytes:
                return False
    except FileNotFoundError:
        pass
    except OSError:
        pass  # fall through to unconditional write
    atomic_write(target_path, content, binary=binary, preserve_permissions=preserve_permissions)
    return True


def safe_read_text_file(filepath, encoding="utf-8", force_no_mmap=False, respect_locks=False, lock_args=None):
    """Read text file safely, closing file descriptors properly.

    Always uses regular file I/O and closes file descriptors immediately.
    The OS page cache provides good performance for recently accessed files.

    Note: We use regular I/O instead of mmap because:
    1. We're reading entire files (not streaming), so mmap has no lazy-load benefit
    2. Keeping mmap fds open causes resource exhaustion in large builds
    3. OS page cache already provides excellent performance for re-reads
    4. Avoids SIGBUS issues on NFS/GPFS during concurrent writes

    Args:
        filepath: Path to file to read
        encoding: Text encoding
        force_no_mmap: Ignored (kept for API compatibility)
        respect_locks: If True, wait for write locks before reading
        lock_args: Lock configuration (required if respect_locks=True)

    Returns:
        sz.Str object with file contents in memory (no open file descriptor)

    Raises:
        OSError: If file cannot be read
        FileNotFoundError: If file doesn't exist
    """
    from stringzilla import Str

    # Optional lock barrier - wait for any active writers
    if respect_locks and lock_args:
        from compiletools.locking import FileLock

        with FileLock(filepath, lock_args):
            pass  # Lock released immediately, now safe to read

    # Regular file I/O - safe on all filesystems, closes fd via context manager
    # OS page cache handles performance optimization
    with open(filepath, encoding=encoding, errors="replace") as f:
        return Str(f.read())


def atomic_output_file(target_path, mode="w", encoding="utf-8", preserve_permissions=True, force_mode=None):
    """Context manager for atomic file writes via temp file + os.replace().

    Returns a file object that writes to a temp file. On successful exit,
    atomically replaces target with temp file. On error, removes temp file.

    Prevents SIGBUS for concurrent readers with memory-mapped files.

    Usage:
        with atomic_output_file('/path/to/file.txt') as f:
            f.write(content)

    Args:
        target_path: Final destination path
        mode: File mode ('w', 'wb', etc.)
        encoding: Text encoding (only used for text mode)
        preserve_permissions: If True and target exists, preserve its mode/group
        force_mode: Exact permission bits for the result, overriding both the
            umask and any preserved mode. Pass 0o666 for files in a shared
            CAS pool that every peer must be able to read — the
            first-creator's umask must not lock peers out (same rationale as
            locking.py's explicit fchmod). Group preservation still applies.

    Yields:
        File object for writing

    Raises:
        OSError: If write or replace fails
    """
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _atomic_context():
        target_dir = os.path.dirname(target_path) or "."
        target_name = os.path.basename(target_path)

        # Ensure target directory exists
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        target_mode, target_gid = _resolve_target_mode_and_gid(target_path, preserve_permissions)
        if force_mode is not None:
            target_mode = force_mode

        # Create temp file in same directory
        fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=f".tmp.{target_name}.", suffix=f".{os.getpid()}")

        f = None
        try:
            # Convert fd to file object with requested mode
            if "b" in mode:
                f = os.fdopen(fd, mode)
            else:
                f = os.fdopen(fd, mode, encoding=encoding)

            yield f

            f.close()
            f = None

            os.chmod(temp_path, target_mode)
            if target_gid is not None:
                try:
                    os.chown(temp_path, -1, target_gid)
                except PermissionError:
                    pass

            # Atomic replace
            os.replace(temp_path, target_path)

        except Exception:
            # Clean up on error
            if f is not None and not f.closed:
                try:
                    f.close()
                except OSError:
                    pass
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    return _atomic_context()
