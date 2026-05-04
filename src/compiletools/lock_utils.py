"""Shared utilities for file locking and lock management.

This module contains pure utility functions used by both:
- locking.py (for lock acquisition/release)
- cleanup_locks.py (for stale lock cleanup)

No circular dependencies: only imports stdlib (os, subprocess, sys, time).
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
            unreadable. start_time is a float (ticks-since-boot in seconds,
            as returned by ``get_process_start_time``) or None when the file
            uses the legacy two-field format or when the platform cannot
            report a start time.
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
            pid = int(lock_pid)
            # Reject pid <= 0: os.kill(0, 0) targets the calling pgrp and
            # os.kill(-1, 0) targets all signalable processes — both succeed
            # and would be misread as "lock holder still alive".
            if pid <= 0:
                return None, None, None
            return lock_host, pid, None
        lock_host, lock_pid, start_time_str = parts
        pid = int(lock_pid)
        if pid <= 0:
            return None, None, None
        try:
            start_time = float(start_time_str)
        except ValueError:
            start_time = None
        return lock_host, pid, start_time

    except (OSError, ValueError):
        return None, None, None


# PID-reuse start_time tolerance.
#
# Linux/Android: ``get_process_start_time`` reads /proc/[pid]/stat field 22
# (starttime, clock-tick resolution, typically 10ms). 0.1s is comfortably
# above that and tight enough to catch a PID reused within a second.
#
# Other platforms: ``get_process_start_time`` returns None and the
# tolerance is unused, but we keep 1.0s as a defensive default for any
# future platform-specific implementation that might use coarser
# (second-resolution) kernel APIs.
_PID_REUSE_TOLERANCE_SECONDS = 0.1 if sys.platform.startswith("linux") else 1.0


def is_process_alive_local(pid, start_time=None):
    """Check if a local process exists, optionally verifying it is the
    *same* process that recorded the lock (not a PID-reused successor).

    Uses ``os.kill(pid, 0)`` to probe pid existence:
        - ProcessLookupError -> the process is gone, return False
        - PermissionError    -> the process exists but is owned by another
          uid; we cannot signal it, but we know it's alive
        - success            -> alive

    Args:
        pid: Process ID to check.
        start_time: Optional start_time of the recorded holder, as
            produced by ``get_process_start_time`` (ticks-since-boot in
            seconds on Linux/Android). When provided, returns False
            unless the live process with this pid has a matching
            start_time. Tolerance is platform-dependent — see
            ``_PID_REUSE_TOLERANCE_SECONDS``. None means legacy file
            with no recorded start_time; fall back to pid-existence
            only. If ``get_process_start_time`` returns None for the
            live process (e.g. on macOS, or /proc unreadable), we
            conservatively return True — same as the legacy
            two-field-lockfile path.

    Returns:
        bool: True if the live process is (probably) the original
            lock holder. False if the pid does not exist, or if it
            does but its start_time does not match the recorded one.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another uid — treat as alive.
        # We cannot read /proc/{pid}/stat for a foreign-uid process
        # reliably enough to compare start_time, so skip that check.
        return True
    except OSError:
        # Defensive: any other OSError (e.g. EINVAL) — treat as not alive.
        return False

    if start_time is None:
        return True

    actual_start = get_process_start_time(pid)
    if actual_start is None:
        # Cannot determine start_time for the live process (non-Linux,
        # /proc unreadable, race with process exit, etc.). Conservative
        # fallback: trust pid-existence, matching legacy two-field
        # lockfile behaviour.
        return True
    return abs(actual_start - start_time) < _PID_REUSE_TOLERANCE_SECONDS


def get_process_start_time(pid):
    """Return the process start time for pid in seconds, or None if
    unavailable.

    On Linux/Android this reads field 22 (``starttime``) from
    ``/proc/{pid}/stat``, which is the time the process started after
    system boot, expressed in clock ticks. We divide by
    ``os.sysconf("SC_CLK_TCK")`` to convert to seconds.

    The returned value is **ticks-since-boot in seconds** — a per-boot
    *relative* value, not a wall-clock absolute time. This is
    intentional: lockdir start_time comparisons are always same-host
    same-boot, so a relative value is sufficient and side-steps
    Termux/Android's unreadable ``/proc/stat`` ``btime`` field (which
    a wall-clock conversion would require).

    On any other platform (notably macOS/darwin), returns None. The
    caller treats None as "skip start_time check, fall back to
    pid-existence", matching the legacy two-field-lockfile path.
    """
    if not sys.platform.startswith(("linux", "android")):
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # The ``comm`` field (field 2) is wrapped in parens and may
        # itself contain spaces or close-parens, so locate the *last*
        # ')' byte to find the end of comm. Everything after that is
        # space-separated and starts with field 3 (state).
        rparen = data.rfind(b")")
        if rparen < 0:
            return None
        fields = data[rparen + 1 :].split()
        # post-comm slice is fields[3:]; index 0 = field 3 (state),
        # so field 22 (starttime) lives at index 22 - 3 = 19.
        if len(fields) <= 19:
            return None
        ticks = int(fields[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        if clk_tck <= 0:
            return None
        return ticks / clk_tck
    except (OSError, ValueError):
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
