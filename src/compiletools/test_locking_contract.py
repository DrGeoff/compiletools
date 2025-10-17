"""Contract tests for locking behavior.

These tests document and verify the current behavior of locking.py methods
BEFORE refactoring to extract shared utilities. They serve as a safety net
to ensure that refactoring doesn't change observable behavior.

These tests must pass both BEFORE and AFTER the refactor to lock_utils.py.
"""

import pytest
import tempfile
import os
import time
from unittest.mock import Mock
import compiletools.locking


@pytest.fixture
def mock_args():
    """Create mock args object with locking configuration."""
    args = Mock()
    args.shared_objects = True
    args.lock_cross_host_timeout = 600
    args.lock_warn_interval = 60
    args.lock_creation_grace_period = 2
    args.sleep_interval_lockdir = 0.01
    args.sleep_interval_cifs = 0.01
    args.sleep_interval_flock_fallback = 0.01
    args.verbose = 0
    return args


@pytest.fixture
def temp_lockdir():
    """Create temporary directory for lock testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lockfile = os.path.join(tmpdir, "test.o")
        yield lockfile
        # Cleanup lockdirs
        for ext in [".lockdir", ".lock", ".lock.excl", ".lock.pid"]:
            try:
                path = lockfile + ext
                if os.path.isdir(path):
                    import shutil
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass


class TestLockdirLockContract:
    """Contract tests for LockdirLock behavior - must not change during refactor."""

    def test_lock_age_calculation_normal(self, temp_lockdir, mock_args):
        """Contract: Lock age is calculated as (now - mtime) in seconds."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create lockdir with known mtime
        os.makedirs(lock.lockdir)
        known_mtime = time.time() - 100.5  # 100.5 seconds ago
        os.utime(lock.lockdir, (known_mtime, known_mtime))

        age = lock._get_lock_age_seconds()

        # Age should be approximately 100.5 seconds (allow 1 second tolerance)
        assert 99.5 <= age <= 101.5, f"Expected age ~100.5, got {age}"

    def test_lock_age_calculation_future_mtime(self, temp_lockdir, mock_args):
        """Contract: Future mtime (clock skew) returns age 0."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create lockdir with future mtime
        os.makedirs(lock.lockdir)
        future_mtime = time.time() + 3600  # 1 hour in future
        os.utime(lock.lockdir, (future_mtime, future_mtime))

        age = lock._get_lock_age_seconds()

        # Contract: Future mtime must return 0
        assert age == 0, f"Expected age 0 for future mtime, got {age}"

    def test_lock_age_calculation_nonexistent(self, temp_lockdir, mock_args):
        """Contract: Nonexistent lockdir returns age 0."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Don't create lockdir
        age = lock._get_lock_age_seconds()

        # Contract: Nonexistent lock must return 0
        assert age == 0, f"Expected age 0 for nonexistent lock, got {age}"

    def test_stale_detection_local_dead_process(self, temp_lockdir, mock_args):
        """Contract: Local lock with dead process (PID 999999) is stale."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create lockdir with fake PID that doesn't exist
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write(f"{lock.hostname}:999999\n")

        is_stale = lock._is_lock_stale()

        # Contract: Dead local process must be detected as stale
        assert is_stale is True, "Expected dead local process to be stale"

    def test_stale_detection_local_alive_process(self, temp_lockdir, mock_args):
        """Contract: Local lock with alive process (our PID) is not stale."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create lockdir with our own PID (guaranteed to exist)
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write(f"{lock.hostname}:{os.getpid()}\n")

        is_stale = lock._is_lock_stale()

        # Contract: Our own process must NOT be detected as stale
        assert is_stale is False, "Expected our own process to not be stale"

    def test_stale_detection_remote_host(self, temp_lockdir, mock_args):
        """Contract: Remote host lock is not considered stale (can't verify)."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create lockdir from different host
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write("remote-host:12345\n")

        is_stale = lock._is_lock_stale()

        # Contract: Remote locks must NOT be considered stale
        # (LockdirLock doesn't do SSH checks - only local process checks)
        assert is_stale is False, "Expected remote lock to not be stale"

    def test_stale_detection_missing_pid_file(self, temp_lockdir, mock_args):
        """Contract: Fresh lockdir without pid file is NOT stale (grace period)."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create fresh lockdir but no pid file (simulates creation race)
        os.makedirs(lock.lockdir)

        is_stale = lock._is_lock_stale()

        # Contract: Fresh lock without PID is NOT stale (within grace period)
        assert is_stale is False, "Expected fresh lockdir without pid file to not be stale"

        # Make lock old (older than cross_host_timeout)
        old_time = time.time() - 700
        os.utime(lock.lockdir, (old_time, old_time))

        is_stale = lock._is_lock_stale()

        # Contract: Old lock without PID IS stale (exceeded timeout)
        assert is_stale is True, "Expected old lockdir without pid file to be stale"

    def test_stale_detection_malformed_pid_file(self, temp_lockdir, mock_args):
        """Contract: Fresh malformed pid file is NOT stale (grace period)."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create fresh lockdir with malformed pid file
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write("invalid-no-colon\n")

        is_stale = lock._is_lock_stale()

        # Contract: Fresh malformed lock is NOT stale (within grace period)
        assert is_stale is False, "Expected fresh malformed pid file to not be stale"

        # Make lock old
        old_time = time.time() - 700
        os.utime(lock.lockdir, (old_time, old_time))

        is_stale = lock._is_lock_stale()

        # Contract: Old malformed lock IS stale
        assert is_stale is True, "Expected old malformed pid file to be stale"

    def test_stale_detection_empty_pid_file(self, temp_lockdir, mock_args):
        """Contract: Fresh empty pid file is NOT stale (grace period)."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Create fresh lockdir with empty pid file
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write("")

        is_stale = lock._is_lock_stale()

        # Contract: Fresh empty pid file is NOT stale (within grace period)
        assert is_stale is False, "Expected fresh empty pid file to not be stale"

        # Make lock old
        old_time = time.time() - 700
        os.utime(lock.lockdir, (old_time, old_time))

        is_stale = lock._is_lock_stale()

        # Contract: Old empty pid file IS stale
        assert is_stale is True, "Expected old empty pid file to be stale"


