"""Unit tests for locking.py."""

import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import compiletools.apptools
from compiletools.locking import CIFSLock, FcntlLock, FileLock, FlockLock, LockdirLock, atomic_compile
from compiletools.testhelper import requires_functional_compiler


def _make_lock_args(**overrides):
    """Create a minimal args object for locking."""
    defaults = dict(
        verbose=0,
        file_locking=True,
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


class TestFcntlLock:
    """Test FcntlLock (fcntl.lockf-based locking for GPFS)."""

    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            lock.acquire()
            assert os.path.exists(lock.lockfile)
            lock.release()
            # Lock file is intentionally NOT removed
            assert os.path.exists(lock.lockfile)

    def test_creates_parent_dir(self):
        """Acquire creates parent directory if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            lock.acquire()
            assert os.path.exists(lock.lockfile)
            lock.release()

    def test_release_error_handled(self, capsys):
        """Release handles errors gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = FcntlLock(target, args)
            lock.fd = None
            # Release without acquire — should not crash
            lock.release()

    def test_locks_target_directly(self):
        """FcntlLock.lockfile should be the target itself (no .lock suffix)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            assert lock.lockfile == os.path.realpath(target)

    def test_fcntl_direct_compile_true(self):
        """FcntlLock should have direct_compile = True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            assert lock.direct_compile is True

    def test_no_sidecar_file(self):
        """Acquiring FcntlLock should NOT create a .lock sidecar file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            lock.acquire()
            try:
                assert not os.path.exists(target + ".lock")
            finally:
                lock.release()


class TestFlockLock:
    """Test FlockLock edge cases."""

    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            lock.release()

    def test_flock_no_fallback_attributes(self):
        """FlockLock should not have O_EXCL fallback attributes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            assert not hasattr(lock, "use_flock")
            assert not hasattr(lock, "lockfile_pid")
            assert not hasattr(lock, "sleep_interval")

    def test_flock_locks_target_directly(self):
        """FlockLock.lockfile should be the target itself (no .lock suffix)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            assert lock.lockfile == os.path.realpath(target)

    def test_flock_no_sidecar_file(self):
        """Acquiring FlockLock should NOT create a .lock sidecar file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            try:
                assert not os.path.exists(target + ".lock")
            finally:
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

    def test_release_without_acquire(self):
        """Release without acquire should not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(verbose=2)
            lock = FlockLock(target, args)
            lock.fd = None
            # Release without acquire — should not crash
            lock.release()

    def test_acquire_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "subdir", "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            lock.release()


class TestDirectCompileProperty:
    """Test that non-fcntl lock classes have direct_compile = False."""

    def test_lockdir_direct_compile_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            assert lock.direct_compile is False

    def test_flock_direct_compile_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            assert lock.direct_compile is True

    def test_cifs_direct_compile_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            assert lock.direct_compile is False


class TestAtomicCompile:
    """Test atomic_compile with direct_compile vs indirect locks."""

    @staticmethod
    def _compile_cmd(source="test.c"):
        """Build a compile command using the detected functional compiler."""
        cxx = compiletools.apptools.get_functional_cxx_compiler() or "c++"
        return [cxx, "-c", source]

    @requires_functional_compiler
    def test_atomic_compile_direct_no_temp(self):
        """FcntlLock (direct_compile=True): compiler gets -o target, no rename."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                )
                atomic_compile(lock, target, self._compile_cmd())

                # Compiler should get -o target directly
                call_args = mock_run.call_args[0][0]
                assert call_args[-2:] == ["-o", target]

            # No temp files should exist
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f

    @requires_functional_compiler
    def test_atomic_compile_direct_no_rename(self):
        """FcntlLock (direct_compile=True): os.rename is NOT called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            with patch("subprocess.run") as mock_run, \
                 patch("os.rename") as mock_rename:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                )
                atomic_compile(lock, target, self._compile_cmd())
                mock_rename.assert_not_called()

    @requires_functional_compiler
    def test_atomic_compile_indirect_uses_temp(self):
        """CIFSLock (direct_compile=False): compiler gets -o *.tmp, rename IS called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            with patch("subprocess.run") as mock_run, \
                 patch("os.rename") as mock_rename:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                )
                atomic_compile(lock, target, self._compile_cmd())

                # Compiler should get -o *.tmp
                call_args = mock_run.call_args[0][0]
                assert call_args[-2] == "-o"
                assert call_args[-1].endswith(".tmp")

                # os.rename should be called
                mock_rename.assert_called_once()

    @requires_functional_compiler
    def test_atomic_compile_direct_failure_releases_lock(self):
        """FcntlLock (direct_compile=True): lock is released on compiler failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="error"
                )
                with pytest.raises(subprocess.CalledProcessError):
                    atomic_compile(lock, target, self._compile_cmd())

            # Lock must be released — verify by acquiring it again
            lock2 = FcntlLock(target, args)
            lock2.acquire()
            lock2.release()

    @requires_functional_compiler
    def test_atomic_compile_indirect_failure_releases_lock_and_cleans_temp(self):
        """CIFSLock (direct_compile=False): lock released and temp cleaned on failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="error"
                )
                with pytest.raises(subprocess.CalledProcessError):
                    atomic_compile(lock, target, self._compile_cmd())

            # No temp files should remain
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f, f"Stale temp file found: {f}"

    @requires_functional_compiler
    def test_atomic_compile_indirect_rename_failure_cleans_temp(self):
        """If os.rename fails, temp file is cleaned up and lock is released."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            with patch("subprocess.run") as mock_run, \
                 patch("os.rename", side_effect=OSError("cross-device")):
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                )
                # Create the temp file that subprocess.run would create
                def create_temp(cmd, **kwargs):
                    output_file = cmd[cmd.index("-o") + 1]
                    with open(output_file, "w") as f:
                        f.write("fake")
                    return subprocess.CompletedProcess(args=cmd, returncode=0)

                mock_run.side_effect = create_temp

                with pytest.raises(OSError, match="cross-device"):
                    atomic_compile(lock, target, self._compile_cmd())

            # No temp files should remain
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f, f"Stale temp file found: {f}"


class TestFileLock:
    """Test FileLock context manager."""

    def test_no_lock_when_file_locking_disabled(self):
        args = _make_lock_args(file_locking=False)
        lock = FileLock("/some/file.o", args)
        assert lock.lock is None

    def test_context_manager_no_file_locking(self):
        args = _make_lock_args(file_locking=False)
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

    def test_fcntl_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="gpfs"), \
                 patch("compiletools.filesystem_utils.get_lock_strategy", return_value="fcntl"):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, FcntlLock)

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
