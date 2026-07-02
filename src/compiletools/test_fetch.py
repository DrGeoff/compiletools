"""Tests for fetch.py — parsing primitives and the git resolver for //#GIT= declarations."""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from unittest.mock import patch

import configargparse
import pytest

import compiletools.apptools
import compiletools.fetch as fetch
import compiletools.headerdeps
from compiletools.build_context import BuildContext
from compiletools.fetch import (
    FetchError,
    GitExternal,
    ResolvedExternal,
    derive_name,
    extract_git_allow_protocols,
    extract_git_externals,
    fetch_externals,
    parse_git_declaration,
    parse_git_path_overrides,
    parse_git_value,
    resolve_external,
    resolve_externals_dir,
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


def test_resolve_no_fetch_present_branch_update_errors_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """no_fetch must veto a branch --update: raise offline, run no network git."""
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        # Clone the feature branch (present, on-branch work tree).
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="feature"), externals_dir=externals)
        # Advance the remote so an actual --update would fast-forward.
        _advance_branch(origin, "feature", "more\n")

        # Any network/mutating git op (fetch/checkout/merge/pull/clone) goes
        # through _run_git; make it explode so the test proves none ran.
        def _boom(*_args, **_kwargs):
            raise AssertionError("network git op attempted under no_fetch")

        monkeypatch.setattr(fetch, "_run_git", _boom)

        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref="feature"),
                externals_dir=externals,
                no_fetch=True,
                update=True,
            )
        msg = str(excinfo.value)
        assert "mylib" in msg
        assert "no-fetch" in msg.lower() or "offline" in msg.lower()


def test_resolve_no_fetch_present_no_ref_update_errors_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """no_fetch must veto a no-ref --update pull: raise offline, run no network git."""
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        # Clone with no ref (tracks the default branch).
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        # Advance the remote so an actual --update would pull.
        _advance_branch(origin, "master", "three\n")

        def _boom(*_args, **_kwargs):
            raise AssertionError("network git op attempted under no_fetch")

        monkeypatch.setattr(fetch, "_run_git", _boom)

        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref=None),
                externals_dir=externals,
                no_fetch=True,
                update=True,
            )
        msg = str(excinfo.value)
        assert "mylib" in msg
        assert "no-fetch" in msg.lower() or "offline" in msg.lower()


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


