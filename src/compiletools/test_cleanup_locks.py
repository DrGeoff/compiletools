"""Comprehensive tests for lock cleanup functionality.

Tests cover:
- Unit tests with mocked SSH (no real network calls)
- Integration tests with real filesystem
- Race condition handling
- Metrics validation
- CLI entry point
- Whole-pool --all-variants sweep
"""

import os
import shutil
import socket
import subprocess
import time
from unittest.mock import Mock, patch

import pytest

import compiletools.cleanup_locks
import compiletools.cleanup_locks_main
import compiletools.git_utils
import compiletools.trim_cache


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
def cleaner(mock_args):
    """LockCleaner instance backed by the default mock_args fixture."""
    return compiletools.cleanup_locks.LockCleaner(mock_args)


@pytest.fixture
def tmpdir_with_locks(tmp_path):
    """Create temporary directory for lock testing."""
    return str(tmp_path)


def _make_lockdir_with_pid(objdir, name, content):
    """Create ``<objdir>/<name>.lockdir/`` with a ``pid`` file containing *content*."""
    lockdir = os.path.join(objdir, f"{name}.lockdir")
    os.makedirs(lockdir, exist_ok=True)
    with open(os.path.join(lockdir, "pid"), "w") as f:
        f.write(content)
    return lockdir


def create_lockdir(objdir, name, hostname, pid):
    """Helper to create a lockdir with specific hostname:pid."""
    return _make_lockdir_with_pid(objdir, name, f"{hostname}:{pid}\n")


def create_old_lockdir(objdir, name, hostname, pid, age_seconds):
    """Helper to create lockdir with specific age."""
    lockdir = create_lockdir(objdir, name, hostname, pid)
    # Set mtime to age_seconds ago
    old_time = time.time() - age_seconds
    os.utime(lockdir, (old_time, old_time))
    return lockdir


