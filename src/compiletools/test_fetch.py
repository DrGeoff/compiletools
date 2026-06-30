"""Tests for fetch.py — parsing primitives and the git resolver for //#GIT= declarations."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile

import configargparse
import pytest

import compiletools.apptools
import compiletools.headerdeps
from compiletools.build_context import BuildContext
from compiletools.fetch import (
    FetchError,
    GitExternal,
    ResolvedExternal,
    derive_name,
    extract_git_externals,
    fetch_externals,
    parse_git_declaration,
    parse_git_value,
    resolve_external,
)
from compiletools.testhelper import requires_functional_compiler

# ---------------------------------------------------------------------------
# parse_git_value — worked examples from the spec
# ---------------------------------------------------------------------------


def test_parse_git_value_scp_with_ref() -> None:
    url, ref = parse_git_value("git@github.com:me/mylib.git@v1.2.0")
    assert url == "git@github.com:me/mylib.git"
    assert ref == "v1.2.0"


def test_parse_git_value_scp_no_ref() -> None:
    url, ref = parse_git_value("git@github.com:me/mylib.git")
    assert url == "git@github.com:me/mylib.git"
    assert ref is None


def test_parse_git_value_https_with_ref() -> None:
    url, ref = parse_git_value("https://github.com/me/mylib.git@v1.2.0")
    assert url == "https://github.com/me/mylib.git"
    assert ref == "v1.2.0"


def test_parse_git_value_https_no_ref() -> None:
    url, ref = parse_git_value("https://github.com/me/mylib.git")
    assert url == "https://github.com/me/mylib.git"
    assert ref is None


def test_parse_git_value_file_with_ref() -> None:
    url, ref = parse_git_value("file:///tmp/x/mylib@abc123")
    assert url == "file:///tmp/x/mylib"
    assert ref == "abc123"


def test_parse_git_value_file_no_ref() -> None:
    url, ref = parse_git_value("file:///tmp/x/mylib")
    assert url == "file:///tmp/x/mylib"
    assert ref is None


def test_parse_git_value_scp_shorthand_with_ref() -> None:
    """scp shorthand with no slash in path: git@host:mylib.git@v1"""
    url, ref = parse_git_value("git@host:mylib.git@v1")
    assert url == "git@host:mylib.git"
    assert ref == "v1"


# ---------------------------------------------------------------------------
# parse_git_value — whitespace stripping
# ---------------------------------------------------------------------------


def test_parse_git_value_strips_surrounding_whitespace() -> None:
    url, ref = parse_git_value("  https://github.com/me/mylib.git@v1.2.0  ")
    assert url == "https://github.com/me/mylib.git"
    assert ref == "v1.2.0"


def test_parse_git_value_strips_whitespace_no_ref() -> None:
    url, ref = parse_git_value("  https://github.com/me/mylib.git  ")
    assert url == "https://github.com/me/mylib.git"
    assert ref is None


# ---------------------------------------------------------------------------
# parse_git_value — error cases
# ---------------------------------------------------------------------------


def test_parse_git_value_empty_string_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_git_value("")


def test_parse_git_value_whitespace_only_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_git_value("   ")


def test_parse_git_value_trailing_at_raises() -> None:
    """Trailing @ with empty ref is an error."""
    with pytest.raises(ValueError, match="ref"):
        parse_git_value("https://github.com/me/mylib.git@")


def test_parse_git_value_trailing_at_scp_raises() -> None:
    with pytest.raises(ValueError, match="ref"):
        parse_git_value("git@github.com:me/mylib.git@")


def test_parse_git_value_no_separator_raises() -> None:
    """A degenerate value with neither '/' nor ':' is not a valid git URL.

    Without a separator, sep == -1 and the old code silently mis-split
    'git@host' into url='git', ref='host'. A real git URL always carries
    a ':' (scp/scheme) or '/' (path), so reject such input outright.
    """
    with pytest.raises(ValueError, match="separator"):
        parse_git_value("git@host")


# ---------------------------------------------------------------------------
# parse_git_value — documented v1 limitation (pin current behavior)
# ---------------------------------------------------------------------------


def test_parse_git_value_branch_ref_with_slash_not_supported() -> None:
    """v1 LIMITATION: a branch ref containing '/' defeats the separator
    heuristic, so the '@feature' part stays glued onto the URL and ref is
    None.

    This test pins the CURRENT (intentionally-limited) behavior. When a
    future task adds proper support for slash-bearing refs, this test is
    the one to update — the URL should then become 'git@host:repo.git'
    with ref 'feature/foo'.
    """
    url, ref = parse_git_value("git@host:repo.git@feature/foo")
    assert url == "git@host:repo.git@feature/foo"
    assert ref is None


# ---------------------------------------------------------------------------
# derive_name — worked examples from the spec
# ---------------------------------------------------------------------------


def test_derive_name_scp_slash_path() -> None:
    assert derive_name("git@github.com:me/mylib.git") == "mylib"


def test_derive_name_https() -> None:
    assert derive_name("https://github.com/me/mylib.git") == "mylib"


def test_derive_name_file_no_git_suffix() -> None:
    assert derive_name("file:///tmp/x/mylib") == "mylib"


def test_derive_name_scp_shorthand_no_slash() -> None:
    assert derive_name("git@host:mylib.git") == "mylib"


def test_derive_name_strips_git_suffix_only_once() -> None:
    """A repo named 'foo.git.git' should become 'foo.git', not 'foo'."""
    assert derive_name("https://example.com/foo.git.git") == "foo.git"


def test_derive_name_no_git_suffix() -> None:
    assert derive_name("https://example.com/myrepo") == "myrepo"


def test_derive_name_empty_basename_raises() -> None:
    """URL ending in '/' has an empty basename — should raise."""
    with pytest.raises(ValueError, match="basename is empty"):
        derive_name("https://example.com/")


# ---------------------------------------------------------------------------
# parse_git_declaration — convenience combinator
# ---------------------------------------------------------------------------


def test_parse_git_declaration_scp_with_ref() -> None:
    result = parse_git_declaration("git@github.com:me/mylib.git@v1.2.0")
    assert result == GitExternal(name="mylib", url="git@github.com:me/mylib.git", ref="v1.2.0")


def test_parse_git_declaration_https_no_ref() -> None:
    result = parse_git_declaration("https://github.com/me/mylib.git")
    assert result == GitExternal(name="mylib", url="https://github.com/me/mylib.git", ref=None)


def test_parse_git_declaration_file_with_ref() -> None:
    result = parse_git_declaration("file:///tmp/x/mylib@abc123")
    assert result == GitExternal(name="mylib", url="file:///tmp/x/mylib", ref="abc123")


def test_parse_git_declaration_empty_string_raises() -> None:
    """ValueError from parse_git_value propagates through the combinator."""
    with pytest.raises(ValueError, match="empty"):
        parse_git_declaration("")


def test_parse_git_declaration_trailing_at_raises() -> None:
    """ValueError for an empty ref propagates through the combinator."""
    with pytest.raises(ValueError, match="ref"):
        parse_git_declaration("https://github.com/me/mylib.git@")


def test_parse_git_declaration_empty_basename_raises() -> None:
    """ValueError from derive_name (URL ending in '/') propagates."""
    with pytest.raises(ValueError, match="basename is empty"):
        parse_git_declaration("https://example.com/")


# ---------------------------------------------------------------------------
# GitExternal dataclass properties
# ---------------------------------------------------------------------------


def test_git_external_is_frozen() -> None:
    ext = GitExternal(name="mylib", url="https://example.com/mylib.git", ref=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ext.name = "other"  # type: ignore[misc]


def test_git_external_equality() -> None:
    a = GitExternal(name="mylib", url="https://example.com/mylib.git", ref="v1")
    b = GitExternal(name="mylib", url="https://example.com/mylib.git", ref="v1")
    assert a == b


def test_git_external_inequality_on_ref() -> None:
    a = GitExternal(name="mylib", url="https://example.com/mylib.git", ref="v1")
    b = GitExternal(name="mylib", url="https://example.com/mylib.git", ref=None)
    assert a != b


# ===========================================================================
# Resolver + git operations (Task 2)
#
# All git operations are exercised against *local* bare repos cloned via
# file:// URLs — no network access required, fully deterministic.
# ===========================================================================


def _git(cwd: str, *args: str) -> str:
    """Run a git command in *cwd*, return stripped stdout. Raise on failure."""
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        stderr=subprocess.STDOUT,
        text=True,
        env=_git_env(),
    ).strip()


def _git_env() -> dict:
    """Deterministic git environment: fixed identity, no ambient-config bleed.

    Disabling both the system (``GIT_CONFIG_NOSYSTEM``) and global
    (``GIT_CONFIG_GLOBAL=os.devnull``, git >= 2.32) config layers keeps a CI
    machine's ``/etc/gitconfig`` or a developer's ``~/.gitconfig`` (e.g.
    ``commit.gpgsign=true`` or ``transfer.fsckObjects=true``) from breaking
    the bare-repo setup and clones.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "ct-test",
            "GIT_AUTHOR_EMAIL": "ct-test@example.com",
            "GIT_COMMITTER_NAME": "ct-test",
            "GIT_COMMITTER_EMAIL": "ct-test@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": env.get("HOME", "/tmp"),
        }
    )
    return env