class TestLockBehaviorContract:
    """Contract tests for overall locking behavior."""

    def test_acquire_creates_lockdir_with_pid_file(self, temp_lockdir, mock_args):
        """Contract: acquire() creates lockdir with hostname:pid in pid file."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        lock.acquire()

        # Verify lockdir exists
        assert os.path.exists(lock.lockdir), "Expected lockdir to exist after acquire"
        assert os.path.isdir(lock.lockdir), "Expected lockdir to be a directory"

        # Verify pid file exists and has correct format
        assert os.path.exists(lock.pid_file), "Expected pid file to exist"

        with open(lock.pid_file, 'r') as f:
            content = f.read().strip()

        # Contract: Format must be hostname:pid
        assert ':' in content, f"Expected hostname:pid format, got: {content}"
        hostname, pid = content.split(':', 1)
        assert int(pid) == os.getpid(), f"Expected our PID, got {pid}"

        lock.release()

    def test_release_removes_lockdir(self, temp_lockdir, mock_args):
        """Contract: release() removes the lockdir."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        lock.acquire()
        assert os.path.exists(lock.lockdir), "Lockdir should exist after acquire"

        lock.release()

        # Contract: Lockdir must be removed after release
        assert not os.path.exists(lock.lockdir), "Expected lockdir to be removed after release"

    def test_stale_lock_removal_on_acquire(self, temp_lockdir, mock_args):
        """Contract: acquire() removes stale locks before acquiring."""
        lock = compiletools.locking.LockdirLock(temp_lockdir, mock_args)

        # Manually create stale lock
        os.makedirs(lock.lockdir)
        with open(lock.pid_file, 'w') as f:
            f.write(f"{lock.hostname}:999999\n")  # Fake dead PID

        # Verify it's considered stale
        assert lock._is_lock_stale(), "Precondition: lock should be stale"

        # acquire() should remove stale lock and create new one
        lock.acquire()

        # Verify new lock has our PID
        with open(lock.pid_file, 'r') as f:
            content = f.read().strip()
        _, pid = content.split(':', 1)
        assert int(pid) == os.getpid(), "Expected new lock with our PID"

        lock.release()
