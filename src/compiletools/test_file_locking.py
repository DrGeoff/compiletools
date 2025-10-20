"""Unit tests for file locking mechanisms."""

import pytest
import tempfile
import multiprocessing
import time
import os
from unittest.mock import Mock, patch
import compiletools.locking
import compiletools.apptools


@pytest.fixture
def mock_args():
    """Create mock args object with locking configuration."""
    args = Mock()
    args.shared_objects = True
    args.lock_cross_host_timeout = 600
    args.lock_warn_interval = 60
    args.lock_creation_grace_period = 2
    args.sleep_interval_lockdir = 0.01  # Fast for tests
    args.sleep_interval_cifs = 0.01
    args.sleep_interval_flock_fallback = 0.01
    args.verbose = 0
    return args


@pytest.fixture
def temp_lock_file():
    """Create temporary file for lock testing."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        temp_path = f.name
    yield temp_path
    # Cleanup
    for ext in ["", ".lockdir", ".lock", ".lock.excl", ".lock.pid"]:
        try:
            path = temp_path + ext
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


class TestLockdirLock:
    """Tests for lockdir-based locking (NFS/GPFS/Lustre)."""

    def test_acquire_release_basic(self, temp_lock_file, mock_args):
        """Test basic lock acquire and release."""
        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Acquire lock
        lock.acquire()
        assert os.path.exists(lock.lockdir)
        assert os.path.exists(lock.pid_file)

        # Verify pid file content
        with open(lock.pid_file, "r") as f:
            content = f.read().strip()
            assert ":" in content
            hostname, pid = content.split(":", 1)
            assert int(pid) == os.getpid()

        # Release lock
        lock.release()
        assert not os.path.exists(lock.lockdir)

    def test_concurrent_access_serial(self, temp_lock_file, mock_args):
        """Test two locks acquiring serially."""
        lock1 = compiletools.locking.LockdirLock(temp_lock_file, mock_args)
        lock2 = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # First lock acquires
        lock1.acquire()
        assert os.path.exists(lock1.lockdir)

        # Release first lock
        lock1.release()
        assert not os.path.exists(lock1.lockdir)

        # Second lock acquires
        lock2.acquire()
        assert os.path.exists(lock2.lockdir)
        lock2.release()

    def test_stale_lock_detection_same_host(self, temp_lock_file, mock_args):
        """Test stale lock removal when process is dead."""
        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Manually create a stale lock with fake PID
        os.mkdir(lock.lockdir)
        fake_pid = 999999  # Unlikely to exist
        with open(lock.pid_file, "w") as f:
            f.write(f"{lock.hostname}:{fake_pid}\n")

        # Verify lock is detected as stale
        assert lock._is_lock_stale()

        # Acquiring should remove stale lock and succeed
        lock.acquire()
        assert os.path.exists(lock.lockdir)

        # Verify new PID is ours
        with open(lock.pid_file, "r") as f:
            content = f.read().strip()
            _, pid = content.split(":", 1)
            assert int(pid) == os.getpid()

        lock.release()

    def test_cross_host_lock_not_stale(self, temp_lock_file, mock_args):
        """Test that cross-host locks are not considered stale."""
        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Create lock from different host
        os.mkdir(lock.lockdir)
        with open(lock.pid_file, "w") as f:
            f.write("otherhost:12345\n")

        # Should not be stale (can't verify remote process)
        assert not lock._is_lock_stale()

    def test_future_mtime_clock_skew(self, temp_lock_file, mock_args):
        """Test handling of future mtimes (clock skew)."""
        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Create lock and set mtime to future
        os.mkdir(lock.lockdir)
        future_time = time.time() + 3600  # 1 hour in future
        os.utime(lock.lockdir, (future_time, future_time))

        # Should return age 0 for future mtimes
        age = lock._get_lock_age_seconds()
        assert age == 0

    def test_unreadable_lock_info_grace_period(self, temp_lock_file, mock_args):
        """Test grace period for locks without PID file during creation."""
        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Create lock without pid file (simulates creation race)
        os.mkdir(lock.lockdir)

        # Should NOT be stale immediately (within grace period)
        assert not lock._is_lock_stale()

        # Make lock old by setting ancient mtime (older than cross_host_timeout)
        old_time = time.time() - 700  # Older than 600s timeout
        os.utime(lock.lockdir, (old_time, old_time))

        # Should be stale now (exceeded timeout)
        assert lock._is_lock_stale()

    def test_context_manager(self, temp_lock_file, mock_args):
        """Test FileLock context manager with lockdir strategy."""
        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"):
            with compiletools.locking.FileLock(temp_lock_file, mock_args):
                lockdir = temp_lock_file + ".lockdir"
                assert os.path.exists(lockdir)

            # Lock released after context
            assert not os.path.exists(lockdir)

    def test_nonexistent_directory(self, mock_args):
        """Test that lock creation works when parent directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create path in nonexistent subdirectory
            lock_file = os.path.join(tmpdir, "subdir", "nested", "file.txt")

            lock = compiletools.locking.LockdirLock(lock_file, mock_args)

            # Should create parent directories and acquire lock
            lock.acquire()
            assert os.path.exists(lock.lockdir)
            assert os.path.exists(lock.pid_file)

            # Cleanup
            lock.release()
            assert not os.path.exists(lock.lockdir)

    def test_multiuser_permissions(self, mock_args):
        """Test that lockdir and pid file have correct permissions for multi-user mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "file.txt")

            # Create target file first so group can be copied
            with open(lock_file, "w") as f:
                f.write("test")

            lock = compiletools.locking.LockdirLock(lock_file, mock_args)
            lock.acquire()

            # Verify lockdir permissions (should be 775 = rwxrwxr-x)
            lockdir_stat = os.stat(lock.lockdir)
            lockdir_mode = lockdir_stat.st_mode & 0o777
            assert lockdir_mode == 0o775, f"Expected 0o775, got {oct(lockdir_mode)}"

            # Verify pid file permissions (should be 664 = rw-rw-r--)
            pid_stat = os.stat(lock.pid_file)
            pid_mode = pid_stat.st_mode & 0o777
            assert pid_mode == 0o664, f"Expected 0o664, got {oct(pid_mode)}"

            # Verify lockdir group matches target file group
            target_stat = os.stat(lock_file)
            assert lockdir_stat.st_gid == target_stat.st_gid, \
                "Lockdir group should match target file group"

            lock.release()

    def test_lockdir_removed_during_pid_write_retry(self, temp_lock_file, mock_args):
        """Test retry mechanism when lockdir removed during pid write.

        This tests the fix for the race condition where:
        1. Process A: mkdir succeeds, begins pid file write
        2. Process B: Sees lockdir without pid, treats as stale after grace period
        3. Process B: Removes lockdir
        4. Process A: Fails to write pid file, retries acquisition
        """
        import shutil

        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Track attempts
        attempt_count = {'value': 0}
        original_atomic = compiletools.filesystem_utils.atomic_output_file

        def failing_atomic(target_path, *args, **kwargs):
            """Simulate lockdir removal on first attempt."""
            attempt_count['value'] += 1

            if attempt_count['value'] == 1:
                # Simulate concurrent process removing lockdir during pid write
                if os.path.exists(lock.lockdir):
                    shutil.rmtree(lock.lockdir)
                # Raise the error that would occur when lockdir is gone
                raise FileNotFoundError(f"No such file or directory: '{lock.lockdir}'")

            # Subsequent attempts succeed normally
            return original_atomic(target_path, *args, **kwargs)

        # Patch atomic_output_file to simulate race condition
        with patch('compiletools.filesystem_utils.atomic_output_file', side_effect=failing_atomic):
            # Should retry and succeed on second attempt
            lock.acquire()

            # Verify lock acquired successfully
            assert os.path.exists(lock.lockdir), "Lockdir should exist after retry"
            assert os.path.exists(lock.pid_file), "PID file should exist after retry"

            # Verify we retried (2 attempts: 1 failed + 1 succeeded)
            assert attempt_count['value'] == 2, f"Expected 2 attempts, got {attempt_count['value']}"

            # Verify pid file has correct content
            with open(lock.pid_file, 'r') as f:
                content = f.read().strip()
            assert ':' in content, "PID file should have hostname:pid format"
            _, pid = content.split(':', 1)
            assert int(pid) == os.getpid(), "PID file should contain our PID"

            lock.release()
            assert not os.path.exists(lock.lockdir), "Lockdir should be removed after release"

    def test_lockdir_removed_max_retries_exceeded(self, temp_lock_file, mock_args):
        """Test that acquisition fails after 3 retry attempts."""
        import shutil

        lock = compiletools.locking.LockdirLock(temp_lock_file, mock_args)

        # Make atomic_output_file always fail
        def always_failing_atomic(target_path, *args, **kwargs):
            """Always remove lockdir to force continuous failures."""
            if os.path.exists(lock.lockdir):
                shutil.rmtree(lock.lockdir)
            raise FileNotFoundError(f"No such file or directory: '{lock.lockdir}'")

        with patch('compiletools.filesystem_utils.atomic_output_file', side_effect=always_failing_atomic):
            # Should fail after 3 attempts
            with pytest.raises(RuntimeError, match="Failed to acquire lock after 3 attempts"):
                lock.acquire()

            # Lockdir should not exist (cleaned up after final failure)
            assert not os.path.exists(lock.lockdir), "Lockdir should be cleaned up after failed attempts"


class TestCIFSLock:
    """Tests for CIFS/SMB locking."""

    def test_acquire_release_basic(self, temp_lock_file, mock_args):
        """Test basic CIFS lock acquire and release."""
        lock = compiletools.locking.CIFSLock(temp_lock_file, mock_args)

        lock.acquire()
        assert os.path.exists(lock.lockfile_excl)

        lock.release()
        assert not os.path.exists(lock.lockfile_excl)

    def test_concurrent_access_serial(self, temp_lock_file, mock_args):
        """Test two CIFS locks acquiring serially."""
        lock1 = compiletools.locking.CIFSLock(temp_lock_file, mock_args)
        lock2 = compiletools.locking.CIFSLock(temp_lock_file, mock_args)

        lock1.acquire()
        assert os.path.exists(lock1.lockfile_excl)

        lock1.release()
        assert not os.path.exists(lock1.lockfile_excl)

        lock2.acquire()
        assert os.path.exists(lock2.lockfile_excl)
        lock2.release()

    def test_context_manager(self, temp_lock_file, mock_args):
        """Test FileLock context manager with CIFS strategy."""
        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="cifs"):
            with compiletools.locking.FileLock(temp_lock_file, mock_args):
                lockfile_excl = temp_lock_file + ".lock.excl"
                assert os.path.exists(lockfile_excl)

            assert not os.path.exists(lockfile_excl)

    def test_nonexistent_directory(self, mock_args):
        """Test that CIFS lock works when parent directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "subdir", "nested", "file.txt")
            lock = compiletools.locking.CIFSLock(lock_file, mock_args)

            lock.acquire()
            assert os.path.exists(lock.lockfile_excl)

            lock.release()
            assert not os.path.exists(lock.lockfile_excl)