class TestLockCleanerUnit:
    """Unit tests with full mocking - no real SSH or filesystem complexity."""

    def test_read_lock_info_valid(self, tmpdir_with_locks, cleaner):
        """Test reading valid lock info (legacy host:pid format)."""
        lockdir = create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        hostname, pid, start_time = cleaner._read_lock_info(lockdir)

        assert hostname == "host1"
        assert pid == 12345
        assert start_time is None  # legacy format has no start_time

    def test_read_lock_info_with_start_time(self, tmpdir_with_locks, cleaner):
        """Test reading the new host:pid:start_time format."""
        lockdir = _make_lockdir_with_pid(tmpdir_with_locks, "x", "host1:12345:1700000000.5\n")

        hostname, pid, start_time = cleaner._read_lock_info(lockdir)
        assert hostname == "host1"
        assert pid == 12345
        assert start_time == 1700000000.5

    def test_read_lock_info_invalid_format_no_colon(self, tmpdir_with_locks, cleaner):
        """Test handling of malformed pid file without colon."""
        lockdir = _make_lockdir_with_pid(tmpdir_with_locks, "test1", "invalid-no-colon\n")

        hostname, pid, start_time = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None
        assert start_time is None

    def test_read_lock_info_empty_file(self, tmpdir_with_locks, cleaner):
        """Test handling of empty pid file."""
        lockdir = _make_lockdir_with_pid(tmpdir_with_locks, "test1", "")

        hostname, pid, start_time = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None
        assert start_time is None

    def test_read_lock_info_missing_file(self, tmpdir_with_locks, cleaner):
        """Test handling of missing pid file."""
        lockdir = os.path.join(tmpdir_with_locks, "test1.lockdir")
        os.makedirs(lockdir)

        hostname, pid, start_time = cleaner._read_lock_info(lockdir)

        assert hostname is None
        assert pid is None
        assert start_time is None

    @patch("subprocess.run")
    def test_is_process_alive_remote_success(self, mock_run, cleaner):
        """Test SSH check when process exists."""
        mock_run.return_value = Mock(returncode=0)
        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is True
        assert ssh_error is False
        # Verify SSH command
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "ssh" in call_args
        assert "remote-host" in call_args
        assert "kill -0 12345" in " ".join(call_args)

    @patch("subprocess.run")
    def test_is_process_alive_remote_not_found(self, mock_run, cleaner):
        """Test SSH check when process doesn't exist."""
        mock_run.return_value = Mock(returncode=1)
        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is False  # Exit code 1 = process not found, not SSH error

    @patch("subprocess.run")
    def test_is_process_alive_remote_ssh_failure(self, mock_run, cleaner):
        """Test SSH check when connection fails."""
        mock_run.return_value = Mock(returncode=255)
        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is True  # Exit code 255 = SSH connection failed

    @patch("subprocess.run")
    def test_is_process_alive_remote_timeout(self, mock_run, cleaner):
        """Test SSH check timeout handling."""
        mock_run.side_effect = subprocess.TimeoutExpired("ssh", 5)
        is_alive, ssh_error = cleaner._is_process_alive_remote("remote-host", 12345)

        assert is_alive is False
        assert ssh_error is True  # Timeout treated as SSH failure

    @patch("compiletools.lock_utils.os.kill")
    def test_is_process_alive_local(self, mock_kill, cleaner):
        """Local process check probes pid existence with os.kill(pid, 0)."""
        mock_kill.return_value = None
        result = cleaner._is_process_alive_local(12345)

        assert result is True
        mock_kill.assert_called_once_with(12345, 0)

    def test_get_lock_age_future_mtime(self, tmpdir_with_locks, cleaner):
        """Test clock skew handling (future mtime returns age 0)."""
        lockdir = create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        # Set mtime to future
        future_time = time.time() + 3600
        os.utime(lockdir, (future_time, future_time))

        age = cleaner._get_lock_age_seconds(lockdir)

        assert age == 0

    def test_get_lock_age_normal(self, tmpdir_with_locks, cleaner):
        """Test normal lock age calculation."""
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "host1", 12345, 100)

        age = cleaner._get_lock_age_seconds(lockdir)

        # Allow 1 second tolerance
        assert 99 <= age <= 101


class TestFcntlCleanupRemoved:
    """Verify fcntl-specific cleanup code has been removed."""

    def test_no_fcntl_lock_held_method(self, cleaner):
        """LockCleaner should not have _is_fcntl_lock_held (no sidecar files to clean)."""
        assert not hasattr(cleaner, "_is_fcntl_lock_held")

    def test_no_fcntl_lock_scan(self, tmpdir_with_locks, cleaner):
        """Scanning a GPFS directory should NOT scan for .lock files."""
        # Create a .lock file that would previously have been scanned
        lockfile = os.path.join(tmpdir_with_locks, "test.o.lock")
        with open(lockfile, "w") as f:
            f.write("")

        with (
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="gpfs"),
            patch("compiletools.filesystem_utils.get_lock_strategy", return_value="fcntl"),
        ):
            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # .lock file should NOT be counted (no fcntl scan)
        assert stats["total"] == 0


