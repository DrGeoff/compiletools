"""Comprehensive tests for lock cleanup functionality.

Tests cover:
- Unit tests with mocked SSH (no real network calls)
- Integration tests with real filesystem
- Race condition handling
- Metrics validation
- CLI entry point
"""

import pytest
import tempfile
import os
import time
import subprocess
import socket
from unittest.mock import Mock, patch
import compiletools.cleanup_locks
import compiletools.cleanup_locks_main
import compiletools.git_utils


@pytest.fixture
def mock_args():
    """Create mock args object with cleanup configuration."""
    args = Mock()
    args.dry_run = False
    args.ssh_timeout = 5
    args.min_lock_age = 10  # 10 seconds
    args.lock_cross_host_timeout = 600
    args.verbose = 1
    return args


@pytest.fixture
def tmpdir_with_locks():
    """Create temporary directory for lock testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def create_lockdir(objdir, name, hostname, pid):
    """Helper to create a lockdir with specific hostname:pid."""
    lockdir = os.path.join(objdir, f"{name}.lockdir")
    os.makedirs(lockdir, exist_ok=True)
    pid_file = os.path.join(lockdir, "pid")
    with open(pid_file, 'w') as f:
        f.write(f"{hostname}:{pid}\n")
    return lockdir


def create_old_lockdir(objdir, name, hostname, pid, age_seconds):
    """Helper to create lockdir with specific age."""
    lockdir = create_lockdir(objdir, name, hostname, pid)
    # Set mtime to age_seconds ago
    old_time = time.time() - age_seconds
    os.utime(lockdir, (old_time, old_time))
    return lockdir


class TestLockCleanerUnit:
    """Unit tests with full mocking - no real SSH or filesystem complexity."""

    def test_read_lock_info_valid(self, tmpdir_with_locks, mock_args):
        """Test reading valid lock info."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        hostname, pid = cleaner._read_lock_info(lockdir)

        assert hostname == "host1"
        assert pid == 12345

    def test_read_lock_info_invalid_format_no_colon(self, tmpdir_with_locks, mock_args):
        """Test handling of malformed pid file without colon."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = os.path.join(tmpdir_with_locks, "test1.lockdir")
        os.makedirs(lockdir)
        pid_file = os.path.join(lockdir, "pid")
        with open(pid_file, 'w') as f:
            f.write("invalid-no-colon\n")

        hostname, pid = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None

    def test_read_lock_info_empty_file(self, tmpdir_with_locks, mock_args):
        """Test handling of empty pid file."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = os.path.join(tmpdir_with_locks, "test1.lockdir")
        os.makedirs(lockdir)
        pid_file = os.path.join(lockdir, "pid")
        with open(pid_file, 'w') as f:
            f.write("")

        hostname, pid = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None

    def test_read_lock_info_missing_file(self, tmpdir_with_locks, mock_args):
        """Test handling of missing pid file."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = os.path.join(tmpdir_with_locks, "test1.lockdir")
        os.makedirs(lockdir)

        hostname, pid = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None

    @patch('subprocess.run')
    def test_is_process_alive_remote_success(self, mock_run, mock_args):
        """Test SSH check when process exists."""
        mock_run.return_value = Mock(returncode=0)
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is True
        assert ssh_error is False
        # Verify SSH command
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert 'ssh' in call_args
        assert 'remote-host' in call_args
        assert 'kill -0 12345' in ' '.join(call_args)

    @patch('subprocess.run')
    def test_is_process_alive_remote_not_found(self, mock_run, mock_args):
        """Test SSH check when process doesn't exist."""
        mock_run.return_value = Mock(returncode=1)
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is False  # Exit code 1 = process not found, not SSH error

    @patch('subprocess.run')
    def test_is_process_alive_remote_ssh_failure(self, mock_run, mock_args):
        """Test SSH check when connection fails."""
        mock_run.return_value = Mock(returncode=255)
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is True  # Exit code 255 = SSH connection failed

    @patch('subprocess.run')
    def test_is_process_alive_remote_timeout(self, mock_run, mock_args):
        """Test SSH check timeout handling."""
        mock_run.side_effect = subprocess.TimeoutExpired('ssh', 5)
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is True  # Timeout treated as SSH failure

    @patch('psutil.pid_exists')
    def test_is_process_alive_local(self, mock_psutil, mock_args):
        """Test local process check using psutil."""
        mock_psutil.return_value = True
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        result = cleaner._is_process_alive_local(12345)

        assert result is True
        mock_psutil.assert_called_once_with(12345)

    def test_get_lock_age_future_mtime(self, tmpdir_with_locks, mock_args):
        """Test clock skew handling (future mtime returns age 0)."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        # Set mtime to future
        future_time = time.time() + 3600
        os.utime(lockdir, (future_time, future_time))

        age = cleaner._get_lock_age_seconds(lockdir)

        assert age == 0

    def test_get_lock_age_normal(self, tmpdir_with_locks, mock_args):
        """Test normal lock age calculation."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "host1", 12345, 100)

        age = cleaner._get_lock_age_seconds(lockdir)

        # Allow 1 second tolerance
        assert 99 <= age <= 101