def _make_args(verbose: int = 0) -> argparse.Namespace:
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
@pytest.mark.parametrize("override_key", ["mylib", "MyLib"])
def test_fetch_externals_override_matches_mixed_case_name(override_key: str) -> None:
    """A mixed-case URL basename (``MyLib`` -> derive_name ``MyLib``) is matched
    case-insensitively by an override key given in any case.

    Regression: the lookup previously used the raw ``ext.name`` while
    parse_git_path_overrides lowercased its keys, so a mixed-case external
    could never hit an override (silent clone attempt instead)."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "MyLib", {"foo.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        local = os.path.join(root, "mylocal")
        _git(root, "clone", "-q", ext["url"], local)

        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={ext['url']}@master\nint main() {{ return 0; }}\n")

        # Build the overrides dict through the real CLI parser so the whole
        # normalization path is exercised: a user-supplied key in ANY case
        # (mylib or MyLib) must match the MyLib-basename external.
        overrides = parse_git_path_overrides([f"{override_key}={local}"], {})
        results = fetch_externals(
            [main],
            _make_args(),
            BuildContext(),
            externals_dir=externals,
            overrides=overrides,
        )
        assert len(results) == 1
        res = results[0]
        assert res.name == "MyLib"
        assert res.source == "override"
        assert res.path == local
        # The override matched, so nothing was cloned into the managed location.
        assert not os.path.exists(os.path.join(externals, "MyLib"))


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
def test_present_checkout_origin_mismatch_warns(capsys) -> None:
    """A managed checkout whose origin differs from the //#GIT= url is used
    as-is but warns (name-collision under the sibling layout)."""
    with tempfile.TemporaryDirectory() as root:
        # Clone repo A into externals/mylib, then declare mylib -> repo B's url.
        # repo A is cloned into externals/mylib; the //#GIT= declaration derives
        # the SAME name 'mylib' (its basename is mylib.git) but points at repo B.
        repo_a = _make_bare_with_files(root, "mylib", {"a.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        subprocess.check_call(
            ["git", "clone", "-q", repo_a["url"], os.path.join(externals, "mylib")],
            env=fetch._git_env(),
        )
        # repo B: a distinct repo whose bare dir basename is also 'mylib' so its
        # derived external name collides with the present checkout of repo A.
        repo_b_work = os.path.join(root, "b-work")
        os.makedirs(repo_b_work)
        subprocess.check_call(["git", "init", "-q", "-b", "master", repo_b_work], env=fetch._git_env())
        with open(os.path.join(repo_b_work, "b.h"), "w") as fh:
            fh.write("#pragma once\n")
        subprocess.check_call(["git", "add", "-A"], cwd=repo_b_work, env=fetch._git_env())
        subprocess.check_call(["git", "commit", "-q", "-m", "b"], cwd=repo_b_work, env=fetch._git_env())
        repo_b_bare = os.path.join(root, "bdir", "mylib.git")
        os.makedirs(os.path.dirname(repo_b_bare))
        subprocess.check_call(["git", "clone", "-q", "--bare", repo_b_work, repo_b_bare], env=fetch._git_env())

        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            # No ref -> no update -> the present checkout is used as-is (a no-op),
            # so the only observable effect is the origin-mismatch warning.
            fh.write(f"//#GIT=file://{repo_b_bare}\nint main() {{ return 0; }}\n")

        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)
        assert [r.name for r in results] == ["mylib"]
        err = capsys.readouterr().err
        assert "origin" in err and "mylib" in err


def test_normalize_remote_url_folds_equivalent_spellings() -> None:
    """scp-vs-ssh, host case, user@ prefix, and trailing '/'/'.git' fold to one
    canonical form so the origin-mismatch check does not warn spuriously;
    genuinely different remotes (and case-differing paths) stay distinct."""
    n = fetch._normalize_remote_url
    canonical = n("https://github.com/org/repo")
    # Equivalent spellings of the SAME remote.
    assert n("https://github.com/org/repo.git") == canonical
    assert n("https://github.com/org/repo/") == canonical
    assert n("git@github.com:org/repo.git") == canonical
    assert n("ssh://git@github.com/org/repo") == canonical
    assert n("https://GitHub.com/org/repo") == canonical
    assert n("git://github.com/org/repo.git") == canonical
    # Genuinely different remotes must NOT fold together.
    assert n("https://github.com/org/other") != canonical
    assert n("https://gitlab.com/org/repo") != canonical
    # Repo paths are case-sensitive on most forges — keep them distinct.
    assert n("https://github.com/org/Repo") != canonical
    # file:// URLs fold on their path (scheme + trailing '/'/'.git' stripped).
    assert n("file:///srv/x/mylib.git") == n("file:///srv/x/mylib/")


@requires_functional_compiler
def test_fetch_externals_parallel_resolves_all_in_declaration_order() -> None:
    """Multiple independent externals discovered in one round are resolved in a
    thread pool, yet the result list stays in stable declaration order.

    All three externals are declared in a single source file, so they are
    discovered in the same fixpoint round and handed to the parallel resolver at
    once. This exercises finding 7 (bounded ThreadPoolExecutor) and pins the
    deterministic ordering contract.
    """
    with tempfile.TemporaryDirectory() as root:
        exts = [
            _make_bare_with_files(root, name, {f"{name}.h": "#pragma once\n"}) for name in ("alpha", "beta", "gamma")
        ]
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            decls = "".join(f"//#GIT={e['url']}@master\n" for e in exts)
            fh.write(decls + "int main() { return 0; }\n")

        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)

        # Declaration order (alpha, beta, gamma) is preserved despite parallelism.
        assert [r.name for r in results] == ["alpha", "beta", "gamma"]
        for name in ("alpha", "beta", "gamma"):
            assert os.path.isfile(os.path.join(externals, name, f"{name}.h"))


@requires_functional_compiler
def test_fetch_externals_parallel_one_bad_url_raises_fetcherror() -> None:
    """A failure in any parallel worker surfaces as a FetchError naming the
    offender; a good peer resolving concurrently does not mask it."""
    with tempfile.TemporaryDirectory() as root:
        good = _make_bare_with_files(root, "goodlib", {"g.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(
                f"//#GIT={good['url']}@master\n"
                f"//#GIT=file://{root}/does-not-exist-repo.git@master\n"
                "int main() { return 0; }\n"
            )

        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals, no_fetch=False)
        # The named offender is the missing repo (its derived name).
        assert "does-not-exist-repo" in str(excinfo.value)


@requires_functional_compiler
def test_fetch_externals_parallel_first_bad_url_in_declaration_order_wins() -> None:
    """With two failing externals discovered in the same parallel round, the
    FetchError names the FIRST one in declaration order (deterministic, matching
    a sequential run), regardless of which worker thread fails first."""
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(
                f"//#GIT=file://{root}/first-missing.git@master\n"
                f"//#GIT=file://{root}/second-missing.git@master\n"
                "int main() { return 0; }\n"
            )
        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)
        assert "first-missing" in str(excinfo.value)
        assert "second-missing" not in str(excinfo.value)


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


@requires_functional_compiler
def test_extract_git_externals_tolerates_missing_file() -> None:
    """A file the registry cannot resolve yields [] rather than aborting."""
    with tempfile.TemporaryDirectory() as root:
        missing = os.path.join(root, "does-not-exist.cpp")
        assert extract_git_externals(missing, _make_args(), BuildContext()) == []


@requires_functional_compiler
def test_git_declaration_in_dead_conditional_is_still_extracted() -> None:
    """//#GIT= is extracted regardless of preprocessor conditionals: a
    declaration inside `#if 0` is still discovered. This is intentional
    (evaluating conditionals may require the not-yet-fetched external's own
    headers) and is pinned here so a future change is deliberate."""
    with tempfile.TemporaryDirectory() as root:
        src = os.path.join(root, "main.cpp")
        with open(src, "w") as fh:
            fh.write("#if 0\n//#GIT=file:///x/deadlib.git\n#endif\nint main() { return 0; }\n")
        exts = extract_git_externals(src, _make_args(), BuildContext())
        assert [e.name for e in exts] == ["deadlib"]


@requires_functional_compiler
def test_extract_git_allow_protocols_reads_declarations() -> None:
    with tempfile.TemporaryDirectory() as root:
        src = os.path.join(root, "main.cpp")
        with open(src, "w") as fh:
            fh.write(
                "//#GIT_ALLOW_PROTOCOL=file:git:ssh:http:https:ext\n"
                "//#GIT=file:///x/mylib.git\n"
                "int main() { return 0; }\n"
            )
        assert extract_git_allow_protocols(src, _make_args(), BuildContext()) == ["file:git:ssh:http:https:ext"]


@requires_functional_compiler
def test_augmented_headerdeps_threads_include_dirs_without_deepcopy() -> None:
    """_augmented_headerdeps no longer deep-copies args, and the external
    include dirs (spaces and all) reach the headerdeps search list.

    The extra dirs are passed through ``headerdeps.create(extra_include_dirs=)``
    and land directly on the DirectHeaderDeps include list as raw path strings —
    no shlex round-trip through CPPFLAGS — so a path with a space survives. And
    because there is no deepcopy, the headerdeps instance holds the caller's
    real args object, and args.CPPFLAGS is left untouched.
    """
    from compiletools.fetch import _augmented_headerdeps

    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "ex ternals")  # space in the path
        os.makedirs(externals)
        space_root = os.path.join(root, "resolved root")  # another space
        os.makedirs(space_root)

        args = _make_args()
        cppflags_before = args.CPPFLAGS
        hd = _augmented_headerdeps(
            args,
            BuildContext(),
            externals_dir=externals,
            resolved_roots=[space_root],
        )

        # No deepcopy: the headerdeps holds the caller's real args object.
        assert hd.args is args
        # The caller's flags are not mutated.
        assert cppflags_before == args.CPPFLAGS

        # The extra include dirs are threaded through verbatim...
        assert externals in hd._extra_include_dirs
        assert space_root in hd._extra_include_dirs
        assert os.path.join(space_root, "include") in hd._extra_include_dirs
        # ...and appended to the DirectHeaderDeps include search list (raw
        # strings, so the embedded spaces survive intact).
        assert externals in hd.includes
        assert space_root in hd.includes
        assert os.path.join(space_root, "include") in hd.includes


@requires_functional_compiler
def test_augmented_headerdeps_searches_externals_dir_last() -> None:
    """externals_dir is appended LAST so a same-named dir inside a resolved root
    is found before the broad siblings-parent dir."""
    args = _make_args()
    context = BuildContext()
    hd = fetch._augmented_headerdeps(
        args,
        context,
        externals_dir="/tmp/externals",
        resolved_roots=["/tmp/externals/alpha", "/tmp/externals/beta"],
    )
    extra = hd._extra_include_dirs
    assert extra[-1] == "/tmp/externals"  # externals_dir searched last
    # Every resolved root (and its include/ subdir) precedes externals_dir.
    assert extra.index("/tmp/externals/alpha") < extra.index("/tmp/externals")
    assert extra.index("/tmp/externals/beta") < extra.index("/tmp/externals")


@requires_functional_compiler
def test_fetch_externals_restores_context_analyzer_args() -> None:
    """After fetch_externals returns, context.analyzer_args is the prior value.

    Each round's _augmented_headerdeps -> headerdeps.create ->
    HeaderDepsBase.__init__ calls set_analyzer_args(args, context), which stores
    the real args into context.analyzer_args. The caller's context must be left
    holding whatever it held before (here None) via the try/finally restore.
    """
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "mylib/foo.h"\nint main() {{ return foo(); }}\n')

        context = BuildContext()
        assert context.analyzer_args is None
        fetch_externals([main], _make_args(), context, externals_dir=externals)
        assert context.analyzer_args is None


@requires_functional_compiler
def test_fetch_externals_restores_context_analyzer_args_on_error() -> None:
    """The restore happens in a finally, so it holds even when fetch_externals raises."""
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            # Trailing '@' → malformed //#GIT= → FetchError mid-fixpoint.
            fh.write("//#GIT=https://example.com/x.git@\nint main() { return 0; }\n")

        context = BuildContext()
        assert context.analyzer_args is None
        with pytest.raises(FetchError):
            fetch_externals([main], _make_args(), context, externals_dir=externals)
        assert context.analyzer_args is None


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


# ===========================================================================
# Pure config helpers (Task 11): parse_git_path_overrides + resolve_externals_dir
# ===========================================================================


def test_parse_git_path_overrides_empty() -> None:
    assert parse_git_path_overrides([], {}) == {}


def test_parse_git_path_overrides_cli_only() -> None:
    result = parse_git_path_overrides(["foo=/abs/foo", "bar=/abs/bar"], {})
    assert result == {"foo": "/abs/foo", "bar": "/abs/bar"}


def test_parse_git_path_overrides_cli_relative_is_absolutized() -> None:
    result = parse_git_path_overrides(["foo=rel/foo"], {})
    assert result["foo"] == os.path.abspath("rel/foo")
    assert os.path.isabs(result["foo"])


def test_parse_git_path_overrides_cli_tilde_is_expanded() -> None:
    """A leading ``~`` in a CLI PATH is expanduser'd: the shell does not
    tilde-expand after the ``=`` in ``--git-path sudoku=~/code/sudoku``, so
    the natural spelling would otherwise become a literal ``<cwd>/~/...``.
    """
    result = parse_git_path_overrides(["foo=~/code/foo"], {})
    assert result["foo"] == os.path.join(os.path.expanduser("~"), "code", "foo")
    assert "~" not in result["foo"]


def test_parse_git_path_overrides_env_tilde_is_expanded() -> None:
    """CT_GIT_PATH_* values get the same expanduser treatment as CLI paths."""
    result = parse_git_path_overrides([], {"CT_GIT_PATH_FOO": "~/code/foo"})
    assert result["foo"] == os.path.join(os.path.expanduser("~"), "code", "foo")


def test_parse_git_path_overrides_env_only() -> None:
    result = parse_git_path_overrides([], {"CT_GIT_PATH_FOO": "/p"})
    assert result == {"foo": "/p"}


def test_parse_git_path_overrides_env_suffix_lowercased() -> None:
    """The suffix after CT_GIT_PATH_ is lowercased to match a derived name."""
    result = parse_git_path_overrides([], {"CT_GIT_PATH_MyLib": "/p"})
    assert result == {"mylib": "/p"}


def test_parse_git_path_overrides_cli_overrides_env() -> None:
    result = parse_git_path_overrides(["foo=/cli"], {"CT_GIT_PATH_FOO": "/env"})
    assert result == {"foo": "/cli"}


def test_parse_git_path_overrides_cli_name_case_normalized() -> None:
    """A CLI NAME is lowercased so it lines up with the env-suffix rule."""
    result = parse_git_path_overrides(["Foo=/cli"], {"CT_GIT_PATH_FOO": "/env"})
    assert result == {"foo": "/cli"}


def test_parse_git_path_overrides_accumulate_and_cli_over_env() -> None:
    """A17: multiple --git-path entries and multiple CT_GIT_PATH_* env vars
    accumulate into one map; where a name is set by both, CLI wins.
    """
    result = parse_git_path_overrides(
        ["foo=/cli/foo", "baz=/cli/baz"],
        {"CT_GIT_PATH_FOO": "/env/foo", "CT_GIT_PATH_BAR": "/env/bar"},
    )
    assert result == {
        "foo": "/cli/foo",  # CLI overrides the env entry of the same name
        "bar": "/env/bar",  # env-only survives
        "baz": "/cli/baz",  # cli-only survives
    }


def test_parse_git_path_overrides_empty_env_skipped_cli_raises() -> None:
    """A14: an empty CT_GIT_PATH_* env value is intentionally skipped, while an
    empty CLI PATH raises — the documented asymmetry.
    """
    # Empty env value → skipped (not an error, not present).
    assert parse_git_path_overrides([], {"CT_GIT_PATH_FOO": ""}) == {}
    # Empty CLI PATH → hard error.
    with pytest.raises(FetchError, match="empty PATH"):
        parse_git_path_overrides(["foo="], {})


def test_parse_git_path_overrides_ignores_unrelated_env() -> None:
    result = parse_git_path_overrides([], {"PATH": "/usr/bin", "CT_GIT_PATH_X": "/x"})
    assert result == {"x": "/x"}


def test_parse_git_path_overrides_no_equals_raises() -> None:
    with pytest.raises(FetchError, match="NAME=PATH"):
        parse_git_path_overrides(["noequals"], {})


def test_parse_git_path_overrides_empty_name_raises() -> None:
    with pytest.raises(FetchError, match="empty NAME"):
        parse_git_path_overrides(["=/p"], {})


def test_parse_git_path_overrides_empty_path_raises() -> None:
    with pytest.raises(FetchError, match="empty PATH"):
        parse_git_path_overrides(["foo="], {})


def test_resolve_externals_dir_explicit_is_absolutized() -> None:
    assert resolve_externals_dir("/abs/ex", "/some/gitroot") == "/abs/ex"
    assert resolve_externals_dir("rel/ex", "/some/gitroot") == os.path.abspath("rel/ex")


def test_resolve_externals_dir_default_is_parent_of_gitroot() -> None:
    assert resolve_externals_dir(None, "/home/u/proj") == "/home/u"
    assert resolve_externals_dir("", "/home/u/proj") == "/home/u"


# ===========================================================================
# CLI surface (Task 11): add_fetch_arguments
# ===========================================================================


def test_add_fetch_arguments_parses_dests() -> None:
    cap = configargparse.ArgumentParser()
    compiletools.apptools.add_fetch_arguments(cap)
    args = cap.parse_args(
        ["--no-fetch", "--update", "--externals-dir", "/x", "--git-path", "a=/b", "--git-path", "c=/d"]
    )
    assert args.no_fetch is True
    assert args.update is True
    assert args.externals_dir == "/x"
    assert args.git_paths == ["a=/b", "c=/d"]


def test_add_fetch_arguments_defaults() -> None:
    cap = configargparse.ArgumentParser()
    compiletools.apptools.add_fetch_arguments(cap)
    args = cap.parse_args([])
    assert args.no_fetch is False
    assert args.update is False
    assert args.externals_dir is None
    assert args.git_paths == []


def test_add_fetch_arguments_idempotent() -> None:
    """Calling twice on the same parser must not raise (guard short-circuits)."""
    cap = configargparse.ArgumentParser()
    compiletools.apptools.add_fetch_arguments(cap)
    compiletools.apptools.add_fetch_arguments(cap)
    args = cap.parse_args(["--no-fetch"])
    assert args.no_fetch is True


# ===========================================================================
# Parallax hardening regressions (no compiler required)
# ===========================================================================
#
# One test per confirmed finding in docs/parallax/PARALLAX_1681e545.json. These
# exercise the parsing primitives and the git resolver directly (file:// bare
# repos); the fixpoint-driver findings (N2, N3, A1-lock, A11) live in
# test_ct_fetch.py where the full headerdeps pipeline / a compiler is available.


# --- A2: git option-injection guard (leading-dash url/ref) ------------------


def test_parse_git_value_leading_dash_url_rejected() -> None:
    """A2: a url beginning with '-' would be read by git as an option."""
    with pytest.raises(ValueError) as excinfo:
        parse_git_value("--upload-pack=/bin/sh/x")
    assert "-" in str(excinfo.value)


def test_parse_git_value_leading_dash_ref_rejected() -> None:
    """A2: a ref beginning with '-' (e.g. '--upload-pack=<cmd>') is an RCE vector."""
    with pytest.raises(ValueError) as excinfo:
        parse_git_value("https://example.com/x.git@--upload-pack=evil")
    assert "may not begin with '-'" in str(excinfo.value)


def test_end_of_options_present_in_git_argv() -> None:
    """A2 suspenders: every untrusted positional is guarded by --end-of-options.

    Assert the source wires the sentinel into the clone/fetch/checkout/merge
    argv rather than a bare '--' (which git checkout reinterprets as a pathspec).
    """
    import inspect

    src = inspect.getsource(fetch)
    assert "--end-of-options" in src
    # '--' as a positional guard is the wrong token for checkout; ensure the
    # clone/fetch/checkout/merge calls use the sentinel, not a bare separator.
    for call in ('"clone", "--end-of-options"', '"checkout", "--end-of-options"'):
        assert call in src, f"expected {call!r} in fetch.py"


# --- N1: derive_name rejects escaping / unsafe names ------------------------


def test_derive_name_rejects_dotdot() -> None:
    """N1: a URL ending '/..' yields '..', which os.path.join would escape."""
    with pytest.raises(ValueError) as excinfo:
        derive_name("file:///tmp/x/..")
    assert "unsafe" in str(excinfo.value)


def test_derive_name_rejects_single_dot() -> None:
    with pytest.raises(ValueError):
        derive_name("file:///tmp/x/.")


def test_derive_name_rejects_dot_leading() -> None:
    """A hidden/dot-leading name (e.g. '.git' collision) is rejected."""
    with pytest.raises(ValueError):
        derive_name("file:///tmp/x/.hidden")


def test_resolve_external_name_escape_rejected() -> None:
    """N1 defense-in-depth: a hand-built GitExternal whose name escapes
    externals_dir is rejected by resolve_external's containment check even
    though derive_name would never produce such a name.
    """
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        ext = GitExternal(name=os.path.join("..", "evil"), url="file:///x.git", ref=None)
        with pytest.raises(FetchError) as excinfo:
            resolve_external(ext, externals_dir=externals)
        assert "escapes" in str(excinfo.value)


# --- A7 / A8 / A20: _git_env behaviour --------------------------------------


def test_git_env_does_not_neutralize_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A7: honour the user's git config so enterprise auth (insteadOf, proxy,
    credential helpers) works. _git_env must NOT set GIT_CONFIG_GLOBAL /
    GIT_CONFIG_NOSYSTEM.
    """
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    env = fetch._git_env()
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_NOSYSTEM" not in env


