"""Filesystem detection and compatibility utilities.

This module provides filesystem type detection and policy decisions for:
1. File locking strategies (makefile.py)
2. Memory mapping safety (file_analyzer.py)
3. Filesystem-specific performance tuning
"""

from functools import lru_cache
import os


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
                    device, mountpoint, fstype = parts[0], parts[1], parts[2]
                    # Unescape octal sequences in mount paths (spaces, etc)
                    mountpoint = mountpoint.replace('\\040', ' ')
                    mounts.append((mountpoint, fstype))

        # Sort by length descending to find most specific mount
        mounts.sort(key=lambda x: len(x[0]), reverse=True)

        # Find matching mount point
        for mountpoint, fstype in mounts:
            if path.startswith(mountpoint):
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
