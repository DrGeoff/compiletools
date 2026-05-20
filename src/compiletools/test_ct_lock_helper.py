"""Tests for ct-lock-helper (Python implementation).

Tests the ct-lock-helper entry point which wraps locking.py's atomic_compile().
"""

import os
import shutil
import subprocess
import tempfile

import pytest

from compiletools.ct_lock_helper import create_args_from_env


@pytest.fixture
def temp_target():
    """Create temporary target file for lock testing."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".o") as f:
        temp_path = f.name
    yield temp_path
    # Cleanup all lock artifacts
    for ext in ["", ".lockdir", ".lock", ".lock.excl", ".lock.pid"]:
        try:
            path = temp_path + ext
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass
    # Clean up temp files created by ct-lock-helper
    parent_dir = os.path.dirname(temp_path)
    basename = os.path.basename(temp_path)
    for f in os.listdir(parent_dir):
        if f.startswith(basename) and ".tmp" in f:
            try:
                os.unlink(os.path.join(parent_dir, f))
            except OSError:
                pass


def _run_helper(subcmd, target, strategy, *cmd, timeout=5):
    """Invoke ``ct-lock-helper <subcmd> --target=... --strategy=... -- <cmd...>``."""
    return subprocess.run(
        [
            "ct-lock-helper",
            subcmd,
            f"--target={target}",
            f"--strategy={strategy}",
            "--",
            *cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_lock_cleaned(target, strategy, *, on_error=False):
    """Assert that the strategy-specific lock sidecar was removed.

    ``flock``/``fcntl`` keep a persistent ``<target>.lock`` shared with peers;
    ``cifs`` keeps the base ``.lock`` but removes ``.lock_excl``; ``lockdir``
    removes the entire ``.lockdir``.
    """
    suffix = " on error" if on_error else ""
    if strategy == "lockdir":
        assert not os.path.exists(target + ".lockdir"), f"Lock not cleaned up{suffix}"
    elif strategy == "cifs":
        assert not os.path.exists(target + ".lock_excl"), f"Excl marker not cleaned up{suffix}"
    elif strategy not in ("flock", "fcntl"):
        assert not os.path.exists(target + ".lock"), f"Lock not cleaned up{suffix}"


class TestLockHelper:
    """Tests for ct-lock-helper."""

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock", "fcntl"])
    def test_successful_compile(self, temp_target, strategy):
        """Test that ct-lock-helper successfully compiles a simple program."""
        source = temp_target.replace(".o", ".c")
        with open(source, "w") as f:
            f.write("int main() { return 0; }\n")
        try:
            result = _run_helper("compile", temp_target, strategy, "gcc", "-c", source)
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"
            assert os.path.exists(temp_target), "Target file not created"
            _assert_lock_cleaned(temp_target, strategy)
        finally:
            if os.path.exists(source):
                os.unlink(source)

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock", "fcntl"])
    def test_compile_error_propagates(self, temp_target, strategy):
        """Test that compiler errors cause non-zero exit."""
        source = temp_target.replace(".o", ".c")
        with open(source, "w") as f:
            f.write("int main() { this_is_a_syntax_error; }\n")
        try:
            result = _run_helper("compile", temp_target, strategy, "gcc", "-c", source)
            assert result.returncode != 0, "Should fail with syntax error"
            _assert_lock_cleaned(temp_target, strategy, on_error=True)
        finally:
            if os.path.exists(source):
                os.unlink(source)

    def test_help_command(self):
        """Test that help command works."""
        # Main help
        result = subprocess.run(["ct-lock-helper", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ct-lock-helper" in result.stdout
        assert "compile" in result.stdout

        # Compile subcommand help
        result = subprocess.run(["ct-lock-helper", "compile", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "lockdir" in result.stdout
        assert "cifs" in result.stdout
        assert "flock" in result.stdout


class TestLockHelperLink:
    """Tests for ct-lock-helper link subcommand."""

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock", "fcntl"])
    def test_successful_link(self, temp_target, strategy):
        """Test that ct-lock-helper link runs a command under lock."""
        # Use 'touch' as a simple link stand-in
        result = _run_helper("link", temp_target, strategy, "touch", temp_target)
        assert result.returncode == 0, f"Link failed: {result.stderr}"
        assert os.path.exists(temp_target), "Target file not created"
        if strategy == "lockdir":
            assert not os.path.exists(temp_target + ".lockdir"), "Lock not cleaned up"

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock", "fcntl"])
    def test_link_error_propagates(self, temp_target, strategy):
        """Test that link command errors cause non-zero exit."""
        result = _run_helper("link", temp_target, strategy, "false")
        assert result.returncode != 0, "Should fail when command fails"
        if strategy == "lockdir":
            assert not os.path.exists(temp_target + ".lockdir"), "Lock not cleaned up on error"

    def test_help_shows_link(self):
        """Test that help output includes link subcommand."""
        result = subprocess.run(["ct-lock-helper", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "link" in result.stdout

        # Link subcommand help
        result = subprocess.run(["ct-lock-helper", "link", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--target" in result.stdout
        assert "--strategy" in result.stdout


class TestEnvParsing:
    """Issue #8: bad env values must produce a clear, named warning rather
    than a generic ValueError that kills the helper without diagnostics."""

    @pytest.mark.parametrize(
        ("env_name", "value", "attr", "expected_default"),
        [
            pytest.param("CT_LOCK_WARN_INTERVAL", "not-a-number", "lock_warn_interval", 30, id="garbage-int"),
            pytest.param("CT_LOCK_SLEEP_INTERVAL", "fast", "sleep_interval_lockdir", 0.05, id="garbage-float"),
        ],
    )
    def test_garbage_env_falls_back_to_default_with_warning(
        self, monkeypatch, capsys, env_name, value, attr, expected_default
    ):
        monkeypatch.setenv(env_name, value)
        args = create_args_from_env()
        assert getattr(args, attr) == expected_default
        err = capsys.readouterr().err
        assert env_name in err
        assert value in err

    def test_valid_values_are_parsed(self, monkeypatch):
        monkeypatch.setenv("CT_LOCK_WARN_INTERVAL", "60")
        monkeypatch.setenv("CT_LOCK_SLEEP_INTERVAL", "0.25")
        args = create_args_from_env()
        assert args.lock_warn_interval == 60
        assert args.sleep_interval_lockdir == 0.25

    def test_unset_env_uses_defaults(self, monkeypatch):
        for name in (
            "CT_LOCK_WARN_INTERVAL",
            "CT_LOCK_TIMEOUT",
            "CT_LOCK_SLEEP_INTERVAL",
            "CT_LOCK_SLEEP_INTERVAL_CIFS",
            "CT_LOCK_SLEEP_INTERVAL_FLOCK",
            "CT_LOCK_VERBOSE",
        ):
            monkeypatch.delenv(name, raising=False)
        args = create_args_from_env()
        assert args.lock_warn_interval == 30
        assert args.lock_cross_host_timeout == 600
        assert args.sleep_interval_lockdir == 0.05


class TestGracefulExitSignalStack:
    """Issue #9: when ct-lock-helper receives SIGTERM, its child process must
    be reaped (no zombie) thanks to the signal-handler-stack interaction
    between GracefulExit and atomic_compile's _run_with_signal_forwarding."""

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX-only signal test")
    def test_sigterm_reaps_child(self, tmp_path):
        """Run ct-lock-helper with a long-sleeping shell as the compile cmd,
        SIGTERM the helper, and confirm the child is reaped."""
        import signal as _signal
        import time as _time

        target = str(tmp_path / "test.o")

        # We need to know the helper's PID *and* the child's PID to confirm
        # the child is reaped (no zombie). Simplest approach: have the
        # compile cmd write its own PID to a marker file before sleeping.
        child_pid_file = tmp_path / "CHILD_PID"
        proc = subprocess.Popen(
            [
                "ct-lock-helper",
                "compile",
                f"--target={target}",
                "--strategy=flock",
                "--",
                "sh",
                "-c",
                f'echo $$ > "{child_pid_file}"; sleep 30',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            # Wait for child to record its pid
            deadline = _time.time() + 5
            while not child_pid_file.exists() and _time.time() < deadline:
                _time.sleep(0.05)
            assert child_pid_file.exists(), "Child shell never recorded its pid"
            child_pid = int(child_pid_file.read_text().strip())
            assert child_pid > 0

            # Send SIGTERM to helper
            proc.send_signal(_signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait()
                pytest.fail("Helper did not exit after SIGTERM")

            # Give the kernel a moment to reap the child
            _time.sleep(0.5)

            # Child should no longer exist (kernel reaped it because the
            # session leader / parent forwarded SIGTERM and waited).
            try:
                os.kill(child_pid, 0)
                child_alive = True
            except ProcessLookupError:
                child_alive = False
            except PermissionError:
                # On some systems we get EPERM rather than ESRCH for a
                # zombie; treat as still-around for the purposes of this test.
                child_alive = True
            assert not child_alive, (
                f"Child pid {child_pid} survived helper SIGTERM — signal "
                f"forwarding / reaping in atomic_compile is broken"
            )
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait()
