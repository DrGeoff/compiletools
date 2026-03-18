"""Tests for git_sha_report module."""

from pathlib import Path

import compiletools.git_sha_report as gsr


def test_run_git_basic():
    """run_git can execute simple git commands."""
    output = gsr.run_git("git rev-parse --show-toplevel")
    assert len(output) > 0


def test_run_git_failure():
    """run_git raises RuntimeError on failure."""
    import pytest

    with pytest.raises(RuntimeError, match="Git command failed"):
        gsr.run_git("git log --invalid-flag-xyz")


def test_get_index_hashes():
    """get_index_hashes returns dict of Path -> sha."""
    hashes = gsr.get_index_hashes()
    assert isinstance(hashes, dict)
    assert len(hashes) > 0
    for path, sha in list(hashes.items())[:3]:
        assert isinstance(path, Path)
        assert len(sha) == 40


def test_get_file_stat():
    """get_file_stat returns (size, mtime) tuple."""
    # Use a known file
    import compiletools.git_utils

    git_root = compiletools.git_utils.find_git_root()
    p = Path(git_root) / "pyproject.toml"
    size, mtime = gsr.get_file_stat(p)
    assert size > 0
    assert mtime > 0


def test_get_untracked_files():
    """get_untracked_files returns list of Paths."""
    files = gsr.get_untracked_files()
    assert isinstance(files, list)


def test_get_current_blob_hashes():
    """get_current_blob_hashes returns dict."""
    hashes = gsr.get_current_blob_hashes()
    assert isinstance(hashes, dict)
    assert len(hashes) > 0


def test_get_modified_but_unstaged_files():
    """get_modified_but_unstaged_files returns list."""
    files = gsr.get_modified_but_unstaged_files()
    assert isinstance(files, list)


def test_batch_hash_objects_empty():
    """batch_hash_objects with empty list returns empty dict."""
    assert gsr.batch_hash_objects([]) == {}


def test_main_tracked_only(capsys):
    """main() prints tracked file hashes."""
    import sys

    original_argv = sys.argv
    sys.argv = ["ct-git-sha-report"]
    try:
        gsr.main()
    finally:
        sys.argv = original_argv
    out = capsys.readouterr().out
    assert "Tracked files only" in out
