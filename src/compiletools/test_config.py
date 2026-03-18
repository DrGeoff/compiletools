"""Tests for config module."""

import subprocess

import compiletools.git_utils


def test_main_help():
    """ct-config --help works."""
    result = subprocess.run(
        ["ct-config", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=compiletools.git_utils.find_git_root(),
    )
    assert result.returncode == 0
    assert "Configuration examination tool" in result.stdout or "usage:" in result.stdout