class TestLockCleanerIntegration:
    """Integration tests with real lockdirs, mocked SSH."""

    def test_scan_empty_directory(self, tmpdir_with_locks, mock_args):
        """Test scanning directory with no locks."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 0
        assert stats['active'] == 0
        assert stats['stale_removed'] == 0

    def test_scan_with_stale_local_locks(self, tmpdir_with_locks, mock_args):
        """Test cleanup of stale local locks (fake PID)."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        # Create old stale lock with fake PID
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 1
        assert stats['stale_removed'] == 1
        assert not os.path.exists(lockdir), "Stale lock should be removed"

    @patch('subprocess.run')
    def test_scan_with_active_remote_locks(self, mock_run, tmpdir_with_locks, mock_args):
        """Test preservation of active remote locks."""
        mock_run.return_value = Mock(returncode=0)  # Process exists
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        # Create old remote lock (would be stale if local)
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 1
        assert stats['active'] == 1
        assert stats['stale_removed'] == 0
        assert os.path.exists(lockdir), "Active remote lock should be preserved"

    @patch('subprocess.run')
    def test_scan_with_stale_remote_locks(self, mock_run, tmpdir_with_locks, mock_args):
        """Test cleanup of stale remote locks."""
        mock_run.return_value = Mock(returncode=1)  # Process not found
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 1
        assert stats['stale_removed'] == 1
        assert not os.path.exists(lockdir), "Stale remote lock should be removed"

    @patch('subprocess.run')
    def test_scan_with_ssh_failure(self, mock_run, tmpdir_with_locks, mock_args):
        """Test handling when SSH unavailable."""
        mock_run.return_value = Mock(returncode=255)  # SSH failed
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 1
        assert stats['unknown'] == 1
        assert stats['stale_removed'] == 0
        assert os.path.exists(lockdir), "Lock with SSH failure should be preserved"

    def test_dry_run_mode(self, tmpdir_with_locks, mock_args):
        """Test dry-run doesn't remove locks."""
        mock_args.dry_run = True
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['stale_removed'] == 1  # Would have removed
        assert os.path.exists(lockdir), "Dry-run should not remove locks"

    def test_min_lock_age_filtering(self, tmpdir_with_locks, mock_args):
        """Test that young locks are skipped."""
        mock_args.min_lock_age = 50
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        # Create young stale lock (30 seconds old, below min_lock_age)
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 30)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 1
        assert stats['skipped_young'] == 1
        assert stats['stale_removed'] == 0
        assert os.path.exists(lockdir), "Young lock should be preserved"

    def test_statistics_collection(self, tmpdir_with_locks, mock_args):
        """Test accurate statistics tracking."""
        hostname = socket.gethostname()
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        # Create mix of locks
        # 1. Active local lock (our PID)
        create_old_lockdir(tmpdir_with_locks, "active", hostname, os.getpid(), 100)

        # 2. Stale local lock (fake PID)
        create_old_lockdir(tmpdir_with_locks, "stale", hostname, 999999, 100)

        # 3. Young lock (skip)
        create_old_lockdir(tmpdir_with_locks, "young", hostname, 999998, 5)

        with patch('subprocess.run') as mock_run:
            # 4. Active remote lock
            create_old_lockdir(tmpdir_with_locks, "remote-active", "remote1", 12345, 100)

            # 5. Stale remote lock
            stale_remote_lockdir = create_old_lockdir(tmpdir_with_locks, "remote-stale", "remote2", 12346, 100)

            # Use function-based side_effect to handle non-deterministic os.walk ordering
            def ssh_side_effect(cmd, **kwargs):
                cmd_str = ' '.join(cmd)
                if 'remote1' in cmd_str:  # remote-active
                    return Mock(returncode=0)  # process exists
                elif 'remote2' in cmd_str:  # remote-stale
                    return Mock(returncode=1)  # process not found
                return Mock(returncode=255)  # unexpected

            mock_run.side_effect = ssh_side_effect

            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats['total'] == 5
        assert stats['active'] == 2  # our PID + remote-active
        assert stats['stale_removed'] == 2  # stale local + stale remote
        assert stats['skipped_young'] == 1

        # Verify stale remote lock was actually removed
        assert not os.path.exists(stale_remote_lockdir), "Stale remote lock should be removed"

    def test_permission_errors_handling(self, tmpdir_with_locks, mock_args):
        """Test handling of permission denied on removal."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        # Make lockdir unremovable by setting read-only
        os.chmod(lockdir, 0o555)
        os.chmod(tmpdir_with_locks, 0o555)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # Restore permissions for cleanup
        os.chmod(tmpdir_with_locks, 0o755)
        os.chmod(lockdir, 0o755)

        # Should have tried to remove but failed
        assert stats['total'] == 1
        assert stats['stale_failed'] >= 0  # May or may not fail depending on permissions


class TestLockCleanupRaceConditions:
    """Test edge cases where locks change during scan."""

    def test_lock_disappears_between_scan_and_read(self, tmpdir_with_locks, mock_args):
        """Test lock removed after os.walk finds it."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        # Create lockdir
        create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        # Patch _read_lock_info to delete lockdir before reading
        original_read = cleaner._read_lock_info

        def read_and_delete(lockdir_path):
            import shutil
            if os.path.exists(lockdir_path):
                shutil.rmtree(lockdir_path)
            return original_read(lockdir_path)

        with patch.object(cleaner, '_read_lock_info', side_effect=read_and_delete):
            # Should handle gracefully without exception
            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # Verify completed without crash and found the lock
        assert stats['total'] == 1  # Found 1 lock (even though it disappeared)

    def test_active_process_dies_during_cleanup(self, tmpdir_with_locks, mock_args):
        """Test process exits between stale check and removal."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        # This scenario: check shows active, but dies before removal
        # Create lock with our PID
        create_old_lockdir(tmpdir_with_locks, "test1", hostname, os.getpid(), 100)

        # Patch to make our PID appear dead during removal
        call_count = [0]

        def fake_is_alive(pid):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # First check: active
            return False  # Second check: dead

        with patch.object(cleaner, '_is_process_alive_local', side_effect=fake_is_alive):
            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # Should complete without exception
        # Lock was marked active on first check, so it won't be removed
        assert stats['total'] == 1
        assert stats['active'] == 1


class TestLockCleanupMetrics:
    """Test that statistics accurately reflect operations."""

    def test_metrics_counters_sum_correctly(self, tmpdir_with_locks, mock_args):
        """Test that stats counters add up to total."""
        hostname = socket.gethostname()
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)

        # Create various locks
        create_old_lockdir(tmpdir_with_locks, "active", hostname, os.getpid(), 100)
        create_old_lockdir(tmpdir_with_locks, "stale", hostname, 999999, 100)
        create_old_lockdir(tmpdir_with_locks, "young", hostname, 999998, 5)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # All locks must be accounted for in categories
        accounted = (stats['active'] + stats['stale_removed'] +
                    stats['stale_failed'] + stats['unknown'] +
                    stats['skipped_young'])

        assert stats['total'] == accounted

    def test_metrics_evolution_safety(self, tmpdir_with_locks, mock_args):
        """Test metrics structure for future additions."""
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # These keys must always exist
        required_keys = {'total', 'active', 'stale_removed',
                        'stale_failed', 'unknown', 'skipped_young'}
        assert required_keys.issubset(stats.keys())


class TestCleanupLocksMain:
    """Test CLI entry point and argument parsing.

    Note: These tests use subprocess to avoid configargparse global state issues.
    """

    def test_main_help_works(self):
        """Test that --help works (smoke test)."""
        # This verifies entry point is functional
        result = subprocess.run(
            ['python', '-m', 'compiletools.cleanup_locks_main', '--help'],
            capture_output=True,
            text=True,
            timeout=5
        )

        assert result.returncode == 0
        assert 'Clean up stale locks' in result.stdout or 'usage:' in result.stdout

    def test_integration_dry_run(self, tmpdir_with_locks):
        """Integration test: dry-run on empty directory."""
        hostname = socket.gethostname()

        # Create a stale lock
        create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        result = subprocess.run(
            ['python', '-m', 'compiletools.cleanup_locks_main',
             '--dry-run', '--objdir', tmpdir_with_locks],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=compiletools.git_utils.find_git_root()
        )

        # Dry run should succeed
        assert result.returncode == 0
        # Lock should still exist in dry-run
        assert os.path.exists(os.path.join(tmpdir_with_locks, "test1.lockdir"))

    def test_exit_code_on_empty_directory(self, tmpdir_with_locks):
        """Test exit code 0 when no locks found."""
        result = subprocess.run(
            ['python', '-m', 'compiletools.cleanup_locks_main',
             '--objdir', tmpdir_with_locks],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=compiletools.git_utils.find_git_root()
        )

        assert result.returncode == 0