def test_git_env_sets_fail_fast_prompt_and_ssh_batchmode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A8: never hang on an interactive prompt."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    env = fetch._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_git_env_respects_user_ssh_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A8: GIT_SSH_COMMAND is set via setdefault — a user value is preserved."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /my/key")
    env = fetch._git_env()
    assert env["GIT_SSH_COMMAND"] == "ssh -i /my/key"


def test_git_env_pops_ambient_repo_pointers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A20: an ambient GIT_DIR / GIT_WORK_TREE / ... must not survive, or a
    cwd=target git op could be hijacked onto an enclosing repo.
    """
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ):
        monkeypatch.setenv(var, "/some/ambient/value")
    env = fetch._git_env()
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ):
        assert var not in env


def test_git_env_sets_default_allow_protocol(monkeypatch) -> None:
    """_git_env pins a safe default GIT_ALLOW_PROTOCOL so ext:: remote-helper
    URLs (arbitrary command execution) are refused by git."""
    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    env = fetch._git_env()
    assert env["GIT_ALLOW_PROTOCOL"] == "file:git:ssh:http:https"
    assert "ext" not in env["GIT_ALLOW_PROTOCOL"].split(":")


def test_git_env_user_env_wins_over_default(monkeypatch) -> None:
    """A user who exported GIT_ALLOW_PROTOCOL keeps their value (setdefault)."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")
    assert fetch._git_env()["GIT_ALLOW_PROTOCOL"] == "file:ext"


