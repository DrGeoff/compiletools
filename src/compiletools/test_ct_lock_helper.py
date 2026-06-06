"""Tests for ct-lock-helper (Python implementation).

Tests the ct-lock-helper entry point which wraps locking.py's atomic_compile().
"""

import os
import subprocess

import pytest

from compiletools.ct_lock_helper import create_args_from_env


def _proc_start_ticks(pid):
    """Return field 22 (starttime, in clock ticks since boot) of
    ``/proc/<pid>/stat`` as a string. Together with the pid this uniquely
    identifies a process *incarnation*, so it distinguishes "the original
    child is still alive" from "the pid was recycled by an unrelated process".

    Returns None when unreadable — the process is gone, or we're not on a
    /proc platform (e.g. macOS) — in which case callers fall back to a bare
    liveness probe.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return None
    # Field 2 (comm) is parenthesised and may itself contain spaces/parens;
    # everything after the final ')' is space-delimited starting at field 3,
    # so starttime (field 22) is index 19 of that tail.
    try:
        tail = data[data.rindex(")") + 2 :].split()
        return tail[19]
    except (ValueError, IndexError):
        return None


def _child_gone(pid, start_ticks):
    """True if the original child incarnation is gone.

    Robust against PID reuse: a *live* pid whose start time no longer matches
    the value recorded while the child was running is a different process that
    recycled the number, so the original child is gone. When ``start_ticks``
    is None (no /proc on this platform) this degrades to a bare liveness probe.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # EPERM: the pid is owned by another user, so it cannot be our
        # same-user child — the number was recycled. (A zombie of our own
        # child yields success from os.kill, not EPERM.) Only trust this when
        # /proc identity checking is available; otherwise stay conservative.
        return start_ticks is not None
    # pid is alive — it is the same incarnation only if its start time matches.
    if start_ticks is None:
        return False
    return _proc_start_ticks(pid) != start_ticks


@pytest.fixture
def temp_target(tmp_path):
    """Create temporary target file for lock testing.

    pytest's tmp_path auto-cleans the directory after the test, which
    transparently removes the .o, every lock sidecar (.lockdir, .lock,
    .lock.excl, .lock.pid), and any .tmp files ct-lock-helper writes
    alongside the target — all live in the same per-test directory.
    """
    target = tmp_path / "target.o"
    target.touch()
    return str(target)


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
            # Record the child's start time while it is definitely alive so we
            # can tell "still our child" from "this pid was recycled" later.
            child_start_ticks = _proc_start_ticks(child_pid)

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

            # The helper reaps its child before exiting (the _forward path
            # reaps via proc.wait(); the finally clause unconditionally
            # killpg(SIGKILL)s + reaps), so the child should already be gone.
            # Poll rather than sample once, and verify process *identity* via
            # start time: on a heavily-loaded runner the child's pid can be
            # recycled by an unrelated process within this window, and a bare
            # os.kill(pid, 0) would misread that as the child surviving.
            child_alive = True
            deadline = _time.time() + 3
            while _time.time() < deadline:
                if _child_gone(child_pid, child_start_ticks):
                    child_alive = False
                    break
                _time.sleep(0.05)
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


class TestPidReuseGuard:
    """The PID-reuse guard that keeps test_sigterm_reaps_child from flaking.

    On a slow, heavily-loaded runner (the symptom was pypy-3.11 CI only) the
    reaped child's pid number can be recycled by an unrelated process before
    the liveness check runs, making a bare os.kill(pid, 0) report the child as
    "survived". _child_gone defeats that by checking process *identity* via
    /proc start time. This is the deterministic stand-in for the un-forceable
    real PID-reuse race.
    """

    def test_child_gone_detects_pid_reuse(self):
        live = os.getpid()  # this test process — definitely alive
        real = _proc_start_ticks(live)
        if real is None:
            pytest.skip("no /proc starttime available on this platform")
        # Same pid, matching start time => same incarnation => NOT gone.
        assert _child_gone(live, real) is False
        # Same pid, mismatched start time => the number was recycled by a
        # different process => the original incarnation is treated as gone.
        assert _child_gone(live, str(int(real) + 1)) is True

    def test_child_gone_true_for_dead_pid(self):
        # A pid that never maps to a live process reads as gone regardless of
        # the recorded start time (None: no /proc fallback path).
        import subprocess as _sp

        p = _sp.Popen(["true"])
        p.wait()
        assert _child_gone(p.pid, None) is True
