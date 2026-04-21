"""Shared utilities for file locking and lock management.

This module contains pure utility functions used by both:
- locking.py (for lock acquisition/release)
- cleanup_locks.py (for stale lock cleanup)

No circular dependencies: only imports external libraries (psutil, subprocess, os, time)
"""

import os
import subprocess
import sys
import time


def ensure_parent_dir(path: str) -> None:
    """Create parent directory of path if it does not exist."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


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
    """Read hostname:pid (and optionally :start_time) from a lock pid file.

    The pid-file format added in v8.0.3 includes the lock holder's process
    start_time as a third colon-separated field. Older two-field files
    (host:pid) are still recognised; start_time is then None.

    Args:
        lockdir: Path to lockdir

    Returns:
        tuple: (hostname, pid, start_time) or (None, None, None) if
            unreadable. start_time is a float (psutil.Process.create_time())
            or None when the file uses the legacy two-field format.
    """
    pid_file = os.path.join(lockdir, "pid")

    try:
        if not os.path.exists(pid_file):
            return None, None, None

        with open(pid_file) as f:
            lock_info = f.read().strip()

        if ":" not in lock_info:
            return None, None, None

        parts = lock_info.split(":", 2)
        if len(parts) == 2:
            lock_host, lock_pid = parts
            return lock_host, int(lock_pid), None
        lock_host, lock_pid, start_time_str = parts
        try:
            start_time = float(start_time_str)
        except ValueError:
            start_time = None
        return lock_host, int(lock_pid), start_time

    except (OSError, ValueError):
        return None, None, None


# PID-reuse start_time tolerance.
#
# Linux: psutil reads /proc/[pid]/stat starttime (clock-tick resolution,
# typically 10ms). 0.1s is comfortably above that and tight enough to
# catch a PID reused within a second.
#
# macOS / *BSD: psutil falls back to coarser kernel APIs that may round
# create_time at second resolution, so we keep 1.0s there to avoid false
# negatives that would mark an alive holder as stale.
_PID_REUSE_TOLERANCE_SECONDS = 0.1 if sys.platform.startswith("linux") else 1.0


def is_process_alive_local(pid, start_time=None):
    """Check if a local process exists, optionally verifying it is the
    *same* process that recorded the lock (not a PID-reused successor).

    Args:
        pid: Process ID to check.
        start_time: Optional psutil-style create_time of the recorded
            holder. When provided, returns False unless the live process
            with this pid has a matching create_time. Tolerance is
            platform-dependent — see ``_PID_REUSE_TOLERANCE_SECONDS``.
            None means legacy file with no recorded start_time; fall
            back to pid-existence only.

    Returns:
        bool: True if the live process is (probably) the original
            lock holder. False if the pid does not exist, or if it
            does but its start_time does not match the recorded one.
    """
    import psutil

    if start_time is None:
        return psutil.pid_exists(pid)

    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    try:
        actual_start = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return abs(actual_start - start_time) < _PID_REUSE_TOLERANCE_SECONDS


def get_process_start_time(pid):
    """Return psutil create_time for pid, or None if unavailable."""
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


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
            [
                "ssh",
                "-o",
                f"ConnectTimeout={ssh_timeout}",
                "-o",
                "BatchMode=yes",
                hostname,
                f"kill -0 {pid} 2>/dev/null",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ssh_timeout + 1,
        )

        # Exit code 0 = process exists
        # Exit code 1 = process doesn't exist
        # Exit code 255 = SSH connection failed
        if result.returncode == 255:
            return False, True  # SSH error
        return result.returncode == 0, False

    except (subprocess.TimeoutExpired, OSError):
        return False, True  # Treat timeout/error as SSH failure
