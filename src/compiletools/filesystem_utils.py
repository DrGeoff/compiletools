"""Filesystem detection and compatibility utilities.

This module provides filesystem type detection and policy decisions for:
1. File locking strategies (makefile.py)
2. Memory mapping safety (file_analyzer.py)
3. Filesystem-specific performance tuning
"""

from functools import lru_cache
import os
from pathlib import Path


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

        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mountpoint, fstype = parts[1], parts[2]
                    # Unescape octal sequences in mount paths (spaces, etc)
                    mountpoint = mountpoint.replace('\\040', ' ')
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
        import subprocess
        result = subprocess.run(
            ['stat', '-f', '-c', '%T', path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    return 'unknown'


def get_lock_strategy(fstype: str) -> str:
    """Determine file locking strategy for filesystem type (for makefile.py).

    Returns:
        'lockdir' - Use mkdir-based locking (atomic on all filesystems)
        'cifs' - Use exclusive file creation (CIFS/SMB specific)
        'flock' - Use POSIX flock (standard local filesystems)
    """
    fstype_lower = fstype.lower()

    # Filesystems requiring lockdir approach
    if any(fs in fstype_lower for fs in ['gpfs', 'lustre', 'nfs']):
        return 'lockdir'

    # CIFS/SMB requires exclusive file creation
    if any(fs in fstype_lower for fs in ['cifs', 'smb']):
        return 'cifs'

    # Standard POSIX flock
    return 'flock'


def supports_mmap_safely(fstype: str) -> bool:
    """Determine if filesystem supports mmap reliably (for file_analyzer.py).

    Returns:
        True if mmap is known to be safe on this filesystem
        False if mmap has known issues
    """
    fstype_lower = fstype.lower()

    # Known problematic filesystems
    unsafe_filesystems = ['gpfs', 'cifs', 'smb', 'smbfs', 'afs']
    if any(fs in fstype_lower for fs in unsafe_filesystems):
        return False

    # Questionable filesystems - for now treat as safe but should log warning
    # NFS v4 usually works, but has had issues historically
    # FUSE varies by implementation
    # Unknown or local filesystems assumed safe
    return True


def get_lockdir_sleep_interval(fstype: str) -> float:
    """Get recommended sleep interval for lockdir polling (for makefile.py).

    Returns:
        Sleep interval in seconds for lock acquisition retries
    """
    fstype_lower = fstype.lower()

    if 'lustre' in fstype_lower:
        return 0.01  # Lustre is fast parallel filesystem
    elif 'nfs' in fstype_lower:
        return 0.1   # NFS has network latency
    else:  # GPFS and others
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

    target_dir = os.path.dirname(target_path) or '.'
    target_name = os.path.basename(target_path)

    # Ensure target directory exists
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    # Get target permissions if preserving and file exists
    target_mode = None
    target_gid = None
    if preserve_permissions and os.path.exists(target_path):
        try:
            stat_info = os.stat(target_path)
            target_mode = stat_info.st_mode & 0o777
            target_gid = stat_info.st_gid
        except OSError:
            pass

    # Create temp file in same directory (ensures same filesystem)
    fd, temp_path = tempfile.mkstemp(
        dir=target_dir,
        prefix=f'.tmp.{target_name}.',
        suffix=f'.{os.getpid()}'
    )

    try:
        # Write content
        if binary:
            if isinstance(content, str):
                content = content.encode('utf-8')
            os.write(fd, content)
        else:
            if isinstance(content, bytes):
                os.write(fd, content)
            else:
                os.write(fd, content.encode('utf-8'))
        os.close(fd)
        fd = None

        # Set permissions to match target
        if target_mode is not None:
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


def safe_read_text_file(filepath, encoding='utf-8', force_no_mmap=False, respect_locks=False, lock_args=None):
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
    with open(filepath, 'r', encoding=encoding, errors='replace') as f:
        return Str(f.read())


def atomic_output_file(target_path, mode='w', encoding='utf-8', preserve_permissions=True):
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

    Yields:
        File object for writing

    Raises:
        OSError: If write or replace fails
    """
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _atomic_context():
        target_dir = os.path.dirname(target_path) or '.'
        target_name = os.path.basename(target_path)

        # Ensure target directory exists
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        # Get target permissions if preserving and file exists
        target_mode = None
        target_gid = None
        if preserve_permissions and os.path.exists(target_path):
            try:
                stat_info = os.stat(target_path)
                target_mode = stat_info.st_mode & 0o777
                target_gid = stat_info.st_gid
            except OSError:
                pass

        # Create temp file in same directory
        fd, temp_path = tempfile.mkstemp(
            dir=target_dir,
            prefix=f'.tmp.{target_name}.',
            suffix=f'.{os.getpid()}'
        )

        f = None
        try:
            # Convert fd to file object with requested mode
            if 'b' in mode:
                f = os.fdopen(fd, mode)
            else:
                f = os.fdopen(fd, mode, encoding=encoding)

            yield f

            f.close()
            f = None

            # Set permissions to match target
            if target_mode is not None:
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
