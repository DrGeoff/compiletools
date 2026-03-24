"""File locking for concurrent builds.

Python implementation of the same locking algorithms used in ct-lock-helper shell script.
Both this module and ct-lock-helper use identical algorithms for lock acquisition/release.
All policies (timeouts, sleep intervals) are configured via args object from apptools.py.
"""

import os
import platform
import shutil
import socket
import subprocess
import sys
import time

import compiletools.filesystem_utils
import compiletools.lock_utils
import compiletools.wrappedos

# fcntl only available on Unix (not Windows)
try:
    import fcntl

    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


class FcntlLock:
    """fcntl.lockf()-based locking for GPFS (cross-node, kernel-managed).

    Uses POSIX fcntl record locks which work correctly across GPFS nodes
    (unlike flock which is node-local on GPFS). The kernel handles blocking
    and automatic release on process death — no polling, no stale detection,
    no holder info needed.

    Locks the target file directly (no sidecar .lock file). This works because
    gcc opens the output with O_WRONLY|O_CREAT|O_TRUNC, which preserves the
    inode — so the advisory fcntl lock stays valid.
    """

    direct_compile = True

    def __init__(self, target_file, args):
        self.lockfile = compiletools.wrappedos.realpath(target_file)
        self.fd = None
        self.args = args

    def acquire(self):
        """Acquire lock using fcntl.lockf(LOCK_EX).

        Opens/creates .lock file, then blocks until the lock is acquired.
        The kernel handles queuing and automatic release on process death.
        """
        if not HAS_FCNTL:
            raise RuntimeError("fcntl module not available (Windows?); cannot use fcntl lock strategy")

        # Ensure parent directory exists
        parent_dir = compiletools.wrappedos.dirname(self.lockfile)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        self.fd = os.open(self.lockfile, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.lockf(self.fd, fcntl.LOCK_EX)

    def release(self):
        """Release fcntl lock and close fd. Does NOT unlink lock file."""
        if self.fd is not None:
            try:
                fcntl.lockf(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class LockdirLock:
    """Lockdir-based locking for NFS/Lustre (mkdir atomic operation)."""

    direct_compile = False

    def __init__(self, target_file, args):
        # Use wrappedos for path computations (pure, cacheable)
        self.target_file = compiletools.wrappedos.realpath(target_file)
        self.lockdir = self.target_file + ".lockdir"
        # Use os.path.join for paths we'll check existence of (not wrappedos.join)
        self.pid_file = os.path.join(self.lockdir, "pid")
        self.args = args
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.cross_host_timeout = args.lock_cross_host_timeout
        self.warn_interval = args.lock_warn_interval
        self.creation_grace_period = getattr(args, "lock_creation_grace_period", 2)

        # Auto-detect optimal sleep interval based on filesystem, allow user override
        if args.sleep_interval_lockdir is not None:
            self.sleep_interval = args.sleep_interval_lockdir
        else:
            # Detect filesystem type for auto-tuning
            target_dir = compiletools.wrappedos.dirname(self.target_file) or "."
            try:
                fstype = compiletools.filesystem_utils.get_filesystem_type(target_dir)
                self.sleep_interval = compiletools.filesystem_utils.get_lockdir_sleep_interval(fstype)
            except Exception:
                # Fallback to conservative default if detection fails
                self.sleep_interval = 0.05

        self.platform = platform.system().lower()

    def _set_lockdir_permissions(self):
        """Set lockdir permissions for multi-user file-locking mode.

        Mirrors shell behavior:
        - chmod 775 lockdir (group-writable)
        - chgrp --reference=target_file lockdir (match target file's group)
        """
        try:
            # Set directory to 775 (rwxrwxr-x) - group-writable
            os.chmod(self.lockdir, 0o775)

            # Set group to match target file (if it exists)
            # Shell: chgrp --reference="$@" "$$lockdir"
            if os.path.exists(self.target_file):
                stat_info = os.stat(self.target_file)
                try:
                    os.chown(self.lockdir, -1, stat_info.st_gid)
                except PermissionError:
                    # Can't change group - not fatal, continue
                    pass
        except OSError as e:
            # Permission errors here are not fatal (same as shell || true)
            if self.args.verbose >= 2:
                print(
                    f"Warning: Could not set lockdir permissions: {e}",
                    file=sys.stderr,
                )

    def _get_lock_age_seconds(self):
        """Get lock age (uses shared lock_utils, uncached mtime).

        Returns:
            float: Age in seconds, or 0 if lock doesn't exist or has future mtime
        """
        return compiletools.lock_utils.get_lock_age_seconds(self.lockdir, getattr(self.args, "verbose", 0))

    def _read_lock_info(self):
        """Read hostname:pid from lock file (uses shared lock_utils).

        Returns:
            tuple: (hostname, pid) or (None, None) if unreadable
        """
        return compiletools.lock_utils.read_lock_info(self.lockdir)

    def _is_process_alive_same_host(self, pid):
        """Check if process is alive on same host (uses shared lock_utils).

        Uses psutil (required dependency) for robust cross-platform checking.
        Equivalent to shell's "kill -0 $pid" + "/proc check" but more reliable.

        Args:
            pid: Process ID to check

        Returns:
            bool: True if process exists, False otherwise
        """
        return compiletools.lock_utils.is_process_alive_local(pid)

    def _is_lock_stale(self):
        """Check if lock is stale.

        A lock without a PID file could be in one of three states:
        1. Being created right now (NOT stale - use grace period)
        2. Very old and abandoned (IS stale - use cross_host_timeout)
        3. In-between (conservative: NOT stale, wait for it)

        For locks with PID files:
        - Same-host: check if process is alive
        - Cross-host: cannot verify, assume NOT stale

        Returns:
            bool: True if lock is stale and should be removed
        """
        lock_host, lock_pid = self._read_lock_info()

        if lock_host is None:
            # No PID file - use age-based detection to handle creation race
            age = self._get_lock_age_seconds()

            if age < self.creation_grace_period:
                # Fresh lock being created - NOT stale
                return False

            if age > self.cross_host_timeout:
                # Legitimately abandoned - IS stale
                return True

            # Middle ground: lock exists, no PID, not yet timed out
            # Conservative: don't remove (wait for timeout or grace period)
            return False

        if lock_host != self.hostname:
            # Cross-host lock, not stale (can't verify remote process)
            return False

        # Same-host lock: check if process exists
        # Use psutil (more robust than os.kill + /proc check)
        return not self._is_process_alive_same_host(lock_pid)

    def _remove_stale_lock(self):
        """Remove stale lock with verification (matches shell rm -rf + error check).

        Returns:
            bool: True if removed successfully

        Raises:
            PermissionError: If lock still exists after removal attempt
        """
        lock_host, lock_pid = self._read_lock_info()
        lock_info = f"{lock_host}:{lock_pid}" if lock_host else "unknown"

        try:
            # Shell uses: rm -rf "$$lockdir" (force, recursive, ignore errors)
            shutil.rmtree(self.lockdir, ignore_errors=True)

            # Shell verifies removal: if [ -e "$$lockdir" ]; then ERROR
            if os.path.exists(self.lockdir):
                # Lock still exists - permission error
                print(
                    f"ERROR: Stale lock from {lock_info} cannot be removed",
                    file=sys.stderr,
                )
                print(f"ERROR: Check permissions on: {self.lockdir}", file=sys.stderr)
                print(
                    "ERROR: Parent directory should be SGID with group write permissions",
                    file=sys.stderr,
                )
                raise PermissionError(f"Cannot remove stale lock: {self.lockdir}")

            # Successfully removed
            if self.args.verbose >= 1:
                print(f"Removed stale lock from {lock_info}", file=sys.stderr)
            return True

        except Exception:
            if os.path.exists(self.lockdir):
                # Still exists, this is a fatal error
                raise
            # Removed despite exception, treat as success
            return True

    def acquire(self):
        """Acquire lock using mkdir (atomic on all filesystems).

        Algorithm mirrors ct-lock-helper lockdir strategy:
        1. Try mkdir (atomic)
        2. If fails, check if stale (same-host process check)
        3. If stale, remove with verification and retry immediately
        4. If not stale, wait with periodic warnings
        5. Write hostname:pid to lockdir/pid file
        6. If lockdir removed during pid write, retry up to 3 times

        Raises:
            PermissionError: If stale lock cannot be removed (fatal)
            RuntimeError: If lock acquisition fails after 3 retries
        """
        # Ensure parent directory exists before attempting lock
        parent_dir = compiletools.wrappedos.dirname(self.lockdir)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        for attempt in range(1, 4):
            last_warn_time = 0
            escalated = False

            while True:
                try:
                    os.mkdir(self.lockdir)
                    # Lock acquired - set multi-user permissions (mirrors shell behavior)
                    self._set_lockdir_permissions()
                    # Write pid file atomically to prevent races during stale lock detection
                    with compiletools.filesystem_utils.atomic_output_file(self.pid_file, "w") as f:
                        f.write(f"{self.hostname}:{self.pid}\n")
                    # Set pid file permissions for multi-user access
                    os.chmod(self.pid_file, 0o664)
                    return  # SUCCESS
                except FileExistsError:
                    # Lock exists, check if stale
                    if self._is_lock_stale():
                        # Stale lock - remove with verification (may raise PermissionError)
                        self._remove_stale_lock()
                        # Shell does: continue (retry immediately, no sleep)
                        continue

                    # Not stale, must wait
                    lock_age = self._get_lock_age_seconds()
                    now = time.time()

                    # Periodic warnings (same as makefile.py)
                    if now - last_warn_time > self.warn_interval:
                        lock_host, lock_pid = self._read_lock_info()
                        print(
                            f"Waiting for lock: {self.lockdir} (held by {lock_host}:{lock_pid})",
                            file=sys.stderr,
                        )
                        last_warn_time = now

                    # Escalate warning at timeout threshold
                    if lock_age > self.cross_host_timeout and not escalated:
                        lock_host, lock_pid = self._read_lock_info()
                        print(
                            f"WARNING: Lock held for {lock_age:.0f}s (timeout: {self.cross_host_timeout}s)",
                            file=sys.stderr,
                        )
                        print(f"Lock holder: {lock_host}:{lock_pid}", file=sys.stderr)
                        escalated = True

                    time.sleep(self.sleep_interval)
                except FileNotFoundError as e:
                    # Lockdir removed during pid write - clean up and retry
                    try:
                        os.rmdir(self.lockdir)
                    except OSError:
                        pass  # Best effort, may already be gone

                    if attempt == 3:
                        raise RuntimeError(f"Failed to acquire lock after 3 attempts: {self.lockdir}") from e

                    if self.args.verbose >= 1:
                        print(
                            f"Lock removed during acquisition, retrying (attempt {attempt}/3)...",
                            file=sys.stderr,
                        )

                    time.sleep(self.sleep_interval)
                    break  # Exit inner while, retry outer for loop

    def release(self):
        """Release lock by removing pid file and lockdir."""
        try:
            # CRITICAL: Use os.path.exists, NOT wrappedos (if it had exists())
            # Caching would be WRONG - must check current state before unlink
            if os.path.exists(self.pid_file):
                os.unlink(self.pid_file)
            os.rmdir(self.lockdir)
        except OSError as e:
            # Best effort cleanup (match makefile.py behavior)
            if self.args.verbose >= 2:
                print(
                    f"Warning: Failed to release lock {self.lockdir}: {e}",
                    file=sys.stderr,
                )


class CIFSLock:
    """CIFS/SMB locking using exclusive file creation (O_CREAT|O_EXCL)."""

    direct_compile = False

    def __init__(self, target_file, args):
        self.lockfile = target_file + ".lock"
        self.lockfile_excl = target_file + ".lock.excl"
        self.fd = None
        self.sleep_interval = args.sleep_interval_cifs
        self.args = args

    def acquire(self):
        """Acquire lock using exclusive file creation (CIFS-safe).

        Algorithm mirrors ct-lock-helper cifs strategy.
        """
        # Ensure parent directory exists
        parent_dir = os.path.dirname(self.lockfile)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        # Open base lockfile (non-exclusive, for reference)
        self.fd = os.open(self.lockfile, os.O_CREAT | os.O_WRONLY, 0o666)

        # Acquire exclusive lock using O_EXCL
        while True:
            try:
                excl_fd = os.open(self.lockfile_excl, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                # Write PID
                os.write(excl_fd, f"{os.getpid()}\n".encode())
                os.close(excl_fd)
                return
            except FileExistsError:
                time.sleep(self.sleep_interval)

    def release(self):
        """Release CIFS lock."""
        try:
            if os.path.exists(self.lockfile_excl):
                os.unlink(self.lockfile_excl)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            # Clean up base lockfile to match Makefile suffix behavior
            if os.path.exists(self.lockfile):
                os.unlink(self.lockfile)
        except OSError as e:
            if self.args.verbose >= 2:
                print(f"Warning: Failed to release CIFS lock: {e}", file=sys.stderr)


class FlockLock:
    """POSIX flock locking for local filesystems (ext4/xfs/btrfs).

    WARNING: flock() is node-local on GPFS/Lustre/NFS. Use FcntlLock
    for GPFS or LockdirLock for NFS/Lustre. This class should only be
    used when filesystem detection confirms a local filesystem.
    """

    direct_compile = False

    def __init__(self, target_file, args):
        self.lockfile = target_file + ".lock"
        self.fd = None
        self.args = args

    def acquire(self):
        """Acquire lock using POSIX flock(LOCK_EX).

        Blocks in the kernel until the lock is acquired. Only used on local
        filesystems where flock() is always available.
        """
        if not HAS_FCNTL:
            raise RuntimeError("fcntl module not available (Windows?); cannot use flock lock strategy")

        # Ensure parent directory exists
        parent_dir = os.path.dirname(self.lockfile)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        self.fd = os.open(self.lockfile, os.O_CREAT | os.O_WRONLY, 0o666)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def release(self):
        """Release flock and clean up lockfile."""
        try:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None
            if os.path.exists(self.lockfile):
                os.unlink(self.lockfile)
        except OSError as e:
            if self.args.verbose >= 2:
                print(f"Warning: Failed to release flock: {e}", file=sys.stderr)


class FileLock:
    """Context manager for file locking with automatic strategy selection.

    Strategy selection (via filesystem_utils.get_lock_strategy):
    - 'fcntl': GPFS (fcntl.lockf(), cross-node, kernel-managed)
    - 'lockdir': NFS, Lustre (mkdir-based locking)
    - 'cifs': CIFS/SMB (exclusive file creation)
    - 'flock': All others, including unknown filesystems (POSIX flock, kernel-managed blocking)

    Note: Unknown/undetectable filesystems safely default to 'flock' strategy,
    which is the most portable (works on all POSIX systems).
    """

    def __init__(self, target_file, args):
        if not getattr(args, "file_locking", False):
            self.lock = None
            return

        # Ensure parent directory exists before filesystem detection and lock creation
        target_dir = compiletools.wrappedos.dirname(target_file) or "."
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        try:
            # Filesystem detection does I/O but result is stable for a given dir
            fstype = compiletools.filesystem_utils.get_filesystem_type(target_dir)
            strategy = compiletools.filesystem_utils.get_lock_strategy(fstype)
        except Exception as e:
            # Filesystem detection failed - default to flock (safest/most portable)
            if getattr(args, "verbose", 0) >= 2:
                print(
                    f"Warning: Filesystem detection failed for {target_file}, defaulting to flock: {e}",
                    file=sys.stderr,
                )
            strategy = "flock"

        # Select lock implementation based on strategy
        if strategy == "fcntl":
            self.lock = FcntlLock(target_file, args)
        elif strategy == "lockdir":
            self.lock = LockdirLock(target_file, args)
        elif strategy == "cifs":
            self.lock = CIFSLock(target_file, args)
        else:  # 'flock' or any unexpected value defaults to flock (safest)
            self.lock = FlockLock(target_file, args)

    def __enter__(self):
        if self.lock:
            self.lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock:
            self.lock.release()
        return False  # Don't suppress exceptions


def atomic_compile(lock, target: str, compile_cmd: list[str]) -> subprocess.CompletedProcess:
    """Execute compilation atomically under a lock.

    For locks with direct_compile=True (FcntlLock): compiles directly to
    the target file. The fcntl advisory lock protects the target while gcc
    writes to it (O_WRONLY|O_CREAT|O_TRUNC preserves the inode).

    For other locks: compiles to a temp file, then renames to target,
    preventing TOCTOU races where another process sees a partially-written
    output file.

    Args:
        lock: Lock object with acquire()/release() methods.
        target: Final output file path.
        compile_cmd: Compile command WITHOUT -o flag.

    Returns:
        subprocess.CompletedProcess from the compiler invocation.

    Raises:
        subprocess.CalledProcessError: If compilation fails.
    """
    if getattr(lock, "direct_compile", False):
        lock.acquire()
        try:
            cmd = list(compile_cmd) + ["-o", target]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, result.stdout, result.stderr
                )
            return result
        finally:
            lock.release()

    pid = os.getpid()
    random_suffix = os.urandom(2).hex()
    tempfile_path = f"{target}.{pid}.{random_suffix}.tmp"

    try:
        lock.acquire()

        cmd = list(compile_cmd) + ["-o", tempfile_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        os.rename(tempfile_path, target)
        return result

    finally:
        lock.release()

        if os.path.exists(tempfile_path):
            try:
                os.unlink(tempfile_path)
            except OSError:
                pass