class TestFlockLock:
    """Tests for POSIX flock locking."""

    def test_acquire_release_basic(self, temp_lock_file, mock_args):
        """Test basic flock acquire and release."""
        lock = compiletools.locking.FlockLock(temp_lock_file, mock_args)

        lock.acquire()
        assert os.path.exists(lock.lockfile)

        lock.release()
        # Lockfile may still exist but should be unlocked

    def test_windows_compatibility_fallback(self, temp_lock_file, mock_args):
        """Test that FlockLock works without fcntl (Windows simulation)."""
        # Simulate Windows by temporarily hiding fcntl
        original_has_fcntl = compiletools.locking.HAS_FCNTL
        try:
            compiletools.locking.HAS_FCNTL = False

            lock = compiletools.locking.FlockLock(temp_lock_file, mock_args)
            lock.acquire()

            # Should use fallback mechanism (O_EXCL polling)
            assert lock.use_flock is False
            assert os.path.exists(lock.lockfile_pid)

            lock.release()
            assert not os.path.exists(lock.lockfile_pid)
        finally:
            compiletools.locking.HAS_FCNTL = original_has_fcntl

    def test_context_manager(self, temp_lock_file, mock_args):
        """Test FileLock context manager with flock strategy."""
        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"):
            with compiletools.locking.FileLock(temp_lock_file, mock_args):
                lockfile = temp_lock_file + ".lock"
                assert os.path.exists(lockfile)

    def test_nonexistent_directory(self, mock_args):
        """Test that flock works when parent directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "subdir", "nested", "file.txt")
            lock = compiletools.locking.FlockLock(lock_file, mock_args)

            lock.acquire()
            assert os.path.exists(lock.lockfile)

            lock.release()


class TestFileLock:
    """Tests for FileLock context manager and strategy selection."""

    def test_strategy_selection_lockdir(self, temp_lock_file, mock_args):
        """Test that lockdir strategy is selected for NFS."""
        with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"):
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"):
                lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
                assert isinstance(lock_ctx.lock, compiletools.locking.LockdirLock)

    def test_strategy_selection_cifs(self, temp_lock_file, mock_args):
        """Test that CIFS strategy is selected for CIFS filesystem."""
        with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="cifs"):
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="cifs"):
                lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
                assert isinstance(lock_ctx.lock, compiletools.locking.CIFSLock)

    def test_strategy_selection_flock(self, temp_lock_file, mock_args):
        """Test that flock strategy is selected for local filesystems."""
        with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"):
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"):
                lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
                assert isinstance(lock_ctx.lock, compiletools.locking.FlockLock)

    def test_noop_when_shared_objects_false(self, temp_lock_file, mock_args):
        """Test that FileLock is no-op when shared_objects=False."""
        mock_args.shared_objects = False

        lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
        assert lock_ctx.lock is None

        # Should not raise
        with lock_ctx:
            pass

    def test_filesystem_detection_failure_defaults_to_flock(self, temp_lock_file, mock_args):
        """Test that filesystem detection failure defaults to flock."""
        with patch(
            "compiletools.filesystem_utils.get_filesystem_type",
            side_effect=Exception("Detection failed")
        ):
            lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
            assert isinstance(lock_ctx.lock, compiletools.locking.FlockLock)

    def test_configuration_propagation(self, temp_lock_file, mock_args):
        """Test that args.* values are used correctly."""
        mock_args.lock_cross_host_timeout = 1234
        mock_args.lock_warn_interval = 567
        mock_args.sleep_interval_lockdir = 0.789

        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"):
            lock_ctx = compiletools.locking.FileLock(temp_lock_file, mock_args)
            assert lock_ctx.lock.cross_host_timeout == 1234
            assert lock_ctx.lock.warn_interval == 567
            assert lock_ctx.lock.sleep_interval == 0.789

    def test_exception_propagation(self, temp_lock_file, mock_args):
        """Test that exceptions inside context are propagated."""
        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"):
            with pytest.raises(ValueError):
                with compiletools.locking.FileLock(temp_lock_file, mock_args):
                    raise ValueError("Test exception")

            # Lock should be released even after exception
            lockdir = temp_lock_file + ".lockdir"
            assert not os.path.exists(lockdir)

    def test_creates_output_directory(self, mock_args):
        """Test that FileLock creates output directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Path with nonexistent subdirectories
            output_file = os.path.join(tmpdir, "build", "subdir", "compile_commands.json")

            # FileLock should create the parent directory
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"):
                with compiletools.locking.FileLock(output_file, mock_args):
                    # Directory should exist
                    parent_dir = os.path.dirname(output_file)
                    assert os.path.exists(parent_dir)
                    assert os.path.isdir(parent_dir)