def _make_bare_origin(root: str) -> dict:
    """Build a bare git repo with a known history and return ref metadata.

    History layout:
        c1 (master)            tag: v1
        c2 (master, branch tip)
        cf (feature)           branched off c1

    Returns a dict with keys: ``url`` (file:// URL of the bare repo),
    ``c1``, ``c2``, ``cf`` (full SHAs), ``default_branch`` (the bare repo's
    HEAD branch name).
    """
    work = os.path.join(root, "work")
    bare = os.path.join(root, "origin.git")
    os.makedirs(work)
    _git(root, "init", "-q", "-b", "master", work)

    with open(os.path.join(work, "a.txt"), "w") as fh:
        fh.write("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "c1")
    c1 = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "v1")

    # feature branch off c1
    _git(work, "checkout", "-q", "-b", "feature")
    with open(os.path.join(work, "feat.txt"), "w") as fh:
        fh.write("feat\n")
    _git(work, "add", "feat.txt")
    _git(work, "commit", "-q", "-m", "cf")
    cf = _git(work, "rev-parse", "HEAD")

    # second commit on master
    _git(work, "checkout", "-q", "master")
    with open(os.path.join(work, "a.txt"), "w") as fh:
        fh.write("two\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "c2")
    c2 = _git(work, "rev-parse", "HEAD")

    # Publish a bare clone to serve as the remote origin.
    _git(root, "clone", "-q", "--bare", work, bare)
    # Ensure the bare repo's HEAD points at master.
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/master")
    # Wire the work tree to push back to the bare repo (used by _advance_branch
    # to simulate the remote advancing past a previously-taken clone).
    _git(work, "remote", "add", "origin", bare)

    return {
        "url": "file://" + bare,
        "bare": bare,
        "work": work,
        "c1": c1,
        "c2": c2,
        "cf": cf,
        "default_branch": "master",
    }


def _read_file(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _advance_branch(origin: dict, branch: str, content: str) -> str:
    """Add a commit to *branch* in the origin's work tree, push it to the bare
    repo, and return the new SHA. Lets a local clone fall behind the remote.
    """
    work = origin["work"]
    _git(work, "checkout", "-q", branch)
    with open(os.path.join(work, f"{branch}-extra.txt"), "w") as fh:
        fh.write(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"advance {branch}")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", branch)
    return sha


# ---------------------------------------------------------------------------
# Clone-when-missing
# ---------------------------------------------------------------------------


def test_resolve_clone_when_missing_no_ref() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref=None)
        externals = os.path.join(root, "externals")
        res = resolve_external(ext, externals_dir=externals)

        assert res.source == "managed"
        assert res.path == os.path.join(externals, "mylib")
        assert os.path.isdir(os.path.join(res.path, ".git"))
        # Default branch checked out → second master commit.
        assert res.on_disk_ref == origin["c2"]
        assert _read_file(os.path.join(res.path, "a.txt")) == "two\n"


def test_resolve_clone_when_missing_with_tag() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref="v1")
        externals = os.path.join(root, "externals")
        res = resolve_external(ext, externals_dir=externals)

        assert res.on_disk_ref == origin["c1"]
        assert _read_file(os.path.join(res.path, "a.txt")) == "one\n"


