"""Shared utilities for file locking and lock management.

This module contains pure utility functions used by both:
- locking.py (for lock acquisition/release)
- cleanup_locks.py (for stale lock cleanup)

No circular dependencies: only imports external libraries (psutil, subprocess, os, time)
"""

import os
import sys
import time
import subprocess


def get_lock_age_seconds(lockdir, verbose=0):
    """Calculate lock age from mtime.

    Args:
        lockdir: Path to lockdir to check
        verbose: Verbosity level for debug output

    Returns:
        float: Age in seconds, or 0 if doesn't exist or has future mtime
    """
    try:
        # CRITICAL: Use os.path.getmtime, NOT cached version
        # Lock can be removed/recreated, caching would return stale mtime
        lock_mtime = os.path.getmtime(lockdir)
        now = time.time()

        # Handle future mtime (clock skew between hosts)
        if lock_mtime > now:
            if verbose >= 2:
                print(
                    f"Warning: Lock mtime in future (clock skew): {lockdir}",
                    file=sys.stderr,
                )
            return 0

        return now - lock_mtime
    except OSError:
        return 0


def read_lock_info(lockdir):
    """Read hostname:pid from lock pid file.

    Args:
        lockdir: Path to lockdir

    Returns:
        tuple: (hostname, pid) or (None, None) if unreadable
    """
    pid_file = os.path.join(lockdir, "pid")

    try:
        if not os.path.exists(pid_file):
            return None, None

        with open(pid_file, 'r') as f:
            lock_info = f.read().strip()

        if ':' not in lock_info:
            return None, None

        lock_host, lock_pid = lock_info.split(':', 1)
        return lock_host, int(lock_pid)

    except (OSError, ValueError):
        return None, None


def is_process_alive_local(pid):
    """Check if process exists on local host.

    Args:
        pid: Process ID to check

    Returns:
        bool: True if process exists
    """
    import psutil
    return psutil.pid_exists(pid)


def is_process_alive_remote(hostname, pid, ssh_timeout=5):
    """Check if process exists on remote host via SSH.

    Args:
        hostname: Remote hostname
        pid: Process ID to check
        ssh_timeout: SSH connection timeout in seconds

    Returns:
        tuple: (is_alive: bool, ssh_error: bool)
            is_alive: True if process is running
            ssh_error: True if SSH connection failed (unknown status)
    """
    try:
        result = subprocess.run(
            ['ssh', '-o', f'ConnectTimeout={ssh_timeout}',
             '-o', 'BatchMode=yes', hostname,
             f'kill -0 {pid} 2>/dev/null'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ssh_timeout + 1
        )

        # Exit code 0 = process exists
        # Exit code 1 = process doesn't exist
        # Exit code 255 = SSH connection failed
        if result.returncode == 255:
            return False, True  # SSH error
        return result.returncode == 0, False

    except (subprocess.TimeoutExpired, OSError):
        return False, True  # Treat timeout/error as SSH failure