def _concurrent_lock_worker(queue, temp_file, args_dict, worker_id, strategy):
    """Worker process for concurrent lock testing."""
    # Recreate args from dict
    args = Mock()
    for key, val in args_dict.items():
        setattr(args, key, val)

    try:
        # Patch strategy selection
        with patch("compiletools.filesystem_utils.get_lock_strategy", return_value=strategy):
            with compiletools.locking.FileLock(temp_file, args):
                # Signal we have the lock
                queue.put(("acquired", worker_id))
                # Hold lock briefly
                time.sleep(0.05)
                queue.put(("released", worker_id))
    except Exception as e:
        queue.put(("error", str(e)))


class TestConcurrentLocking:
    """Tests for concurrent lock access."""

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock"])
    def test_concurrent_processes_serial_access(self, temp_lock_file, mock_args, strategy):
        """Test that concurrent processes acquire locks serially."""
        # Use spawn method to avoid fork() deprecation warnings
        ctx = multiprocessing.get_context('spawn')

        # Convert args to dict for pickling
        args_dict = {
            "shared_objects": True,
            "lock_cross_host_timeout": 600,
            "lock_warn_interval": 60,
            "lock_creation_grace_period": 2,
            "sleep_interval_lockdir": 0.01,
            "sleep_interval_cifs": 0.01,
            "sleep_interval_flock_fallback": 0.01,
            "verbose": 0,
        }

        queue = ctx.Queue()
        processes = []

        # Launch 3 workers
        for i in range(3):
            p = ctx.Process(
                target=_concurrent_lock_worker,
                args=(queue, temp_lock_file, args_dict, i, strategy),
            )
            p.start()
            processes.append(p)

        # Collect results
        results = []
        timeout = 5
        start = time.time()
        while len(results) < 6 and time.time() - start < timeout:  # 3 acquire + 3 release
            try:
                result = queue.get(timeout=0.1)
                results.append(result)
            except Exception:
                pass

        # Wait for processes
        for p in processes:
            p.join(timeout=1)
            if p.is_alive():
                p.terminate()

        # Verify all workers succeeded
        acquired = [r for r in results if r[0] == "acquired"]
        released = [r for r in results if r[0] == "released"]
        errors = [r for r in results if r[0] == "error"]

        assert len(errors) == 0, f"Workers had errors: {errors}"
        assert len(acquired) == 3, f"Expected 3 acquires, got {len(acquired)}"
        assert len(released) == 3, f"Expected 3 releases, got {len(released)}"