def test_resolve_clone_when_missing_with_sha() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref=origin["c1"])
        externals = os.path.join(root, "externals")
        res = resolve_external(ext, externals_dir=externals)

        assert res.on_disk_ref == origin["c1"]


def test_resolve_clone_when_missing_with_branch() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref="feature")
        externals = os.path.join(root, "externals")
        res = resolve_external(ext, externals_dir=externals)

        assert res.on_disk_ref == origin["cf"]
        assert os.path.isfile(os.path.join(res.path, "feat.txt"))


# ---------------------------------------------------------------------------
# Present + at-ref → no network (verified by deleting the remote first)
# ---------------------------------------------------------------------------


def test_resolve_present_at_ref_no_network() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref="v1")
        externals = os.path.join(root, "externals")
        resolve_external(ext, externals_dir=externals)

        # Make the remote unreachable; a no-network resolve must still succeed.
        shutil.rmtree(origin["bare"])

        res = resolve_external(ext, externals_dir=externals)
        assert res.on_disk_ref == origin["c1"]


def test_resolve_present_no_ref_no_network() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        ext = GitExternal(name="mylib", url=origin["url"], ref=None)
        externals = os.path.join(root, "externals")
        resolve_external(ext, externals_dir=externals)

        shutil.rmtree(origin["bare"])

        res = resolve_external(ext, externals_dir=externals)
        assert res.on_disk_ref == origin["c2"]


