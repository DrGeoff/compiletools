"""Tests for ct-lock-helper (Python implementation).

Tests the ct-lock-helper entry point which wraps locking.py's atomic_compile().
"""

import os
import shutil
import subprocess
import tempfile

import pytest


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


class TestLockHelper:
    """Tests for ct-lock-helper."""

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock"])
    def test_successful_compile(self, temp_target, strategy):
        """Test that ct-lock-helper successfully compiles a simple program."""
        # Create a simple C source file
        source = temp_target.replace(".o", ".c")
        with open(source, "w") as f:
            f.write("int main() { return 0; }\n")

        try:
            result = subprocess.run(
                [
                    "ct-lock-helper",
                    "compile",
                    f"--target={temp_target}",
                    f"--strategy={strategy}",
                    "--",
                    "gcc",
                    "-c",
                    source,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Verify success
            assert result.returncode == 0, f"Compilation failed: {result.stderr}"
            assert os.path.exists(temp_target), "Target file not created"

            # Verify lock was cleaned up (flock/fcntl lock the target directly, no sidecar)
            if strategy == "lockdir":
                assert not os.path.exists(temp_target + ".lockdir"), "Lock not cleaned up"
            elif strategy not in ("flock", "fcntl"):
                assert not os.path.exists(temp_target + ".lock"), "Lock not cleaned up"

        finally:
            if os.path.exists(source):
                os.unlink(source)

    @pytest.mark.parametrize("strategy", ["lockdir", "cifs", "flock"])
    def test_compile_error_propagates(self, temp_target, strategy):
        """Test that compiler errors cause non-zero exit."""
        # Create source with syntax error
        source = temp_target.replace(".o", ".c")
        with open(source, "w") as f:
            f.write("int main() { this_is_a_syntax_error; }\n")

        try:
            result = subprocess.run(
                [
                    "ct-lock-helper",
                    "compile",
                    f"--target={temp_target}",
                    f"--strategy={strategy}",
                    "--",
                    "gcc",
                    "-c",
                    source,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Verify failure
            assert result.returncode != 0, "Should fail with syntax error"

            # Verify lock was cleaned up even on error (flock keeps lockfile on disk)
            if strategy == "lockdir":
                assert not os.path.exists(temp_target + ".lockdir"), "Lock not cleaned up on error"
            elif strategy != "flock":
                assert not os.path.exists(temp_target + ".lock"), "Lock not cleaned up on error"

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
