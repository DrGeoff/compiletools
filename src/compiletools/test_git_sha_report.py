"""Tests for git_sha_report module."""

import subprocess
from pathlib import Path

import pytest

import compiletools.git_sha_report as gsr


@pytest.fixture()
def tmp_git_repo(tmp_path, monkeypatch):
    """Create a temporary git repository with a committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # Create and commit a file
    (repo / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr("compiletools.git_sha_report.find_git_root", lambda *a, **kw: str(repo))
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda *a, **kw: str(repo))
    monkeypatch.chdir(repo)
    return repo


def test_run_git_basic(tmp_git_repo):
    """run_git can execute simple git commands."""
    output = gsr.run_git("git rev-parse --show-toplevel")
    assert len(output) > 0


def test_run_git_failure():
    """run_git raises RuntimeError on failure."""
    with pytest.raises(RuntimeError, match="Git command failed"):
        gsr.run_git("git log --invalid-flag-xyz")


def test_get_index_hashes(tmp_git_repo):
    """get_index_hashes returns dict of Path -> sha."""
    hashes = gsr.get_index_hashes()
    assert isinstance(hashes, dict)
    assert len(hashes) > 0
    for path, sha in list(hashes.items())[:3]:
        assert isinstance(path, Path)
        assert len(sha) == 40


def test_get_file_stat(tmp_git_repo):
    """get_file_stat returns (size, mtime) tuple."""
    p = tmp_git_repo / "hello.txt"
    size, mtime = gsr.get_file_stat(p)
    assert size > 0
    assert mtime > 0


def test_get_untracked_files(tmp_git_repo):
    """get_untracked_files returns list of Paths."""
    files = gsr.get_untracked_files()
    assert isinstance(files, list)


def test_get_current_blob_hashes(tmp_git_repo):
    """get_current_blob_hashes returns dict."""
    hashes = gsr.get_current_blob_hashes()
    assert isinstance(hashes, dict)
    assert len(hashes) > 0


def test_get_modified_but_unstaged_files(tmp_git_repo):
    """get_modified_but_unstaged_files returns list."""
    files = gsr.get_modified_but_unstaged_files()
    assert isinstance(files, list)


def test_batch_hash_objects_empty():
    """batch_hash_objects with empty list returns empty dict."""
    assert gsr.batch_hash_objects([]) == {}


def test_main_tracked_only(tmp_git_repo, capsys):
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