# ---------------------------------------------------------------------------
# Present + immutable ref differs → fetch + checkout updates HEAD
# ---------------------------------------------------------------------------


def test_resolve_present_tag_differs_checks_out() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        # First clone at the default branch (c2).
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        # Now request tag v1 (== c1); already-local tag, checkout without fetch.
        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        assert res.on_disk_ref == origin["c1"]


def test_resolve_present_sha_not_local_fetches() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        # A commit created on origin AFTER the clone is not present locally;
        # resolving it must trigger a fetch + checkout.
        new_sha = _advance_branch(origin, "master", "later\n")
        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref=new_sha), externals_dir=externals)
        assert res.on_disk_ref == new_sha


# ---------------------------------------------------------------------------
# Present + branch differs
# ---------------------------------------------------------------------------


def test_resolve_present_branch_differs_no_update_warns(capsys: pytest.CaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        # Clone branch feature (local branch tip == cf), then detach HEAD onto
        # c1 so HEAD differs from the local branch tip.
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="feature"), externals_dir=externals)
        _git(target, "checkout", "-q", "--detach", origin["c1"])
        assert _git(target, "rev-parse", "HEAD") == origin["c1"]

        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref="feature"), externals_dir=externals)
        # Left as-is (still detached at c1), not switched to the branch tip.
        assert res.on_disk_ref == origin["c1"]
        err = capsys.readouterr().err
        assert "feature" in err
        assert "mylib" in err


def test_resolve_present_branch_differs_with_update_fast_forwards() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="feature"), externals_dir=externals)
        # Advance the remote feature branch so the local clone is behind.
        new_tip = _advance_branch(origin, "feature", "more\n")

        res = resolve_external(
            GitExternal(name="mylib", url=origin["url"], ref="feature"),
            externals_dir=externals,
            update=True,
        )
        assert res.on_disk_ref == new_tip


def test_resolve_present_no_ref_update_pulls() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        externals_target = os.path.join(externals, "mylib")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        assert _git(externals_target, "rev-parse", "HEAD") == origin["c2"]
        # Advance the remote default branch so the local clone is behind.
        new_tip = _advance_branch(origin, "master", "three\n")

        res = resolve_external(
            GitExternal(name="mylib", url=origin["url"], ref=None),
            externals_dir=externals,
            update=True,
        )
        assert res.on_disk_ref == new_tip


# ---------------------------------------------------------------------------
# no_fetch + missing → error naming external + url + git clone command
# ---------------------------------------------------------------------------