def test_git_env_explicit_allow_protocol_argument(monkeypatch) -> None:
    """An explicit allow_protocol (from //#GIT_ALLOW_PROTOCOL) widens the set."""
    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    env = fetch._git_env("file:git:ssh:http:https:ext")
    assert "ext" in env["GIT_ALLOW_PROTOCOL"].split(":")


def test_git_env_user_env_wins_over_explicit_argument(monkeypatch) -> None:
    """An ambient exported GIT_ALLOW_PROTOCOL wins even over an explicit
    allow_protocol argument (setdefault): the user's env is the final say."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    assert fetch._git_env("file:git:ssh:http:https:ext")["GIT_ALLOW_PROTOCOL"] == "file"


# --- A19: _is_git_work_tree is true only at a checkout root ------------------


def test_is_git_work_tree_true_at_checkout_root() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        assert fetch._is_git_work_tree(res.path) is True


def test_is_git_work_tree_false_for_nested_plain_dir() -> None:
    """A19: a plain subdirectory nested under a work tree must return False —
    otherwise _handle_present would run git ops with cwd=subdir and git would
    walk up to the enclosing .git (host-repo hijack).
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        res = resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        nested = os.path.join(res.path, "plain-subdir")
        os.makedirs(nested)
        assert fetch._is_git_work_tree(nested) is False


