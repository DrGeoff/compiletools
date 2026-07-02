"""Tests for compiletools.git_utils."""

import os
from unittest.mock import patch

import pytest

import compiletools.git_utils


@pytest.fixture(autouse=True)
def _clear_git_root_cache():
    """Reset the per-directory cache so tests don't poison each other."""
    compiletools.git_utils.clear_cache()


@pytest.fixture(autouse=True)
def _reset_allow_fake_git():
    """Ensure the module-level permissive flag is reset to its default after each test."""
    compiletools.git_utils.set_allow_fake_git(False)
    yield
    compiletools.git_utils.set_allow_fake_git(False)


def test_find_git_root_in_repo_returns_toplevel():
    """Inside a real git repo, returns the toplevel reported by git."""
    result = compiletools.git_utils.find_git_root()
    assert result
    assert os.path.isdir(result)


def test_find_git_root_outside_repo_falls_back_to_directory(tmp_path):
    """Outside any repo, returns the queried directory rather than empty."""
    # Use realpath so symlinked /tmp paths (e.g. macOS /private/var) compare equal.
    real_tmp = os.path.realpath(tmp_path)
    result = compiletools.git_utils._find_git_root(real_tmp)
    assert result == real_tmp


def test_find_git_root_treats_empty_git_output_as_failure(tmp_path):
    """Regression: git rev-parse can succeed with empty output in some bare-repo /
    GIT_DIR edge cases; the function must NOT propagate "" — it broke
    test_cleanup_locks::test_integration_dry_run by passing cwd="" to subprocess.
    """
    real_tmp = os.path.realpath(tmp_path)
    with patch("subprocess.check_output", return_value="\n"):
        result = compiletools.git_utils._find_git_root(real_tmp)
    # Must fall through to the fallback walker, ending at the directory itself.
    assert result == real_tmp
    assert result != ""


def test_empty_git_dir_rejected_by_default(tmp_path):
    """A bare empty '.git/' directory must NOT be treated as a real git marker.

    This guards against the cross-user poisoning case: a stray empty
    ``/tmp/.git`` left by another user must not become the gitroot of every
    test that happens to run under ``/tmp/...``. We verify the rejection
    by comparing two scenarios — *with* and *without* the empty ``.git/``
    placeholder — and asserting the strict walker gives the same answer
    in both cases (i.e. the placeholder didn't influence the result).
    """
    real_tmp_with = os.path.realpath(tmp_path)
    (tmp_path / ".git").mkdir()  # empty directory — fake marker
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result_with_fake = compiletools.git_utils._find_git_root(real_tmp_with)
    # Now remove it and re-check (clear cache so we re-run the walker).
    (tmp_path / ".git").rmdir()
    compiletools.git_utils.clear_cache()
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result_without_fake = compiletools.git_utils._find_git_root(real_tmp_with)
    # Strict default: the empty .git/ must NOT alter the walker's answer.
    assert result_with_fake == result_without_fake


def test_empty_git_dir_accepted_when_allow_fake_git_set(tmp_path):
    """With ``set_allow_fake_git(True)`` the legacy permissive walker is restored."""
    real_tmp = os.path.realpath(tmp_path)
    (tmp_path / ".git").mkdir()  # empty directory — fake marker
    compiletools.git_utils.set_allow_fake_git(True)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_tmp)
    assert result == real_tmp


def test_real_git_dir_with_HEAD_accepted_by_default(tmp_path):
    """A directory ``.git/`` containing ``HEAD`` is a real repo and must be accepted."""
    real_tmp = os.path.realpath(tmp_path)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_tmp)
    assert result == real_tmp


def test_git_regular_file_accepted_by_default(tmp_path):
    """A regular-file ``.git`` (worktree gitlink form) must be accepted."""
    real_tmp = os.path.realpath(tmp_path)
    (tmp_path / ".git").write_text("gitdir: /path/to/realrepo/.git/worktrees/wt\n")
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_tmp)
    assert result == real_tmp


def test_empty_git_regular_file_rejected_by_default(tmp_path):
    """A regular-file ``.git`` with empty contents must NOT be accepted as a real marker.

    Defends against the cross-user poisoning vector: ``touch /tmp/.git`` would
    otherwise defeat strict mode. A real worktree gitlink begins with
    ``gitdir: `` per ``git worktree add`` format.
    """
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").write_text("")  # empty regular file — fake marker
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    # Strict default: empty `.git` regular file must not be accepted as marker.
    assert result != real_parent


def test_git_regular_file_with_gitdir_prefix_accepted(tmp_path):
    """A regular-file ``.git`` whose first line begins with ``gitdir: `` is a real
    worktree gitlink and must be accepted."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result == real_parent


def test_git_regular_file_with_garbage_rejected(tmp_path):
    """A regular-file ``.git`` containing random text (no ``gitdir: `` prefix) must NOT
    be accepted as a real marker."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").write_text("this is not a gitlink, just random text\n")
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result != real_parent


def test_empty_head_rejected(tmp_path):
    """A ``.git/`` directory containing an empty ``HEAD`` file must NOT be accepted.

    Defends against ``mkdir /tmp/.git && touch /tmp/.git/HEAD`` defeating strict mode.
    A real HEAD begins with ``ref: refs/...`` or a 40-hex SHA.
    """
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    git_dir = parent / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("")  # empty — fake marker
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result != real_parent


def test_head_with_ref_accepted(tmp_path):
    """A ``.git/HEAD`` beginning with ``ref: refs/...`` is a real repo HEAD."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    git_dir = parent / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result == real_parent


def test_head_with_40hex_accepted(tmp_path):
    """A ``.git/HEAD`` containing a 40-hex SHA on the first line (detached HEAD form)
    is a real repo HEAD."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    git_dir = parent / ".git"
    git_dir.mkdir()
    sha = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    (git_dir / "HEAD").write_text(sha + "\n")
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result == real_parent


def test_head_with_short_hex_rejected(tmp_path):
    """A ``.git/HEAD`` containing a short hex string (not 40 chars, not a ref) must NOT
    be accepted."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    git_dir = parent / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("abc123\n")
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        result = compiletools.git_utils._find_git_root(real_child)
    assert result != real_parent


def test_set_allow_fake_git_clears_cache_on_change(tmp_path):
    """Toggling the flag must invalidate cached strict/permissive answers.

    Construct a hierarchy where ``parent/child/`` has a fake empty
    ``parent/.git/`` placeholder above it. Under strict default the walker
    must NOT stop at ``parent`` (no real marker); under permissive mode it
    must. The cache-clear-on-toggle guarantees the flip is observed.
    """
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").mkdir()  # empty/fake placeholder
    real_child = os.path.realpath(child)
    real_parent = os.path.realpath(parent)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git missing")):
        # Strict default: walker rejects parent/.git/ and falls through.
        strict_result = compiletools.git_utils._find_git_root(real_child)
        # Flip the flag — cache must clear so the permissive answer takes over.
        compiletools.git_utils.set_allow_fake_git(True)
        permissive_result = compiletools.git_utils._find_git_root(real_child)
    assert permissive_result == real_parent
    assert strict_result != real_parent