def test_resolve_no_fetch_missing_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        ext = GitExternal(name="mylib", url=origin["url"], ref="v1")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(ext, externals_dir=externals, no_fetch=True)
        msg = str(excinfo.value)
        assert "mylib" in msg
        assert origin["url"] in msg
        assert "git clone" in msg
        assert "--git-path" in msg


def test_resolve_no_fetch_present_sha_not_local_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        # Create a commit on origin the clone never received.
        new_sha = _advance_branch(origin, "master", "future\n")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref=new_sha),
                externals_dir=externals,
                no_fetch=True,
            )
        assert "mylib" in str(excinfo.value)


# ---------------------------------------------------------------------------
# override_path
# ---------------------------------------------------------------------------


def test_resolve_override_path_used_untouched() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        override = os.path.join(root, "mylocal")
        _git(root, "clone", "-q", origin["url"], override)
        _git(override, "checkout", "-q", origin["c1"])
        before = _git(override, "rev-parse", "HEAD")

        res = resolve_external(
            GitExternal(name="mylib", url=origin["url"], ref="v1"),
            externals_dir=externals,
            override_path=override,
        )
        assert res.source == "override"
        assert res.path == override
        assert res.on_disk_ref == before
        # Nothing was cloned into the managed location.
        assert not os.path.exists(os.path.join(externals, "mylib"))
        # The override checkout is untouched.
        assert _git(override, "rev-parse", "HEAD") == before


def test_resolve_override_path_missing_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        ext = GitExternal(name="mylib", url=origin["url"], ref="v1")
        missing = os.path.join(root, "does-not-exist")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(ext, externals_dir=externals, override_path=missing)
        msg = str(excinfo.value)
        assert "--git-path" in msg
        assert "mylib" in msg


def test_resolve_override_path_non_git_dir() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        override = os.path.join(root, "plaindir")
        os.makedirs(override)
        with open(os.path.join(override, "x.txt"), "w") as fh:
            fh.write("hi\n")
        res = resolve_external(
            GitExternal(name="mylib", url=origin["url"], ref="v1"),
            externals_dir=externals,
            override_path=override,
        )
        assert res.source == "override"
        assert res.path == override
        assert res.on_disk_ref is None


# ---------------------------------------------------------------------------
# Dirty tree + checkout required → refuse
# ---------------------------------------------------------------------------


def test_resolve_dirty_tree_checkout_required_refuses() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        # Dirty the work tree.
        with open(os.path.join(target, "a.txt"), "w") as fh:
            fh.write("dirty\n")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        msg = str(excinfo.value)
        assert "mylib" in msg
        # File still dirty, unchanged.
        assert _read_file(os.path.join(target, "a.txt")) == "dirty\n"


def test_resolve_dirty_tree_update_branch_refuses() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="feature"), externals_dir=externals)
        _git(target, "reset", "-q", "--hard", origin["c1"])
        with open(os.path.join(target, "feat.txt"), "w") as fh:
            fh.write("dirty\n")
        with pytest.raises(FetchError):
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref="feature"),
                externals_dir=externals,
                update=True,
            )