# --- A5: --git-path override must be a directory, not a regular file ---------


def test_resolve_override_path_is_file_raises() -> None:
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        override_file = os.path.join(root, "afile")
        with open(override_file, "w") as fh:
            fh.write("not a dir\n")
        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref="v1"),
                externals_dir=externals,
                override_path=override_file,
            )
        assert "not a directory" in str(excinfo.value)


# --- A15: a failed ref checkout leaves no partial target on disk ------------


def test_resolve_failed_checkout_leaves_no_partial_target() -> None:
    """A15: clone succeeds but the ref fetch/checkout fails → the temp is
    removed and no partial checkout is left at the target (which a later run
    would treat as 'present' and never repair).
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        ext = GitExternal(name="mylib", url=origin["url"], ref="nonexistent-ref")
        with pytest.raises(FetchError):
            resolve_external(ext, externals_dir=externals)
        assert not os.path.exists(target)
        # No leftover temp sibling either.
        assert not any(name.startswith("mylib.ct-fetch.tmp") for name in os.listdir(externals))


# --- A22: a tag is routed as immutable even when a same-named branch exists --


def test_resolve_tag_named_like_branch_routed_immutable(capsys: pytest.CaptureFixture) -> None:
    """A22: after clone the remote branch 'shared' exists as
    refs/remotes/origin/shared AND a tag 'shared' exists. _handle_present must
    test tag first (immutable pin) and warn about the collision, rather than
    fast-forwarding it as a branch.
    """
    with tempfile.TemporaryDirectory() as root:
        # Build an origin with a branch 'shared' and a tag 'shared'.
        work = os.path.join(root, "tb-work")
        bare = os.path.join(root, "tb.git")
        os.makedirs(work)
        _git(root, "init", "-q", "-b", "master", work)
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("one\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "c1")
        _git(work, "tag", "shared")  # tag at c1
        _git(work, "checkout", "-q", "-b", "shared")
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("two\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "c2-on-branch")
        _git(root, "clone", "-q", "--bare", work, bare)
        _git(bare, "symbolic-ref", "HEAD", "refs/heads/master")
        url = "file://" + bare

        externals = os.path.join(root, "externals")
        # First clone (default branch = master).
        resolve_external(GitExternal(name="mylib", url=url, ref=None), externals_dir=externals)
        # Now request 'shared' — both a tag and a branch. --update would try to
        # fast-forward a branch, but a tag must be pinned; the collision warns.
        resolve_external(
            GitExternal(name="mylib", url=url, ref="shared"),
            externals_dir=externals,
            update=True,
        )
        err = capsys.readouterr().err
        assert "both a" in err and "tag" in err


# --- A21: detached-HEAD + --update gives a clear, actionable error -----------


def test_resolve_detached_head_update_clear_error() -> None:
    """A21: an external pinned to a tag/SHA is on a detached HEAD; a later
    ref-less --update run must explain the pin/unpin situation instead of git's
    opaque 'You are not currently on a branch.'
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        # Pin to a tag → detached HEAD.
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref="v1"), externals_dir=externals)
        # Now resolve ref-less with --update.
        with pytest.raises(FetchError) as excinfo:
            resolve_external(
                GitExternal(name="mylib", url=origin["url"], ref=None),
                externals_dir=externals,
                update=True,
            )
        msg = str(excinfo.value)
        assert "detached HEAD" in msg
        assert "mylib" in msg