class TestLockCleanerIntegration:
    """Integration tests with real lockdirs, mocked SSH."""

    def test_scan_empty_directory(self, tmpdir_with_locks, cleaner):
        """Test scanning directory with no locks."""
        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 0
        assert stats["active"] == 0
        assert stats["stale_removed"] == 0

    def test_scan_with_stale_local_locks(self, tmpdir_with_locks, cleaner):
        """Test cleanup of stale local locks (fake PID)."""
        hostname = socket.gethostname()

        # Create old stale lock with fake PID
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 1
        assert stats["stale_removed"] == 1
        assert not os.path.exists(lockdir), "Stale lock should be removed"

    @patch("subprocess.run")
    def test_scan_with_active_remote_locks(self, mock_run, tmpdir_with_locks, cleaner):
        """Test preservation of active remote locks."""
        mock_run.return_value = Mock(returncode=0)  # Process exists
        # Create old remote lock (would be stale if local)
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 1
        assert stats["active"] == 1
        assert stats["stale_removed"] == 0
        assert os.path.exists(lockdir), "Active remote lock should be preserved"

    @patch("subprocess.run")
    def test_scan_with_stale_remote_locks(self, mock_run, tmpdir_with_locks, cleaner):
        """Test cleanup of stale remote locks."""
        mock_run.return_value = Mock(returncode=1)  # Process not found
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 1
        assert stats["stale_removed"] == 1
        assert not os.path.exists(lockdir), "Stale remote lock should be removed"

    @patch("subprocess.run")
    def test_scan_with_ssh_failure(self, mock_run, tmpdir_with_locks, cleaner):
        """Test handling when SSH unavailable."""
        mock_run.return_value = Mock(returncode=255)  # SSH failed
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", "remote-host", 12345, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 1
        assert stats["unknown"] == 1
        assert stats["stale_removed"] == 0
        assert os.path.exists(lockdir), "Lock with SSH failure should be preserved"

    def test_dry_run_mode(self, tmpdir_with_locks, mock_args):
        """Test dry-run doesn't remove locks."""
        mock_args.dry_run = True
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["stale_removed"] == 1  # Would have removed
        assert os.path.exists(lockdir), "Dry-run should not remove locks"

    def test_min_lock_age_filtering(self, tmpdir_with_locks, mock_args):
        """Test that young locks are skipped."""
        mock_args.min_lock_age = 50
        cleaner = compiletools.cleanup_locks.LockCleaner(mock_args)
        hostname = socket.gethostname()

        # Create young stale lock (30 seconds old, below min_lock_age)
        lockdir = create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 30)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 1
        assert stats["skipped_young"] == 1
        assert stats["stale_removed"] == 0
        assert os.path.exists(lockdir), "Young lock should be preserved"

    def test_statistics_collection(self, tmpdir_with_locks, cleaner):
        """Test accurate statistics tracking."""
        hostname = socket.gethostname()
        # Create mix of locks
        # 1. Active local lock (our PID)
        create_old_lockdir(tmpdir_with_locks, "active", hostname, os.getpid(), 100)

        # 2. Stale local lock (fake PID)
        create_old_lockdir(tmpdir_with_locks, "stale", hostname, 999999, 100)

        # 3. Young lock (skip)
        create_old_lockdir(tmpdir_with_locks, "young", hostname, 999998, 5)

        with patch("subprocess.run") as mock_run:
            # 4. Active remote lock
            create_old_lockdir(tmpdir_with_locks, "remote-active", "remote1", 12345, 100)

            # 5. Stale remote lock
            stale_remote_lockdir = create_old_lockdir(tmpdir_with_locks, "remote-stale", "remote2", 12346, 100)

            # Use function-based side_effect to handle non-deterministic os.walk ordering
            def ssh_side_effect(cmd, **kwargs):
                cmd_str = " ".join(cmd)
                if "remote1" in cmd_str:  # remote-active
                    return Mock(returncode=0)  # process exists
                elif "remote2" in cmd_str:  # remote-stale
                    return Mock(returncode=1)  # process not found
                return Mock(returncode=255)  # unexpected

            mock_run.side_effect = ssh_side_effect

            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        assert stats["total"] == 5
        assert stats["active"] == 2  # our PID + remote-active
        assert stats["stale_removed"] == 2  # stale local + stale remote
        assert stats["skipped_young"] == 1

        # Verify stale remote lock was actually removed
        assert not os.path.exists(stale_remote_lockdir), "Stale remote lock should be removed"

    def test_permission_errors_handling(self, tmpdir_with_locks, cleaner):
        """Test handling of permission denied on removal."""
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
        assert stats["total"] == 1
        assert stats["stale_failed"] > 0, "Should fail to remove the permission-denied lockdir"