def test_resolve_dirty_tree_update_no_ref_refuses() -> None:
    """Symmetry with the branch case: ref=None + --update on a dirty tree
    must refuse the pull rather than clobber local changes.
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        with open(os.path.join(target, "a.txt"), "w") as fh:
            fh.write("dirty\n")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref=None),
                externals_dir=externals,
                update=True,
            )
        assert "mylib" in str(excinfo.value)
        # Local changes preserved.
        assert _read_file(os.path.join(target, "a.txt")) == "dirty\n"


# ---------------------------------------------------------------------------
# ref not found → error
# ---------------------------------------------------------------------------


def test_resolve_ref_not_found_on_clone_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        ext = GitExternal(name="mylib", url=origin["url"], ref="nonexistent-ref")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(ext, externals_dir=externals)
        msg = str(excinfo.value)
        assert "mylib" in msg


def test_resolve_clone_bad_url_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        bad_url = "file://" + os.path.join(root, "no-such-repo.git")
        ext = GitExternal(name="mylib", url=bad_url, ref=None)
        with pytest.raises(FetchError) as excinfo:
            resolve_external(ext, externals_dir=externals)
        msg = str(excinfo.value)
        assert "mylib" in msg
        assert bad_url in msg


# ---------------------------------------------------------------------------
# target present but not a git work tree → use as-is, warn, source managed
# ---------------------------------------------------------------------------


def test_resolve_target_present_non_git_warns(capsys: pytest.CaptureFixture) -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        os.makedirs(target)
        marker = os.path.join(target, "user-file.txt")
        with open(marker, "w") as fh:
            fh.write("precious\n")

        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        assert res.source == "managed"
        assert res.path == target
        assert res.on_disk_ref is None
        # Nothing destroyed.
        assert os.path.isfile(marker)
        err = capsys.readouterr().err
        assert "mylib" in err


# ---------------------------------------------------------------------------
# ResolvedExternal dataclass
# ---------------------------------------------------------------------------


def test_resolved_external_is_frozen() -> None:
    res = ResolvedExternal(name="x", url="u", ref=None, path="/p", source="managed", on_disk_ref=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.name = "y"  # type: ignore[misc]


# ===========================================================================
# Source-scanning + fixpoint driver (Task 3)
#
# Drives the real file_analyzer / headerdeps machinery over on-disk sources.
# A functional C++ compiler is required because DirectHeaderDeps probes the
# compiler for its built-in macro set; the headerdeps walk itself is direct
# (no compilation). All git operations stay local via file:// bare repos.
# ===========================================================================


def _make_args(verbose: int = 0) -> object:
    """Build a realistic args namespace for headerdeps / file_analyzer.

    Mirrors the established headerdeps test pattern: register the headerdeps
    argument surface on a configargparse parser and run it through
    ``apptools.parseargs`` so every attribute the analysis machinery expects
    (CXX, CPPFLAGS, magic, headerdeps, verbose, exemarkers, …) is populated.
    """
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.headerdeps.add_arguments(cap)
    compiletools.apptools.add_common_arguments(cap)
    argv = ["--headerdeps", "direct"]
    if verbose:
        argv += ["--verbose", str(verbose)]
    return compiletools.apptools.parseargs(cap, argv, context=BuildContext())


def _make_bare_with_files(root: str, name: str, files: dict[str, str]) -> dict:
    """Create a bare git repo named *name* whose initial commit holds *files*.

    *files* maps relative path -> file content. Returns a dict with ``url``
    (file:// URL), ``bare``, ``work``, and ``sha`` (the single commit's SHA).
    """
    work = os.path.join(root, name + "-work")
    bare = os.path.join(root, name + ".git")
    os.makedirs(work)
    _git(root, "init", "-q", "-b", "master", work)
    for rel, content in files.items():
        dest = os.path.join(work, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh:
            fh.write(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    sha = _git(work, "rev-parse", "HEAD")
    _git(root, "clone", "-q", "--bare", work, bare)
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/master")
    return {"url": "file://" + bare, "bare": bare, "work": work, "sha": sha}


@requires_functional_compiler
def test_fetch_externals_single() -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "mylib/foo.h"\nint main() {{ return foo(); }}\n')

        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)

        assert [r.name for r in results] == ["mylib"]
        res = results[0]
        assert res.source == "managed"
        assert res.path == os.path.join(externals, "mylib")
        assert os.path.isfile(os.path.join(res.path, "foo.h"))
        assert res.on_disk_ref == ext["sha"]


@requires_functional_compiler
def test_fetch_externals_transitive() -> None:
    """The key test: extA's own header declares //#GIT for extB.

    Proves the fixpoint + include augmentation traverse INTO a fetched
    external to discover deps-of-deps.
    """
    with tempfile.TemporaryDirectory() as root:
        ext_b = _make_bare_with_files(root, "extB", {"b.h": "#pragma once\nint b();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        # extA's header both declares extB and includes it.
        ext_a = _make_bare_with_files(
            root,
            "extA",
            {"a.h": f'#pragma once\n//#GIT={ext_b["url"]}@master\n#include "extB/b.h"\nint a();\n'},
        )
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext_a["url"]}@master\n#include "extA/a.h"\nint main() {{ return a(); }}\n')

        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)

        names = sorted(r.name for r in results)
        assert names == ["extA", "extB"], f"expected both externals fetched, got {names}"
        assert os.path.isfile(os.path.join(externals, "extA", "a.h"))
        assert os.path.isfile(os.path.join(externals, "extB", "b.h"))


@requires_functional_compiler
def test_fetch_externals_duplicate_name_different_url_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        # Two repos whose URL basenames BOTH derive to 'mylib' (a name
        # collision) but whose URLs differ — placing the second under a
        # subdir keeps its basename 'mylib.git'.
        ext1 = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\n"})
        sub = os.path.join(root, "sub")
        os.makedirs(sub)
        ext2 = _make_bare_with_files(sub, "mylib", {"bar.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)

        main = os.path.join(root, "main.cpp")
        other = os.path.join(root, "other.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={ext1['url']}@master\nint main() {{ return 0; }}\n")
        with open(other, "w") as fh:
            fh.write(f"//#GIT={ext2['url']}@master\nvoid f() {{}}\n")

        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main, other], _make_args(), BuildContext(), externals_dir=externals)
        msg = str(excinfo.value)
        assert "mylib" in msg
        assert ext1["url"] in msg
        assert ext2["url"] in msg


@requires_functional_compiler
def test_fetch_externals_same_name_same_url_deduped() -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        other = os.path.join(root, "other.cpp")
        decl = f"//#GIT={ext['url']}@master\n"
        with open(main, "w") as fh:
            fh.write(decl + "int main() { return 0; }\n")
        with open(other, "w") as fh:
            fh.write(decl + "void f() {}\n")

        results = fetch_externals([main, other], _make_args(), BuildContext(), externals_dir=externals)
        assert [r.name for r in results] == ["mylib"]


@requires_functional_compiler
def test_fetch_externals_override_uses_local_path() -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        # A pre-existing local checkout to override with.
        local = os.path.join(root, "mylocal")
        _git(root, "clone", "-q", ext["url"], local)

        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={ext['url']}@master\nint main() {{ return 0; }}\n")

        results = fetch_externals(
            [main],
            _make_args(),
            BuildContext(),
            externals_dir=externals,
            overrides={"mylib": local},
        )
        assert len(results) == 1
        res = results[0]
        assert res.source == "override"
        assert res.path == local
        # Nothing cloned into the managed location.
        assert not os.path.exists(os.path.join(externals, "mylib"))


@requires_functional_compiler
def test_fetch_externals_no_fetch_missing_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={ext['url']}@master\nint main() {{ return 0; }}\n")

        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals, no_fetch=True)
        assert "mylib" in str(excinfo.value)


@requires_functional_compiler
def test_fetch_externals_second_call_no_network() -> None:
    """A second fetch with everything already present does no network."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "mylib/foo.h"\nint main() {{ return foo(); }}\n')

        fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)
        # Make the remote unreachable; the second resolve must still succeed.
        shutil.rmtree(ext["bare"])

        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)
        assert [r.name for r in results] == ["mylib"]
        assert results[0].on_disk_ref == ext["sha"]


@requires_functional_compiler
def test_fetch_externals_malformed_value_errors() -> None:
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            # Trailing '@' with empty ref → parse_git_declaration raises ValueError.
            fh.write("//#GIT=https://example.com/x.git@\nint main() { return 0; }\n")

        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)
        assert main in str(excinfo.value)


def test_extract_git_externals_tolerates_missing_file() -> None:
    """A file the registry cannot resolve yields [] rather than aborting."""
    with tempfile.TemporaryDirectory() as root:
        missing = os.path.join(root, "does-not-exist.cpp")
        assert extract_git_externals(missing, _make_args(), BuildContext()) == []


@requires_functional_compiler
def test_extract_git_externals_skips_non_git_flags() -> None:
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        src = os.path.join(root, "main.cpp")
        with open(src, "w") as fh:
            fh.write(
                "//#CXXFLAGS=-O2\n//#GIT=https://example.com/mylib.git@v1\n//#LDFLAGS=-lm\nint main() { return 0; }\n"
            )
        externals_found = extract_git_externals(src, _make_args(), BuildContext())
        assert externals_found == [GitExternal(name="mylib", url="https://example.com/mylib.git", ref="v1")]