# --- A9: an untracked-only work tree is not "dirty" -------------------------


def test_resolve_untracked_only_not_dirty_allows_update() -> None:
    """A9: build artifacts / IDE files (untracked) must not wedge --update; only
    tracked modifications block. A no-ref --update over an untracked-only tree
    succeeds (pull fast-forwards) and preserves the untracked file.
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)
        externals = os.path.join(root, "externals")
        target = os.path.join(externals, "mylib")
        resolve_external(GitExternal(name="mylib", url=origin["url"], ref=None), externals_dir=externals)
        # Drop an untracked file (not added to git).
        stray = os.path.join(target, "build-artifact.o")
        with open(stray, "w") as fh:
            fh.write("junk\n")
        # Advance the remote so --update has something to fast-forward to.
        new_sha = _advance_branch(origin, "master", "more\n")
        res = resolve_external(
            GitExternal(name="mylib", url=origin["url"], ref=None),
            externals_dir=externals,
            update=True,
        )
        assert res.on_disk_ref == new_sha
        # Untracked file survived the pull.
        assert os.path.isfile(stray)


# --- N2: same name + same URL but conflicting refs → hard error -------------


@requires_functional_compiler
def test_fetch_externals_conflicting_refs_raise() -> None:
    """N2: two declarations of the same external at different refs must hard
    error (symmetric with the conflicting-URL raise), naming both files. A
    build cannot silently pick one of two requested refs.
    """
    with tempfile.TemporaryDirectory() as root:
        origin = _make_bare_origin(root)  # basename 'origin', has tag v1 + master
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        other = os.path.join(root, "other.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={origin['url']}@master\nint main() {{ return 0; }}\n")
        with open(other, "w") as fh:
            fh.write(f"//#GIT={origin['url']}@v1\nvoid f() {{}}\n")

        with pytest.raises(FetchError) as excinfo:
            fetch_externals([main, other], _make_args(), BuildContext(), externals_dir=externals)
        msg = str(excinfo.value)
        assert "conflicting" in msg and "refs" in msg
        assert "master" in msg and "v1" in msg
        assert main in msg and other in msg


# --- N3: case-colliding external names → hard error -------------------------


@requires_functional_compiler
def test_fetch_externals_case_collision_raises() -> None:
    """N3: names that differ only in case would silently share one override
    (overrides key on the lowercased name) and one dir on a case-insensitive
    FS. Reject the collision up front, naming both declaring files.
    """
    with tempfile.TemporaryDirectory() as root:
        ext1 = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\n"})
        sub = os.path.join(root, "sub")
        os.makedirs(sub)
        ext2 = _make_bare_with_files(sub, "MyLib", {"bar.h": "#pragma once\n"})
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
        assert "case-colliding" in msg
        assert "mylib" in msg and "MyLib" in msg
        assert main in msg and other in msg


# --- A1: the managed clone/checkout path acquires a sidecar FileLock ---------


@requires_functional_compiler
def test_fetch_externals_locks_managed_target_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: the exists->clone/checkout path is serialized against concurrent
    peers by a FileLock on the <target>.lock SIDECAR (never the target). An
    override (user-owned checkout) is NOT locked. Assert the lock is taken on
    the sidecar for the managed external and not for the override.
    """
    import contextlib

    import compiletools.locking

    locked_paths: list[str] = []

    @contextlib.contextmanager
    def _recording_filelock(path, *_):
        locked_paths.append(path)
        yield

    monkeypatch.setattr(compiletools.locking, "FileLock", _recording_filelock)

    with tempfile.TemporaryDirectory() as root:
        managed = _make_bare_with_files(root, "managed", {"m.h": "#pragma once\n"})
        overridden = _make_bare_with_files(root, "overridden", {"o.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        local = os.path.join(root, "overridden-local")
        _git(root, "clone", "-q", overridden["url"], local)

        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={managed['url']}@master\n//#GIT={overridden['url']}@master\nint main() {{ return 0; }}\n")

        fetch_externals(
            [main],
            _make_args(),
            BuildContext(),
            externals_dir=externals,
            overrides={"overridden": local},
        )

    managed_lock = os.path.join(externals, "managed.lock")
    overridden_lock = os.path.join(externals, "overridden.lock")
    assert managed_lock in locked_paths
    assert overridden_lock not in locked_paths


@requires_functional_compiler
def test_declared_allow_protocol_is_threaded_into_git_env(monkeypatch) -> None:
    """A //#GIT_ALLOW_PROTOCOL= widening declared in a source reaches _git_env
    for that round's clone (gather -> union -> _git_run_ctx -> _run_git)."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"m.h": "#pragma once\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(
                "//#GIT_ALLOW_PROTOCOL=file:git:ssh:http:https:ext\n"
                f"//#GIT={ext['url']}@master\n"
                "int main() { return 0; }\n"
            )

        seen: list[str | None] = []
        real_git_env = fetch._git_env

        def _spy(allow_protocol=None):
            seen.append(allow_protocol)
            return real_git_env(allow_protocol)

        monkeypatch.setattr(fetch, "_git_env", _spy)
        fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)

        # At least one git op ran with the widened set (the clone). The read-only
        # helpers call _git_env() with no arg (None), so filter those out.
        widened = [s for s in seen if s is not None]
        assert widened, "no git op received an explicit allow_protocol"
        assert all("ext" in s.split(":") for s in widened)


@requires_functional_compiler
def test_allow_protocol_from_fetched_external_header_is_ignored(monkeypatch) -> None:
    """A //#GIT_ALLOW_PROTOCOL declared in a FETCHED external's header must NOT
    widen the protocol set — that would re-open ext:: RCE one hop out. Only the
    main project's own sources may widen it. extB clones fine over the default
    `file` protocol; we assert no git op ever saw the header-declared 'evil'."""
    with tempfile.TemporaryDirectory() as root:
        # extB: a plain second external, cloneable over the default `file` proto.
        extb = _make_bare_with_files(root, "extb", {"b.h": "#pragma once\n"})
        # extA's header declares a protocol widening (evil) AND pulls extB in.
        exta = _make_bare_with_files(
            root,
            "exta",
            {
                "a.h": (f"#pragma once\n//#GIT_ALLOW_PROTOCOL=file:git:ssh:http:https:evil\n//#GIT={extb['url']}\n"),
            },
        )
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={exta["url"]}\n#include "exta/a.h"\nint main() {{ return 0; }}\n')

        seen: list[str | None] = []
        real_git_env = fetch._git_env

        def _spy(allow_protocol=None):
            seen.append(allow_protocol)
            return real_git_env(allow_protocol)

        monkeypatch.setattr(fetch, "_git_env", _spy)
        results = fetch_externals([main], _make_args(), BuildContext(), externals_dir=externals)

        # Both externals were fetched (extB over the default `file` protocol).
        assert {r.name for r in results} == {"exta", "extb"}
        # The widening declared in extA's FETCHED header must NOT have reached any
        # git op's allow_protocol — it is untrusted transitive input.
        assert all("evil" not in (s or "") for s in seen), (
            f"a fetched external's //#GIT_ALLOW_PROTOCOL widened the protocol set: {seen}"
        )


# ---------------------------------------------------------------------------
# _run_git lock safety: git subprocesses must run through the lock-safe
# signal-forwarding helper (a new session + SIGINT/SIGTERM forwarding), so a
# parent interrupt during a network clone held under a <target>.lock sidecar
# cannot orphan a git child that keeps writing to the (now-unlocked) target.
# ---------------------------------------------------------------------------


def test_run_git_runs_in_new_session() -> None:
    """_run_git must spawn git via subprocess.Popen with start_new_session=True
    so signals can be forwarded to the git child's process group. A bare
    subprocess.run (the pre-fix implementation) never sets start_new_session,
    leaving a killed parent's git child orphaned under the released lock."""
    ext = GitExternal(name="mylib", url="file:///nowhere", ref=None)
    with patch("subprocess.Popen") as mock_popen:
        proc = mock_popen.return_value
        proc.returncode = 0
        proc.poll.return_value = 0
        proc.communicate.return_value = ("", None)
        proc.wait.return_value = 0
        proc.__enter__ = lambda self_: self_
        proc.__exit__ = lambda self_, *a: False
        try:
            fetch._run_git(["clone", "x", "y"], cwd=None, ext=ext)
        except Exception:
            pass
        assert mock_popen.called, "subprocess.Popen was never called"
        assert any(call.kwargs.get("start_new_session") is True for call in mock_popen.call_args_list), (
            "git subprocess was not started with start_new_session=True — signals "
            f"cannot be forwarded to the git child. Calls: {[c.kwargs for c in mock_popen.call_args_list]}"
        )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX-only signal forwarding")
def test_sigterm_during_run_git_is_forwarded_to_git_child(tmp_path) -> None:
    """End-to-end: a worker calls _run_git against a fake long-running `git`
    shim that traps SIGTERM. After SIGTERM-ing the worker, the trap-marker
    must appear (the git child received TERM via process-group forwarding) and
    the done-marker must NOT (the child did not run to completion as an orphan)."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    ready_marker = tmp_path / "READY"
    trap_marker = tmp_path / "TRAPPED"
    done_marker = tmp_path / "DONE"
    worker_script = tmp_path / "worker.py"

    git_shim = shim_dir / "git"
    git_shim.write_text(
        textwrap.dedent(f"""\
        #!/bin/sh
        touch {ready_marker}
        trap 'touch {trap_marker}; exit 143' TERM
        sleep 5
        touch {done_marker}
        """)
    )
    git_shim.chmod(0o755)

    repo_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker_script.write_text(
        textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {repo_src!r})
        os.environ["PATH"] = {str(shim_dir)!r} + os.pathsep + os.environ.get("PATH", "")
        from compiletools.fetch import _run_git, GitExternal
        ext = GitExternal(name="mylib", url="file:///nowhere", ref=None)
        try:
            _run_git(["clone", "file:///nowhere", "dest"], cwd=None, ext=ext)
        except SystemExit:
            raise
        except Exception:
            pass
    """)
    )

    proc = subprocess.Popen(
        [sys.executable, str(worker_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 15
        while not ready_marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready_marker.exists(), "git shim never started (worker did not reach _run_git)"
        time.sleep(0.5)

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            proc.wait()
            pytest.fail("Worker did not exit promptly after SIGTERM")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            proc.wait()

    # Long enough that DONE would appear if the git child ran to completion as
    # an orphan (sleep 5 in the shim), but bounded so the test stays fast.
    time.sleep(6.0)

    assert trap_marker.exists(), (
        "git child never received SIGTERM — signal was not forwarded to the git child's process group"
    )
    assert not done_marker.exists(), (
        "git child ran to completion as an orphan — worker exited without killing its git child"
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX-only signal forwarding")
def test_sigterm_during_parallel_fetch_forwards_to_all_git_children(tmp_path) -> None:
    """Two externals cloned in one parallel round: a SIGTERM to the fetch
    process must reach BOTH git children (their TRAP markers appear) and neither
    may run to completion as an orphan (no DONE markers). This is the parallel
    counterpart to the single-child _run_git SIGTERM test and pins finding 1's
    fix (worker-spawned git children are registered and force-killed)."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    externals = tmp_path / "externals"
    externals.mkdir()

    # A fake `git` that, for `clone` only, signals readiness, traps TERM
    # (leaving a per-invocation marker keyed by the clone URL), and otherwise
    # sleeps. PATH is hijacked process-wide (see worker_script below), so the
    # SAME shim also intercepts the whole process's other real git bookkeeping
    # calls (git-root detection, dirty-tree checks, ...); those must return
    # fast and successfully rather than each burning 5 real seconds, or the
    # test's fetch round would never even start within its deadline.
    git_shim = shim_dir / "git"
    git_shim.write_text(
        textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" != "clone" ]; then
            exit 0
        fi
        # Key the marker on the clone URL (an argument ending in .git), not the
        # clone destination: the destination is a
        # "<name>.ct-fetch.tmp.<pid>"-suffixed temp sibling of the final target
        # (A15 temp+rename), not a directory literally named "<name>.git".
        urlarg=""
        for a in "$@"; do
            case "$a" in
                *.git) urlarg="$a" ;;
            esac
        done
        base=$(basename "$urlarg")
        touch {tmp_path}/READY.$base
        trap 'touch {tmp_path}/TRAPPED.$base; exit 143' TERM
        sleep 5
        touch {tmp_path}/DONE.$base
        """)
    )
    git_shim.chmod(0o755)

    main = tmp_path / "main.cpp"
    main.write_text(
        "//#GIT=file:///nowhere/alpha.git@master\n//#GIT=file:///nowhere/beta.git@master\nint main() { return 0; }\n"
    )

    repo_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {repo_src!r})
        os.environ["PATH"] = {str(shim_dir)!r} + os.pathsep + os.environ.get("PATH", "")
        import configargparse
        import compiletools.apptools, compiletools.headerdeps
        from compiletools.build_context import BuildContext
        from compiletools.fetch import fetch_externals
        cap = configargparse.ArgumentParser(conflict_handler="resolve")
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)
        args = compiletools.apptools.parseargs(cap, ["--headerdeps", "direct"], context=BuildContext())
        try:
            fetch_externals([{str(main)!r}], args, BuildContext(), externals_dir={str(externals)!r})
        except SystemExit:
            raise
        except BaseException:
            pass
    """)
    )

    proc = subprocess.Popen(
        [sys.executable, str(worker_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 20
        while (
            not ((tmp_path / "READY.alpha.git").exists() and (tmp_path / "READY.beta.git").exists())
            and time.time() < deadline
        ):
            time.sleep(0.05)
        assert (tmp_path / "READY.alpha.git").exists() and (tmp_path / "READY.beta.git").exists(), (
            "both git children did not start (parallel round did not spawn two clones)"
        )
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            pytest.fail("fetch process did not exit promptly after SIGTERM")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            proc.wait()

    time.sleep(6.0)  # long enough that DONE would appear if a child orphaned
    assert (tmp_path / "TRAPPED.alpha.git").exists() and (tmp_path / "TRAPPED.beta.git").exists(), (
        "not every git child received SIGTERM — worker-spawned children were not registered/forwarded"
    )
    assert not (tmp_path / "DONE.alpha.git").exists() and not (tmp_path / "DONE.beta.git").exists(), (
        "a git child ran to completion as an orphan after the fetch process was signalled"
    )
