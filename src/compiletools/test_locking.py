"""Unit tests for locking.py."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import compiletools.apptools
from compiletools.locking import CIFSLock, FcntlLock, FileLock, FlockLock, LockdirLock, atomic_compile, atomic_link
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

    def test_pid_file_includes_process_start_time(self):
        """Regression: pid file format must be host:pid:starttime so we
        can detect PID reuse on busy build hosts."""
        import psutil

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            lock.acquire()
            try:
                with open(lock.pid_file) as f:
                    content = f.read().strip()
                parts = content.split(":")
                assert len(parts) == 3, f"Expected host:pid:starttime, got {content!r}"
                host, pid_str, start_str = parts
                # Issue #6: hostname is FQDN (with gethostname fallback)
                assert host == (socket.getfqdn() or socket.gethostname())
                assert int(pid_str) == os.getpid()
                # start_time should match psutil's view of our process
                expected = psutil.Process(os.getpid()).create_time()
                # Allow tiny float tolerance
                assert abs(float(start_str) - expected) < 1.0, (
                    f"start_time {start_str} does not match psutil create_time {expected}"
                )
            finally:
                lock.release()

    def test_stale_detection_rejects_pid_reuse(self):
        """If the pid in the file matches a live process but with a
        different start_time, the lock is stale (PID reuse)."""
        import psutil

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)

            os.mkdir(lock.lockdir)
            os.chmod(lock.lockdir, 0o775)
            # Write pid file with current pid but a wildly different start_time
            real_start = psutil.Process(os.getpid()).create_time()
            fake_start = real_start - 10000.0  # 10000s earlier — clearly different
            with open(lock.pid_file, "w") as f:
                f.write(f"{lock.hostname}:{os.getpid()}:{fake_start}\n")

            assert lock._is_lock_stale() is True, "PID reuse must be detected via start_time mismatch"

    def test_stale_detection_legacy_format_falls_back_to_pid_only(self):
        """Old-format pid files (host:pid, no start_time) keep working —
        we fall back to pid-existence check, matching pre-fix behavior."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            os.mkdir(lock.lockdir)
            os.chmod(lock.lockdir, 0o775)
            # Old format
            with open(lock.pid_file, "w") as f:
                f.write(f"{lock.hostname}:{os.getpid()}\n")

            # Our pid is alive — legacy file accepted as ACTIVE
            assert lock._is_lock_stale() is False

    def test_pid_write_does_not_resurrect_torn_down_lockdir(self):
        """C3 regression: if a peer tears down our lockdir between our mkdir
        and our pid-file write, the pid write must fail (so we retry the
        whole acquire) rather than silently re-creating the lockdir via
        os.makedirs and writing a pid file into a directory nobody owns."""
        import unittest.mock as _mock

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)

            real_mkdir = os.mkdir
            sabotaged = {"done": False}

            def racy_mkdir(path, mode=0o777):
                result = real_mkdir(path, mode)
                # First call only: tear down the lockdir we just created
                # to simulate a peer's concurrent rmtree.
                if not sabotaged["done"] and path == lock.lockdir:
                    sabotaged["done"] = True
                    shutil.rmtree(path)
                return result

            real_makedirs = os.makedirs
            makedirs_paths = []

            def tracking_makedirs(path, *a, **kw):
                makedirs_paths.append(path)
                return real_makedirs(path, *a, **kw)

            # Limit the retry loop so the test cannot run forever if the
            # acquire happens to keep racing.
            with (
                _mock.patch("os.mkdir", side_effect=racy_mkdir),
                _mock.patch("os.makedirs", side_effect=tracking_makedirs),
            ):
                try:
                    lock.acquire()
                except Exception:
                    pass
                finally:
                    try:
                        lock.release()
                    except Exception:
                        pass

            for path in makedirs_paths:
                assert path != lock.lockdir, (
                    f"os.makedirs({path!r}) called to resurrect the torn-down "
                    "lockdir — pid write must use plain open+rename inside the "
                    "lockdir, not a makedirs-bearing helper."
                )

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

    def test_hostname_uses_fqdn(self, monkeypatch):
        """Issue #6: multi-interface hosts get consistent identity via FQDN
        rather than gethostname() (which can return per-interface aliases)."""
        monkeypatch.setattr(socket, "getfqdn", lambda *a, **kw: "node01.cluster.example.com")
        monkeypatch.setattr(socket, "gethostname", lambda: "node01.eth0")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            assert lock.hostname == "node01.cluster.example.com"

    def test_hostname_falls_back_to_gethostname_when_fqdn_empty(self, monkeypatch):
        """If getfqdn returns empty string we fall back to gethostname."""
        monkeypatch.setattr(socket, "getfqdn", lambda *a, **kw: "")
        monkeypatch.setattr(socket, "gethostname", lambda: "node01.eth0")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = LockdirLock(target, args)
            assert lock.hostname == "node01.eth0"


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

    def test_locks_sidecar_not_target(self):
        """FcntlLock.lockfile should be ``<target>.lock`` sidecar, never the
        target itself. Locking the target directly creates an empty target
        file at acquire-time which fools peer make's mtime check into
        skipping the compile recipe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            assert lock.lockfile == os.path.realpath(target) + ".lock"

    def test_fcntl_direct_compile_true(self):
        """FcntlLock should have direct_compile = True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            assert lock.direct_compile is True

    def test_acquire_does_not_create_target(self):
        """Acquire must NOT create the target file. Peer make uses target
        mtime to decide whether to recompile; an empty target file with
        fresh mtime tricks it into skipping compile and linking empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)
            lock.acquire()
            try:
                assert not os.path.exists(target), (
                    "FcntlLock.acquire created the build target — peer make "
                    "will treat the empty file as up-to-date and skip compile"
                )
                assert os.path.exists(target + ".lock")
            finally:
                lock.release()

    def test_acquire_sets_0o666_regardless_of_umask(self):
        """Issue #2 regression: lock file must be group/other writable so a
        second user can reopen+lock the same inode. With umask 0o022 the
        os.open(..., 0o666) yields a 0o644 file unless we explicitly fchmod."""
        import stat as _stat

        old_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "test.o")
                args = _make_lock_args()
                lock = FcntlLock(target, args)
                lock.acquire()
                try:
                    mode = _stat.S_IMODE(os.stat(lock.lockfile).st_mode)
                    assert mode == 0o666, f"Expected 0o666, got {oct(mode)}"
                finally:
                    lock.release()
        finally:
            os.umask(old_umask)


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

    def test_flock_locks_sidecar_not_target(self):
        """FlockLock.lockfile should be ``<target>.lock`` sidecar, never the
        target itself. See FcntlLock.test_locks_sidecar_not_target for why."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            assert lock.lockfile == os.path.realpath(target) + ".lock"

    def test_flock_acquire_does_not_create_target(self):
        """Acquire must NOT create the target file. Peer make uses target
        mtime to decide whether to recompile; an empty target file with
        fresh mtime tricks it into skipping compile and linking empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            lock.acquire()
            try:
                assert not os.path.exists(target), (
                    "FlockLock.acquire created the build target — peer make "
                    "will treat the empty file as up-to-date and skip compile"
                )
                assert os.path.exists(target + ".lock")
            finally:
                lock.release()

    def test_acquire_sets_0o666_regardless_of_umask(self):
        """Issue #2 regression: same as FcntlLock — defeat umask so a
        second user can reopen+lock the same inode."""
        import stat as _stat

        old_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "test.o")
                args = _make_lock_args()
                lock = FlockLock(target, args)
                lock.acquire()
                try:
                    mode = _stat.S_IMODE(os.stat(lock.lockfile).st_mode)
                    assert mode == 0o666, f"Expected 0o666, got {oct(mode)}"
                finally:
                    lock.release()
        finally:
            os.umask(old_umask)


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
            # Issue #3: base lockfile is intentionally left behind so a peer
            # who legitimately recreates lockfile_excl during our release
            # window does not have its base file deleted underneath it.
            assert os.path.exists(lock.lockfile)

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
            with patch("os.path.exists", return_value=True), patch("os.unlink", side_effect=OSError("fail")):
                lock.release()
            assert "Failed to release CIFS lock" in capsys.readouterr().err

    def test_excl_holder_format_is_host_pid_starttime(self):
        """Issue #4 prerequisite: excl file carries host:pid:start_time so
        peers can detect dead local holders."""
        import psutil

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            lock.acquire()
            try:
                with open(lock.lockfile_excl) as f:
                    content = f.read().strip()
                parts = content.split(":")
                assert len(parts) == 3, f"Expected host:pid:starttime, got {content!r}"
                host, pid_str, st_str = parts
                # host is FQDN (or gethostname fallback) — match the same
                assert host == (socket.getfqdn() or socket.gethostname())
                assert int(pid_str) == os.getpid()
                expected = psutil.Process(os.getpid()).create_time()
                assert abs(float(st_str) - expected) < 1.0
            finally:
                lock.release()

    def test_acquire_removes_dead_local_holder(self):
        """Issue #4: a killed peer's lockfile_excl is removed and acquisition
        proceeds rather than deadlocking forever."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            # Plant a stale lockfile_excl owned by a dead local PID.
            os.makedirs(os.path.dirname(lock.lockfile_excl) or ".", exist_ok=True)
            with open(lock.lockfile_excl, "w") as f:
                f.write(f"{lock.hostname}:99999999\n")
            # Should not block — stale removal kicks in.
            lock.acquire()
            try:
                assert os.path.exists(lock.lockfile_excl)
            finally:
                lock.release()

    def test_acquire_does_not_remove_live_local_holder(self):
        """Live local holder must NOT be cleared — that would clobber a
        legitimate concurrent compile. Verified directly via _is_excl_stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            holder = CIFSLock(target, args)
            holder.acquire()
            try:
                # The holder's own pid/start_time is recorded in lockfile_excl.
                # A peer probing _is_excl_stale must see the holder as ACTIVE.
                peer = CIFSLock(target, args)
                assert peer._is_excl_stale() is False
            finally:
                holder.release()

    def test_acquire_does_not_remove_cross_host_holder(self):
        """Cross-host holders cannot be verified; we must not evict them."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args(sleep_interval_cifs=0.01, lock_cross_host_timeout=600)
            lock = CIFSLock(target, args)
            with open(lock.lockfile_excl, "w") as f:
                f.write("some.other.host.example.com:12345:1.0\n")
            # is_excl_stale should be False (cross-host)
            assert lock._is_excl_stale() is False

    def test_release_does_not_unlink_base_lockfile(self):
        """Issue #3: release must leave self.lockfile in place so a peer who
        recreates lockfile_excl during our release window doesn't have its
        base file deleted underneath it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            lock.acquire()
            assert os.path.exists(lock.lockfile)
            lock.release()
            assert os.path.exists(lock.lockfile), (
                "Base lockfile must persist; deleting it races with a peer "
                "that has just (legitimately) recreated lockfile_excl."
            )


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

    @staticmethod
    def _patch_runner(returncode=0, on_run=None):
        """Patch _run_with_signal_forwarding (the new boundary atomic_compile
        and atomic_link delegate to) with a mock. on_run is called with the
        cmd list before returning so tests can simulate side effects (like
        creating the -o output file). Returns the patcher's mock object."""
        from unittest.mock import MagicMock

        mock = MagicMock()

        def fake_run(cmd):
            if on_run is not None:
                on_run(cmd)
            return subprocess.CompletedProcess(cmd, returncode, None, None)

        mock.side_effect = fake_run
        return patch("compiletools.locking._run_with_signal_forwarding", new=mock), mock

    @requires_functional_compiler
    def test_atomic_compile_direct_uses_temp(self):
        """FcntlLock (direct_compile=True) STILL routes through a temp file
        and renames: prevents a peer linker from reading a half-written .o
        while a compile is in progress (no read-side lock)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                open(out, "w").close()

            patcher, mock_run = self._patch_runner(on_run=create_temp)
            with patcher:
                atomic_compile(lock, target, self._compile_cmd())
                call_args = mock_run.call_args[0][0]
                assert call_args[-2] == "-o"
                assert call_args[-1].endswith(".tmp"), f"compiler -o should be temp path, got {call_args[-1]}"
                assert call_args[-1] != target

            assert os.path.exists(target)
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f

    @requires_functional_compiler
    def test_atomic_compile_direct_calls_rename(self):
        """FcntlLock (direct_compile=True) calls os.replace(temp, target)
        after a successful compile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                open(out, "w").close()

            patcher, _ = self._patch_runner(on_run=create_temp)
            with patcher, patch("os.replace") as mock_replace:
                atomic_compile(lock, target, self._compile_cmd())
                mock_replace.assert_called_once()
                args_passed = mock_replace.call_args[0]
                assert args_passed[0].endswith(".tmp")
                assert args_passed[1] == target

    @requires_functional_compiler
    def test_atomic_compile_direct_failure_cleans_temp_no_rename(self):
        """FcntlLock (direct_compile=True): on compiler failure, the temp
        file is removed and os.replace is NOT called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                with open(out, "w") as f:
                    f.write("partial")

            patcher, _ = self._patch_runner(returncode=1, on_run=create_temp)
            with patcher, patch("os.replace") as mock_replace, pytest.raises(subprocess.CalledProcessError):
                atomic_compile(lock, target, self._compile_cmd())
            mock_replace.assert_not_called()
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f, f"Stale temp file found: {f}"

    @requires_functional_compiler
    def test_atomic_compile_indirect_uses_temp(self):
        """CIFSLock (direct_compile=False): compiler gets -o *.tmp, rename IS called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                open(out, "w").close()

            patcher, mock_run = self._patch_runner(on_run=create_temp)
            with patcher:
                atomic_compile(lock, target, self._compile_cmd())
                call_args = mock_run.call_args[0][0]
                assert call_args[-2] == "-o"
                assert call_args[-1].endswith(".tmp")

            assert os.path.exists(target)
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f

    @requires_functional_compiler
    def test_atomic_compile_direct_failure_releases_lock(self):
        """FcntlLock (direct_compile=True): lock is released on compiler failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FcntlLock(target, args)

            patcher, _ = self._patch_runner(returncode=1)
            with patcher, pytest.raises(subprocess.CalledProcessError):
                atomic_compile(lock, target, self._compile_cmd())

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

            patcher, _ = self._patch_runner(returncode=1)
            with patcher, pytest.raises(subprocess.CalledProcessError):
                atomic_compile(lock, target, self._compile_cmd())

            for f in os.listdir(tmpdir):
                assert ".tmp" not in f, f"Stale temp file found: {f}"

    @requires_functional_compiler
    def test_atomic_compile_indirect_rename_failure_cleans_temp(self):
        """If os.replace fails, temp file is cleaned up and lock is released."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                with open(out, "w") as f:
                    f.write("fake")

            patcher, _ = self._patch_runner(on_run=create_temp)
            with (
                patcher,
                patch("os.replace", side_effect=OSError("cross-device")),
                pytest.raises(OSError, match="cross-device"),
            ):
                atomic_compile(lock, target, self._compile_cmd())

            for f in os.listdir(tmpdir):
                assert ".tmp" not in f, f"Stale temp file found: {f}"

    @requires_functional_compiler
    def test_atomic_compile_replaces_existing_target_in_subdir(self):
        """Issue #1 regression: target inside a subdirectory with an existing
        file is replaced atomically via os.replace. Exercises the typical
        case (target lives in an obj subdir, previous .o already exists)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "objs", "deep")
            os.makedirs(subdir)
            target = os.path.join(subdir, "test.o")
            with open(target, "w") as f:
                f.write("OLD")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            def create_temp(cmd):
                out = cmd[cmd.index("-o") + 1]
                with open(out, "w") as f:
                    f.write("NEW")

            patcher, _ = self._patch_runner(on_run=create_temp)
            with patcher:
                atomic_compile(lock, target, self._compile_cmd())

            with open(target) as f:
                assert f.read() == "NEW"
            for f in os.listdir(subdir):
                assert ".tmp" not in f


class TestAtomicLink:
    """Test atomic_link with temp-then-rename semantics (Critical bug C2)."""

    @staticmethod
    def _patch_runner(returncode=0, on_run=None):
        from unittest.mock import MagicMock

        mock = MagicMock()

        def fake_run(cmd):
            if on_run is not None:
                on_run(cmd)
            return subprocess.CompletedProcess(cmd, returncode, None, None)

        mock.side_effect = fake_run
        return patch("compiletools.locking._run_with_signal_forwarding", new=mock), mock

    def test_atomic_link_runs_command_under_lock(self):
        """Lock is acquired before command runs and released after."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            call_order = []
            orig_acquire = lock.acquire
            orig_release = lock.release

            def tracking_acquire():
                call_order.append("acquire")
                return orig_acquire()

            def tracking_release():
                call_order.append("release")
                return orig_release()

            lock.acquire = tracking_acquire
            lock.release = tracking_release

            def on_run(cmd):
                call_order.append("run")
                # Simulate ar producing the temp archive
                tmp = cmd[cmd.index("rcs") + 1]
                open(tmp, "w").close()

            patcher, _ = self._patch_runner(on_run=on_run)
            with patcher:
                atomic_link(lock, target, ["ar", "rcs", target, "foo.o"])

            assert call_order == ["acquire", "run", "release"]

    def test_atomic_link_writes_to_temp_then_renames(self):
        """atomic_link writes to a .tmp file and renames to target — never
        leaves a partial archive on the path another process is reading."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            args = _make_lock_args()
            lock = CIFSLock(target, args)

            def fake_ar(cmd):
                # The temp path appears where the target used to be
                tmp = cmd[2]
                assert tmp.endswith(".tmp"), f"ar should be told to write the .tmp path, got {tmp!r}"
                open(tmp, "w").close()

            patcher, mock_run = self._patch_runner(on_run=fake_ar)
            with patcher:
                atomic_link(lock, target, ["ar", "rcs", target, "foo.o"])
                rewritten = mock_run.call_args[0][0]
                assert rewritten[0] == "ar"
                assert rewritten[1] == "rcs"
                assert rewritten[2].endswith(".tmp")
                assert rewritten[3] == "foo.o"

            assert os.path.exists(target)
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f

    def test_atomic_link_returns_zero_on_success(self):
        """Successful link returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            def on_run(cmd):
                tmp = cmd[2]
                open(tmp, "w").close()

            patcher, _ = self._patch_runner(on_run=on_run)
            with patcher:
                result = atomic_link(lock, target, ["ar", "rcs", target, "foo.o"])
                assert result == 0

    def test_atomic_link_raises_on_failure(self):
        """Failed link raises CalledProcessError and releases lock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            patcher, _ = self._patch_runner(returncode=1)
            with patcher, pytest.raises(subprocess.CalledProcessError) as exc_info:
                atomic_link(lock, target, ["ar", "rcs", target, "foo.o"])
            assert exc_info.value.returncode == 1

            lock2 = FlockLock(target, args)
            lock2.acquire()
            lock2.release()

    def test_atomic_link_no_torn_target_when_link_fails(self):
        """If the linker dies, the target is NOT replaced — peers see the
        last good artifact (or nothing), never a partial archive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            with open(target, "w") as f:
                f.write("LAST_GOOD_CONTENT")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            def fake_partial_then_fail(cmd):
                # Linker writes a partial output to temp, then fails
                tmp = cmd[2]
                with open(tmp, "w") as f:
                    f.write("PARTIAL_GARBAGE")

            patcher, _ = self._patch_runner(returncode=1, on_run=fake_partial_then_fail)
            with patcher, pytest.raises(subprocess.CalledProcessError):
                atomic_link(lock, target, ["ar", "rcs", target, "foo.o"])

            # Target retains the last good content; partial garbage is gone
            with open(target) as f:
                assert f.read() == "LAST_GOOD_CONTENT"
            for f in os.listdir(tmpdir):
                assert ".tmp" not in f

    def test_atomic_link_ld_o_form_uses_temp(self):
        """ld/cc -o form: the path after -o is rewritten to the .tmp path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "myexe")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            captured = {}

            def fake_ld(cmd):
                captured["cmd"] = list(cmd)
                tmp = cmd[cmd.index("-o") + 1]
                open(tmp, "w").close()

            patcher, _ = self._patch_runner(on_run=fake_ld)
            with patcher:
                atomic_link(lock, target, ["c++", "foo.o", "bar.o", "-o", target])

            assert captured["cmd"][captured["cmd"].index("-o") + 1].endswith(".tmp")
            assert os.path.exists(target)

    def test_atomic_link_ar_append_seeds_temp_with_existing_archive(self):
        """ar with mutating mode (r/q/m) seeds the temp file with the
        existing archive content so the append operates as intended."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            with open(target, "w") as f:
                f.write("EXISTING_ARCHIVE")
            args = _make_lock_args()
            lock = FlockLock(target, args)

            seen_temp_content = {}

            def fake_ar_append(cmd):
                tmp = cmd[2]
                # ar would read the existing content and append; we just
                # observe whether it was seeded
                if os.path.exists(tmp):
                    with open(tmp) as f:
                        seen_temp_content["content"] = f.read()
                # Pretend ar updated it
                with open(tmp, "w") as f:
                    f.write("APPENDED")

            patcher, _ = self._patch_runner(on_run=fake_ar_append)
            with patcher:
                atomic_link(lock, target, ["ar", "rcs", target, "extra.o"])

            assert seen_temp_content.get("content") == "EXISTING_ARCHIVE"

    def test_atomic_link_warns_when_target_not_found_in_cmd(self, capsys):
        """Issue #7: when the link command does not contain the target in a
        recognised form, atomic_link can't do temp+rename. The user must be
        told (verbose >= 2) so they can diagnose torn-binary races."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.bin")
            args = _make_lock_args(verbose=2)
            lock = FlockLock(target, args)

            # Custom linker invocation that does not match -o or ar shapes
            # (target nowhere in the command).
            patcher, _ = self._patch_runner()
            with patcher:
                atomic_link(lock, target, ["custom-linker", "--out-magic-flag", "/somewhere/else"])

            err = capsys.readouterr().err
            assert "atomic_link could not find target" in err
            assert "no temp+rename atomicity" in err

    def test_atomic_link_no_warning_when_rewrite_succeeds(self, capsys):
        """Sanity: when -o target is present, no warning is emitted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.bin")
            args = _make_lock_args(verbose=2)
            lock = FlockLock(target, args)

            def on_run(cmd):
                tmp = cmd[cmd.index("-o") + 1]
                open(tmp, "w").close()

            patcher, _ = self._patch_runner(on_run=on_run)
            with patcher:
                atomic_link(lock, target, ["c++", "-o", target, "foo.o"])

            err = capsys.readouterr().err
            assert "atomic_link could not find target" not in err

    def test_atomic_link_skips_seed_for_empty_target(self):
        """An empty (0-byte) target is the lock-file artifact left by
        FlockLock/FcntlLock O_CREAT, not a real archive. atomic_link must
        NOT seed the temp file from it (ar would fail with
        'File format not recognized')."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            # Pre-create empty target (the FlockLock O_CREAT artifact)
            open(target, "w").close()
            assert os.path.getsize(target) == 0
            args = _make_lock_args()
            lock = FlockLock(target, args)

            seen_temp_state = {}

            def fake_ar_append(cmd):
                tmp = cmd[2]
                seen_temp_state["existed_before_ar"] = os.path.exists(tmp)
                # Pretend ar created a fresh archive
                with open(tmp, "w") as f:
                    f.write("FRESH_ARCHIVE")

            patcher, _ = self._patch_runner(on_run=fake_ar_append)
            with patcher:
                atomic_link(lock, target, ["ar", "rcs", target, "extra.o"])

            # The temp file should NOT have been pre-seeded from the empty target
            assert seen_temp_state.get("existed_before_ar") is False
            with open(target) as f:
                assert f.read() == "FRESH_ARCHIVE"


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
            with (
                patch("compiletools.filesystem_utils.get_filesystem_type", return_value="unknown"),
                patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"),
            ):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, FlockLock)

    def test_lockdir_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with (
                patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"),
                patch("compiletools.filesystem_utils.get_lock_strategy", return_value="lockdir"),
            ):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, LockdirLock)

    def test_fcntl_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with (
                patch("compiletools.filesystem_utils.get_filesystem_type", return_value="gpfs"),
                patch("compiletools.filesystem_utils.get_lock_strategy", return_value="fcntl"),
            ):
                lock = FileLock(target, args)
                assert isinstance(lock.lock, FcntlLock)

    def test_cifs_strategy_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            with (
                patch("compiletools.filesystem_utils.get_filesystem_type", return_value="cifs"),
                patch("compiletools.filesystem_utils.get_lock_strategy", return_value="cifs"),
            ):
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


class TestPidReuseTolerance:
    """Issue #5: tolerance for psutil create_time mismatch must be tight on
    Linux (0.1s) where /proc/[pid]/stat is fine-grained, looser elsewhere."""

    def test_tolerance_constant_linux_is_0_1s(self):
        import sys as _sys

        from compiletools.lock_utils import _PID_REUSE_TOLERANCE_SECONDS

        if _sys.platform.startswith("linux"):
            assert _PID_REUSE_TOLERANCE_SECONDS == 0.1
        else:
            assert _PID_REUSE_TOLERANCE_SECONDS == 1.0

    def test_pid_reuse_within_loose_window_is_detected_on_linux(self):
        """A simulated PID reuse where the impostor's start_time is 0.5s
        from the recorded holder must be flagged as STALE on Linux. The
        old 1.0s tolerance would have wrongly marked it ACTIVE."""
        import sys as _sys

        if not _sys.platform.startswith("linux"):
            pytest.skip("Tighter tolerance is Linux-only")

        import psutil

        from compiletools.lock_utils import is_process_alive_local

        recorded_start = psutil.Process(os.getpid()).create_time()
        # Pretend the recorded holder started 0.5s before the live process
        # (i.e. live process is a PID-reuse impostor at 0.5s offset).
        impostor_start = recorded_start - 0.5
        assert is_process_alive_local(os.getpid(), impostor_start) is False, (
            "0.5s mismatch must be flagged as stale on Linux (tolerance 0.1s)"
        )

    def test_exact_match_still_alive(self):
        """Sanity: matching start_time still resolves to ACTIVE."""
        import psutil

        from compiletools.lock_utils import is_process_alive_local

        st = psutil.Process(os.getpid()).create_time()
        assert is_process_alive_local(os.getpid(), st) is True


class TestSubprocessSafety:
    """Verify atomic_compile/atomic_link spawn children in a new session and
    forward SIGINT/SIGTERM so the lock is never released while a child is
    still writing to the target. (Critical bug C1.)"""

    @staticmethod
    def _make_popen_mock(mock_popen, write_output=False):
        """Configure a mock subprocess.Popen so it behaves enough like a real
        Popen for our wrappers. Optionally writes the -o output file when
        wait() is called (mimics a successful compile)."""
        proc = mock_popen.return_value
        proc.returncode = 0
        proc.poll.return_value = 0

        def fake_wait(timeout=None):
            if write_output and mock_popen.call_args is not None:
                cmd = mock_popen.call_args.args[0] if mock_popen.call_args.args else []
                if "-o" in cmd:
                    out = cmd[cmd.index("-o") + 1]
                    try:
                        open(out, "w").close()
                    except OSError:
                        pass
            return 0

        proc.wait.side_effect = fake_wait
        proc.communicate.return_value = (b"", b"")
        proc.__enter__ = lambda self_: self_
        proc.__exit__ = lambda self_, *a: False
        return proc

    def _assert_popen_used_with_new_session(self, callable_under_test):
        """Run callable; assert subprocess.Popen was called with
        start_new_session=True. Side effects after Popen are tolerated."""
        with patch("subprocess.Popen") as mock_popen:
            self._make_popen_mock(mock_popen, write_output=True)
            try:
                callable_under_test()
            except Exception:
                pass
            assert mock_popen.called, "subprocess.Popen was never called"
            # Find the call where the actual command (not stdout/stderr setup)
            # was passed. We only care that AT LEAST ONE Popen call used
            # start_new_session=True (in case of internal helpers).
            for call in mock_popen.call_args_list:
                if call.kwargs.get("start_new_session") is True:
                    return
            raise AssertionError(
                f"subprocess.Popen never called with start_new_session=True. "
                f"Calls: {[c.kwargs for c in mock_popen.call_args_list]}"
            )

    def test_atomic_compile_indirect_starts_new_session(self):
        """Indirect compile must use start_new_session=True so signals can be
        forwarded to the child's process group."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = CIFSLock(target, args)
            self._assert_popen_used_with_new_session(lambda: atomic_compile(lock, target, ["c++", "-c", "test.c"]))

    def test_atomic_compile_direct_starts_new_session(self):
        """Direct compile path must also use start_new_session=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.o")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            self._assert_popen_used_with_new_session(lambda: atomic_compile(lock, target, ["c++", "-c", "test.c"]))

    def test_atomic_link_starts_new_session(self):
        """atomic_link must use start_new_session=True for signal forwarding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test.a")
            args = _make_lock_args()
            lock = FlockLock(target, args)
            self._assert_popen_used_with_new_session(lambda: atomic_link(lock, target, ["ar", "rcs", target, "foo.o"]))

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX-only signal forwarding")
    def test_sigterm_during_atomic_compile_is_forwarded_to_child_group(self, tmp_path):
        """End-to-end: spawn a worker that holds a lock and runs a child shell
        that traps SIGTERM. After SIGTERM-ing the worker, the trap-marker file
        must appear (proving the child received TERM via process-group
        forwarding), and the done-marker must NOT appear (proving the child
        did not run to completion as an orphan)."""
        import signal as _signal
        import textwrap
        import time as _time

        target = tmp_path / "test.o"
        worker_script = tmp_path / "worker.py"
        ready_marker = tmp_path / "READY"
        trap_marker = tmp_path / "TRAPPED"
        done_marker = tmp_path / "DONE"

        repo_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        worker_script.write_text(
            textwrap.dedent(f"""
            import os, sys, pathlib
            sys.path.insert(0, {repo_src!r})
            from types import SimpleNamespace
            from compiletools.locking import FlockLock, atomic_compile

            args = SimpleNamespace(
                verbose=0, file_locking=True,
                lock_cross_host_timeout=300, lock_warn_interval=30,
                lock_creation_grace_period=2,
                sleep_interval_lockdir=0.01, sleep_interval_cifs=0.01,
                sleep_interval_flock_fallback=0.01,
            )
            target = {str(target)!r}
            lock = FlockLock(target, args)
            pathlib.Path({str(ready_marker)!r}).touch()
            try:
                atomic_compile(lock, target, [
                    'sh', '-c',
                    'trap "touch {trap_marker}; exit 143" TERM; '
                    'sleep 5; touch {done_marker}',
                ])
            except SystemExit:
                raise
            except Exception:
                pass
        """)
        )

        proc = subprocess.Popen(
            [sys.executable, str(worker_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = _time.time() + 15
            while not ready_marker.exists() and _time.time() < deadline:
                _time.sleep(0.05)
            assert ready_marker.exists(), "Worker never reached ready state"
            _time.sleep(0.5)

            proc.send_signal(_signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Hard cleanup of worker AND any orphan children
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait()
                pytest.fail("Worker did not exit promptly after SIGTERM")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait()

        # Wait briefly for orphan child (if any) to either write the marker or
        # not — long enough that DONE_MARKER would be created if the bug exists
        # (sleep 5 in the child) but bounded so the test is fast.
        _time.sleep(6.0)

        assert trap_marker.exists(), (
            "Child shell never received SIGTERM — signal was not forwarded to the child process group"
        )
        assert not done_marker.exists(), (
            "Child shell ran to completion as an orphan — worker exited without killing its child"
        )

        # Lock should be released — verify by acquiring it ourselves.
        verify_args = _make_lock_args()
        lock2 = FlockLock(str(target), verify_args)
        lock2.acquire()
        lock2.release()
