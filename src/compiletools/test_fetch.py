"""Tests for fetch.py — pure parsing layer for //#GIT= declarations."""

from __future__ import annotations

import dataclasses

import pytest

from compiletools.fetch import GitExternal, derive_name, parse_git_declaration, parse_git_value

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