class TestLockCleanupRaceConditions:
    """Test edge cases where locks change during scan."""

    def test_lock_disappears_between_scan_and_read(self, tmpdir_with_locks, cleaner):
        """Test lock removed after os.walk finds it."""
        # Create lockdir
        create_lockdir(tmpdir_with_locks, "test1", "host1", 12345)

        # Patch _read_lock_info to delete lockdir before reading
        original_read = cleaner._read_lock_info

        def read_and_delete(lockdir_path):
            if os.path.exists(lockdir_path):
                shutil.rmtree(lockdir_path)
            return original_read(lockdir_path)

        with patch.object(cleaner, "_read_lock_info", side_effect=read_and_delete):
            # Should handle gracefully without exception
            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # Verify completed without crash and found the lock
        assert stats["total"] == 1  # Found 1 lock (even though it disappeared)

    def test_active_process_dies_during_cleanup(self, tmpdir_with_locks, cleaner):
        """Test process exits between stale check and removal."""
        hostname = socket.gethostname()

        # This scenario: check shows active, but dies before removal
        # Create lock with our PID
        create_old_lockdir(tmpdir_with_locks, "test1", hostname, os.getpid(), 100)

        # Patch to make our PID appear dead during removal
        call_count = [0]

        def fake_is_alive(pid, start_time=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # First check: active
            return False  # Second check: dead

        with patch.object(cleaner, "_is_process_alive_local", side_effect=fake_is_alive):
            stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # Should complete without exception
        # Lock was marked active on first check, so it won't be removed
        assert stats["total"] == 1
        assert stats["active"] == 1


class TestLockCleanupMetrics:
    """Test that statistics accurately reflect operations."""

    def test_metrics_counters_sum_correctly(self, tmpdir_with_locks, cleaner):
        """Test that stats counters add up to total."""
        hostname = socket.gethostname()
        # Create various locks
        create_old_lockdir(tmpdir_with_locks, "active", hostname, os.getpid(), 100)
        create_old_lockdir(tmpdir_with_locks, "stale", hostname, 999999, 100)
        create_old_lockdir(tmpdir_with_locks, "young", hostname, 999998, 5)

        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # All locks must be accounted for in categories
        accounted = (
            stats["active"] + stats["stale_removed"] + stats["stale_failed"] + stats["unknown"] + stats["skipped_young"]
        )

        assert stats["total"] == accounted

    def test_metrics_evolution_safety(self, tmpdir_with_locks, cleaner):
        """Test metrics structure for future additions."""
        stats = cleaner.scan_and_cleanup(tmpdir_with_locks)

        # These keys must always exist
        required_keys = {"total", "active", "stale_removed", "stale_failed", "unknown", "skipped_young"}
        assert required_keys.issubset(stats.keys())


class TestCleanupLocksMain:
    """Test CLI entry point and argument parsing.

    Note: These tests use subprocess to avoid configargparse global state issues.
    """

    def test_main_help_works(self):
        """Test that --help works (smoke test)."""
        # This verifies entry point is functional
        result = subprocess.run(
            ["python", "-m", "compiletools.cleanup_locks_main", "--help"], capture_output=True, text=True, timeout=5
        )

        assert result.returncode == 0
        assert "Clean up stale locks" in result.stdout or "usage:" in result.stdout

    def test_integration_dry_run(self, tmpdir_with_locks):
        """Integration test: dry-run on empty directory."""
        hostname = socket.gethostname()

        # Create a stale lock
        create_old_lockdir(tmpdir_with_locks, "test1", hostname, 999999, 100)

        result = subprocess.run(
            ["python", "-m", "compiletools.cleanup_locks_main", "--dry-run", "--cas-objdir", tmpdir_with_locks],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=compiletools.git_utils.find_git_root(),
        )

        # Dry run should succeed
        assert result.returncode == 0
        # Lock should still exist in dry-run
        assert os.path.exists(os.path.join(tmpdir_with_locks, "test1.lockdir"))

    def test_main_oserror_returns_1(self):
        """main() catches OSError and returns 1."""
        with patch("compiletools.apptools.create_parser", side_effect=OSError(2, "No such file", "/bad")):
            rc = compiletools.cleanup_locks_main.main(argv=["--cas-objdir", "/nonexistent"])
        assert rc == 1

    def test_main_general_exception_returns_1(self):
        """main() catches general exceptions and returns 1."""
        with patch("compiletools.apptools.create_parser", side_effect=RuntimeError("boom")):
            rc = compiletools.cleanup_locks_main.main(argv=["--cas-objdir", "/nonexistent"])
        assert rc == 1

    def test_main_verbose_reraises(self):
        """main() re-raises exceptions when verbose >= 2."""
        # The OSError is raised before args is parsed, so verbose defaults to 0
        # We need to raise after args parsing to test the verbose path
        with (
            patch("compiletools.cleanup_locks.LockCleaner", side_effect=RuntimeError("boom")),
            patch("compiletools.apptools.create_parser") as mock_parser,
        ):
            # Set up mock to return args with verbose=2
            mock_cap = mock_parser.return_value
            mock_args = Mock()
            mock_args.verbose = 2
            mock_args.quiet = 0
            mock_args.min_lock_age = None
            mock_args.lock_cross_host_timeout = 600
            mock_cap.parse_args.return_value = mock_args
            with patch("compiletools.configutils.extract_variant", return_value=""):
                with patch("compiletools.apptools.add_base_arguments"):
                    with patch("compiletools.apptools.add_locking_arguments"):
                        with patch("compiletools.apptools.add_output_directory_arguments"):
                            with patch("compiletools.apptools.resolve_cas_directory_arguments"):
                                with pytest.raises(RuntimeError, match="boom"):
                                    compiletools.cleanup_locks_main.main(argv=[])

    def test_exit_code_on_empty_directory(self, tmpdir_with_locks):
        """Test exit code 0 when no locks found."""
        result = subprocess.run(
            ["python", "-m", "compiletools.cleanup_locks_main", "--cas-objdir", tmpdir_with_locks],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=compiletools.git_utils.find_git_root(),
        )

        assert result.returncode == 0

    def _run_main_with_mocks(self, *, verbose=0, stats=None, dry_run=False):
        """Helper: run main() in-process with full mocking past arg parsing.

        Returns (return_code, mock_cleaner, captured_stdout).
        """
        import io
        from contextlib import redirect_stdout

        if stats is None:
            stats = {"stale_failed": 0, "stale_removed": 0, "active": 0, "total": 0}

        mock_args = Mock()
        mock_args.verbose = verbose
        mock_args.quiet = 0
        mock_args.min_lock_age = None
        mock_args.lock_cross_host_timeout = 600
        mock_args.ssh_timeout = 5
        mock_args.dry_run = dry_run
        mock_args.all_variants = False

        mock_cleaner = Mock()
        mock_cleaner.scan_and_cleanup.return_value = stats

        mock_namer = Mock()
        mock_namer.object_dir.return_value = "/tmp/fake_objdir"

        with (
            patch("compiletools.apptools.create_parser") as mock_parser,
            patch("compiletools.configutils.extract_variant", return_value=""),
            patch("compiletools.apptools.add_base_arguments"),
            patch("compiletools.apptools.add_locking_arguments"),
            patch("compiletools.apptools.add_output_directory_arguments"),
            patch("compiletools.apptools.resolve_cas_directory_arguments"),
            patch("compiletools.cleanup_locks.LockCleaner", return_value=mock_cleaner),
            patch("compiletools.namer.Namer", return_value=mock_namer),
        ):
            mock_parser.return_value.parse_args.return_value = mock_args
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = compiletools.cleanup_locks_main.main(argv=[])
            return rc, mock_cleaner, buf.getvalue()

    def test_main_happy_path_no_stale(self):
        """main() returns 0 when no stale locks fail to remove."""
        rc, mock_cleaner, _ = self._run_main_with_mocks()
        assert rc == 0
        mock_cleaner.scan_and_cleanup.assert_called_once_with("/tmp/fake_objdir")
        mock_cleaner.print_summary.assert_called_once()

    def test_main_returns_1_on_stale_failed(self):
        """main() returns 1 when stale locks fail to remove."""
        rc, _, _ = self._run_main_with_mocks(stats={"stale_failed": 2, "stale_removed": 1, "active": 0, "total": 3})
        assert rc == 1

    def test_main_verbose_prints_config(self):
        """main() prints configuration when verbose >= 1."""
        rc, _, output = self._run_main_with_mocks(verbose=1, dry_run=True)
        assert rc == 0
        assert "Object directory: /tmp/fake_objdir" in output
        assert "Min lock age: 600s" in output
        assert "SSH timeout: 5s" in output
        assert "Dry run: True" in output

    def test_main_quiet_no_config_output(self):
        """main() does not print config when verbose < 1."""
        rc, _, output = self._run_main_with_mocks(verbose=0)
        assert rc == 0
        assert "Configuration:" not in output

    def test_main_oserror_verbose_reraises(self):
        """main() re-raises OSError when verbose >= 2."""
        mock_args = Mock()
        mock_args.verbose = 2
        mock_args.quiet = 0
        mock_args.min_lock_age = None
        mock_args.lock_cross_host_timeout = 600

        with (
            patch("compiletools.apptools.create_parser") as mock_parser,
            patch("compiletools.configutils.extract_variant", return_value=""),
            patch("compiletools.apptools.add_base_arguments"),
            patch("compiletools.apptools.add_locking_arguments"),
            patch("compiletools.apptools.add_output_directory_arguments"),
            patch("compiletools.apptools.resolve_cas_directory_arguments"),
            patch("compiletools.cleanup_locks.LockCleaner", side_effect=OSError(2, "No such file", "/bad")),
        ):
            mock_parser.return_value.parse_args.return_value = mock_args
            with pytest.raises(OSError):
                compiletools.cleanup_locks_main.main(argv=[])


class TestAllVariants:
    """``--all-variants`` sweeps every RESOLVABLE obj-pool cell for stale locks.

    Pool layout used by most tests:
      * ``gcc.debug``    — RESOLVABLE (patched classifier) — has a stale lockdir
      * ``clang.debug``  — RESOLVABLE (patched classifier) — has a stale lockdir
      * ``bogus.variant`` — UNRESOLVABLE (patched classifier) — has a stale
                            lockdir that MUST survive untouched

    The test argv passes ``--cas-objdir=<pool>/gcc.debug`` and
    ``--variant=gcc.debug`` so that ``cell_pool_root`` can climb from the
    variant-suffixed path to the pool root.  ``--min-lock-age=0`` ensures the
    planted locks are not skipped as too young.
    """

    hostname = socket.gethostname()

    def _build_pool(self, tmp_path, monkeypatch):
        """Create an obj pool with resolvable and unresolvable cells, each with a
        stale lockdir inside them."""
        pool = tmp_path / "pool"
        pool.mkdir()

        for cell_name in ("gcc.debug", "clang.debug", "bogus.variant"):
            cell_dir = str(pool / cell_name)
            os.makedirs(cell_dir)
            # Plant a stale lockdir (PID 999999 is non-existent locally)
            create_old_lockdir(cell_dir, "lock1", self.hostname, 999999, 200)

        resolvable = {"gcc.debug", "clang.debug"}
        monkeypatch.setattr(compiletools.trim_cache, "_variant_resolvable", lambda name: name in resolvable)
        monkeypatch.setattr(compiletools.trim_cache, "_variant_canonical_name", lambda name: name)
        return str(pool)

    def test_sweeps_every_resolvable_cell(self, tmp_path, monkeypatch):
        """--all-variants sweeps every RESOLVABLE cell; UNRESOLVABLE cells are untouched."""
        pool = self._build_pool(tmp_path, monkeypatch)

        gcc_lockdir = os.path.join(pool, "gcc.debug", "lock1.lockdir")
        clang_lockdir = os.path.join(pool, "clang.debug", "lock1.lockdir")
        bogus_lockdir = os.path.join(pool, "bogus.variant", "lock1.lockdir")

        # All lockdirs must exist before the sweep
        assert os.path.exists(gcc_lockdir)
        assert os.path.exists(clang_lockdir)
        assert os.path.exists(bogus_lockdir)

        rc = compiletools.cleanup_locks_main.main(
            [
                "--all-variants",
                f"--cas-objdir={pool}/gcc.debug",
                "--variant=gcc.debug",
                "--min-lock-age=0",
            ]
        )

        assert rc == 0
        assert not os.path.exists(gcc_lockdir), "gcc.debug stale lockdir must be removed"
        assert not os.path.exists(clang_lockdir), "clang.debug stale lockdir must be removed"
        assert os.path.exists(bogus_lockdir), "bogus.variant lockdir must NOT be touched"

    def test_dry_run_removes_nothing(self, tmp_path, monkeypatch):
        """--all-variants --dry-run reports but touches nothing."""
        pool = self._build_pool(tmp_path, monkeypatch)

        gcc_lockdir = os.path.join(pool, "gcc.debug", "lock1.lockdir")
        clang_lockdir = os.path.join(pool, "clang.debug", "lock1.lockdir")
        bogus_lockdir = os.path.join(pool, "bogus.variant", "lock1.lockdir")

        rc = compiletools.cleanup_locks_main.main(
            [
                "--all-variants",
                "--dry-run",
                f"--cas-objdir={pool}/gcc.debug",
                "--variant=gcc.debug",
                "--min-lock-age=0",
            ]
        )

        assert rc == 0
        # All lockdirs must still exist after dry-run
        assert os.path.exists(gcc_lockdir), "gcc.debug lockdir must survive --dry-run"
        assert os.path.exists(clang_lockdir), "clang.debug lockdir must survive --dry-run"
        assert os.path.exists(bogus_lockdir), "bogus.variant lockdir must survive --dry-run"

    def test_bad_cell_isolated_not_fatal(self, tmp_path, monkeypatch):
        """A per-cell scan_and_cleanup exception is isolated; the other cell still runs; rc==1."""
        pool = self._build_pool(tmp_path, monkeypatch)

        gcc_lockdir = os.path.join(pool, "gcc.debug", "lock1.lockdir")
        clang_lockdir = os.path.join(pool, "clang.debug", "lock1.lockdir")

        real_scan = compiletools.cleanup_locks.LockCleaner.scan_and_cleanup

        def flaky_scan(self_cleaner, objdir):
            # Fail only for the clang.debug cell
            if objdir.endswith(os.sep + "clang.debug"):
                raise RuntimeError("injected per-cell boom")
            return real_scan(self_cleaner, objdir)

        monkeypatch.setattr(compiletools.cleanup_locks.LockCleaner, "scan_and_cleanup", flaky_scan)

        rc = compiletools.cleanup_locks_main.main(
            [
                "--all-variants",
                f"--cas-objdir={pool}/gcc.debug",
                "--variant=gcc.debug",
                "--min-lock-age=0",
            ]
        )

        # rc==1 because one cell errored
        assert rc == 1
        # gcc.debug must still have been cleaned (the other resolvable cell)
        assert not os.path.exists(gcc_lockdir), "gcc.debug lockdir must be removed despite clang.debug error"
        # clang.debug lockdir intact because scan_and_cleanup raised for that cell
        assert os.path.exists(clang_lockdir), "clang.debug lockdir must remain (cell errored)"
