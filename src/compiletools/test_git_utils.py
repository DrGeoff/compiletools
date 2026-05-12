"""Tests for compiletools.git_utils."""

import os
import tempfile
from unittest.mock import patch

import pytest

import compiletools.git_utils


@pytest.fixture(autouse=True)
def _clear_git_root_cache():
    """Reset the per-directory cache so tests don't poison each other."""
    compiletools.git_utils.clear_cache()


def test_find_git_root_in_repo_returns_toplevel():
    """Inside a real git repo, returns the toplevel reported by git."""
    result = compiletools.git_utils.find_git_root()
    assert result
    assert os.path.isdir(result)


def test_find_git_root_outside_repo_falls_back_to_directory():
    """Outside any repo, returns the queried directory rather than empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use realpath so symlinked /tmp paths (e.g. macOS /private/var) compare equal.
        real_tmp = os.path.realpath(tmpdir)
        result = compiletools.git_utils._find_git_root(real_tmp)
        assert result == real_tmp


def test_find_git_root_treats_empty_git_output_as_failure():
    """Regression: git rev-parse can succeed with empty output in some bare-repo /
    GIT_DIR edge cases; the function must NOT propagate "" — it broke
    test_cleanup_locks::test_integration_dry_run by passing cwd="" to subprocess.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        with patch("subprocess.check_output", return_value="\n"):
            result = compiletools.git_utils._find_git_root(real_tmp)
        # Must fall through to the fallback walker, ending at the directory itself.
        assert result == real_tmp
        assert result != ""
