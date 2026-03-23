"""Unit tests for locking.py."""

import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from compiletools.locking import CIFSLock, FileLock, FlockLock, LockdirLock


def _make_lock_args(**overrides):
    """Create a minimal args object for locking."""
    defaults = dict(
        verbose=0,
        shared_objects=True,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=0.01,
        sleep_interval_cifs=0.01,
        sleep_interval_flock_fallback=0.01,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestLockdirLock:
    """Test LockdirLock edge cases."""

    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            lock.acquire()
            assert os.path.isdir(lock.lockdir)
            lock.release()
            assert not os.path.exists(lock.lockdir)

    def test_stale_detection_dead_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)

            # Create a stale lock manually
            os.mkdir(lock.lockdir)
            os.chmod(lock.lockdir, 0o775)
            with open(lock.pid_file, "w") as f:
                f.write(f"{lock.hostname}:99999999\n")  # Dead PID

            assert lock._is_lock_stale() is True

    def test_permissions_error_handled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=3)
            lock = LockdirLock(target, args)

            # Acquire to create lockdir, then test permissions
            lock.acquire()
            # _set_lockdir_permissions runs during acquire, just verify no crash
            lock.release()

    def test_auto_detect_sleep_interval(self):
        """When sleep_interval_lockdir is None, auto-detect from filesystem."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(sleep_interval_lockdir=None)
            lock = LockdirLock(target, args)
            # Should have auto-detected a sleep interval
            assert lock.sleep_interval > 0

    def test_auto_detect_sleep_interval_fallback_on_error(self):
        """When filesystem detection fails, fall back to 0.05."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(sleep_interval_lockdir=None)
            with patch("compiletools.filesystem_utils.get_filesystem_type", side_effect=RuntimeError("fail")):
                lock = LockdirLock(target, args)
                assert lock.sleep_interval == 0.05

    def test_set_lockdir_permissions_chown_permission_error(self):
        """PermissionError on chown is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            # Create the target file so chown path is reached
            with open(target, "w") as f:
                f.write("x")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            with patch("os.chown", side_effect=PermissionError("not allowed")):
                lock._set_lockdir_permissions()  # Should not raise

    def test_set_lockdir_permissions_oserror_verbose(self, capsys):
        """OSError during chmod prints warning when verbose >= 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            with patch("os.chmod", side_effect=OSError("perm denied")):
                lock._set_lockdir_permissions()
            assert "Could not set lockdir permissions" in capsys.readouterr().err

    def test_is_lock_stale_no_pid_fresh(self):
        """Lock without PID file within grace period is NOT stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            # Fresh lock (age < grace period) => not stale
            assert lock._is_lock_stale() is False

    def test_is_lock_stale_no_pid_old(self):
        """Lock without PID file older than cross_host_timeout IS stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(lock_cross_host_timeout=1, lock_creation_grace_period=0)
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            # Make the lock appear old
            with patch.object(lock, "_get_lock_age_seconds", return_value=10):
                assert lock._is_lock_stale() is True

    def test_is_lock_stale_no_pid_middle(self):
        """Lock without PID in middle ground (past grace, before timeout) is NOT stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(lock_cross_host_timeout=300, lock_creation_grace_period=2)
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            with patch.object(lock, "_get_lock_age_seconds", return_value=10):
                assert lock._is_lock_stale() is False

    def test_is_lock_stale_cross_host(self):
        """Cross-host lock is NOT stale (can't verify remote process)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            with open(lock.pid_file, "w") as f:
                f.write("otherhost.example.com:12345\n")
            assert lock._is_lock_stale() is False

    def test_remove_stale_lock_success(self, capsys):
        """Successfully removes a stale lock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=1)
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            with open(lock.pid_file, "w") as f:
                f.write(f"{lock.hostname}:99999999\n")
            assert lock._remove_stale_lock() is True
            assert not os.path.exists(lock.lockdir)
            assert "Removed stale lock" in capsys.readouterr().err

    def test_remove_stale_lock_permission_error(self):
        """Raises PermissionError when lock cannot be removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            # Make rmtree a no-op so lockdir still exists
            with patch("shutil.rmtree"):
                with pytest.raises(PermissionError, match="Cannot remove stale lock"):
                    lock._remove_stale_lock()

    def test_remove_stale_lock_exception_but_removed(self):
        """If rmtree raises but lock is gone, treat as success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)

            real_rmtree = shutil.rmtree

            def remove_then_raise(path, ignore_errors=False):
                real_rmtree(path, ignore_errors=True)
                raise RuntimeError("spurious")

            with patch("shutil.rmtree", side_effect=remove_then_raise):
                assert lock._remove_stale_lock() is True

    def test_acquire_stale_lock_removed_and_reacquired(self):
        """Acquire removes stale lock and succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            # Create stale lock with dead PID
            os.mkdir(lock.lockdir)
            with open(lock.pid_file, "w") as f:
                f.write(f"{lock.hostname}:99999999\n")
            lock.acquire()
            assert os.path.isdir(lock.lockdir)
            lock.release()

    def test_acquire_creates_parent_dir(self):
        """Acquire creates parent directory if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            lock.acquire()
            assert os.path.isdir(lock.lockdir)
            lock.release()

    def test_acquire_filenotfounderror_retry(self):
        """FileNotFoundError during pid write triggers retry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=1)
            lock = LockdirLock(target, args)

            call_count = 0
            real_mkdir = os.mkdir

            def flaky_mkdir(path, *a, **kw):
                nonlocal call_count
                call_count += 1
                real_mkdir(path, *a, **kw)
                if call_count == 1:
                    # Simulate lockdir disappearing during pid write
                    shutil.rmtree(path)
                    raise FileNotFoundError("lockdir vanished")

            with patch("os.mkdir", side_effect=flaky_mkdir):
                lock.acquire()
            assert os.path.isdir(lock.lockdir)
            lock.release()

    def test_acquire_filenotfounderror_max_retries(self):
        """RuntimeError after 3 failed attempts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)

            real_mkdir = os.mkdir

            def always_vanish(path, *a, **kw):
                real_mkdir(path, *a, **kw)
                raise FileNotFoundError("lockdir vanished")

            with patch("os.mkdir", side_effect=always_vanish):
                with pytest.raises(RuntimeError, match="Failed to acquire lock after 3 attempts"):
                    lock.acquire()

    def test_release_oserror_verbose(self, capsys):
        """Release OSError prints warning when verbose >= 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = LockdirLock(target, args)
            # Don't actually acquire - lockdir doesn't exist, release should handle it
            lock.release()
            # rmdir on non-existent dir raises OSError, caught with verbose warning
            assert "Failed to release lock" in capsys.readouterr().err

    def test_get_lock_age_seconds(self):
        """_get_lock_age_seconds delegates to lock_utils."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            age = lock._get_lock_age_seconds()
            assert age >= 0
            os.rmdir(lock.lockdir)


class TestFlockLock:
    """Test FlockLock edge cases."""

    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            lock.release()

    def test_flock_oserror_falls_back_to_polling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            # Mock flock to raise OSError
            with patch("compiletools.locking.fcntl") as mock_fcntl:
                mock_fcntl.flock.side_effect = OSError("flock failed")
                mock_fcntl.LOCK_EX = 2
                mock_fcntl.LOCK_UN = 8
                lock.acquire()
                assert lock.use_flock is False
            lock.release()


class TestCIFSLock:
    """Test CIFSLock."""

    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            lock.acquire()
            assert os.path.exists(lock.lockfile_excl)
            lock.release()
            assert not os.path.exists(lock.lockfile_excl)
            assert not os.path.exists(lock.lockfile)

    def test_acquire_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            lock.acquire()
            lock.release()

    def test_release_oserror_verbose(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = CIFSLock(target, args)
            lock.fd = None
            # Release without acquire - should handle gracefully
            with patch("os.path.exists", return_value=True), \
                 patch("os.unlink", side_effect=OSError("fail")):
                lock.release()
            assert "Failed to release CIFS lock" in capsys.readouterr().err


class TestFlockLockRelease:
    """Additional FlockLock tests."""

    def test_release_fallback_path(self):
        """Release via fallback (non-flock) path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            # Simulate fallback mode
            lock.use_flock = False
            lock.fd = os.open(lock.lockfile, os.O_CREAT | os.O_WRONLY, 0o666)
            # Create pid file
            with open(lock.lockfile_pid, "w") as f:
                f.write("123\n")
            lock.release()
            assert not os.path.exists(lock.lockfile_pid)

    def test_release_oserror_verbose(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = FlockLock(target, args)
            lock.use_flock = True
            lock.fd = None
            with patch("os.path.exists", return_value=True), \
                 patch("os.unlink", side_effect=OSError("fail")):
                lock.release()
            assert "Failed to release flock" in capsys.readouterr().err

    def test_acquire_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            lock.release()


class TestFileLock:
    """Test FileLock context manager."""

    def test_no_lock_when_shared_objects_disabled(self):
        args = _make_lock_args(shared_objects=False)
        lock = FileLock("/some/file.o", args)
        assert lock.lock is None

    def test_context_manager_no_shared_objects(self):
        args = _make_lock_args(shared_objects=False)
        with FileLock("/some/file.o", args):
            pass  # Should not crash

    def test_detection_error_defaults_to_flock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=3)
            with patch("compiletools.filesystem_utils.get_filesystem_type", side_effect=RuntimeError("fail")):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, FlockLock)

    def test_unknown_fs_defaults_to_flock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="unknown"), \
                 patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, FlockLock)

    def test_lockdir_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"), \
                 patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, LockdirLock)

    def test_cifs_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="cifs"), \
                 patch("compiletools.filesystem_utils.get_lock_strategy", return_value="cifs"):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, CIFSLock)

    def test_context_manager_acquires_and_releases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with FileLock(target, args) as fl:
                # Lock should be acquired
                assert fl.lock is not None

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = FileLock(target, args)
            assert lock.lock is not None
