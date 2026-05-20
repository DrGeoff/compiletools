"""Tests for apptools include-path dedup helpers.

Covers Flags.existing_include_paths (the dedup oracle now used by
_add_include_paths_to_flags) and _add_include_paths_to_flags itself:

- Flags.existing_include_paths walks tokens and recognizes -I as either
  attached (-I/path) or detached (-I /path). Other tokens (e.g.,
  -DFOO=/path, -isystem /path, -L/path) are NOT treated as -I paths.
- _add_include_paths_to_flags appends -I entries from args.INCLUDE
  into CPPFLAGS/CFLAGS/CXXFLAGS without re-adding paths that are
  already present as -I entries; the dedup check inspects tokens,
  not raw substrings.
"""

from types import SimpleNamespace

import pytest

from compiletools.apptools import _add_include_paths_to_flags
from compiletools.flags import Flags
from compiletools.utils import split_command_cached


def _existing_include_paths(flags_str: str) -> set[str]:
    """Test shim: builds a Flags from the raw string and returns its
    -I dedup set. Mirrors the legacy ``apptools._existing_include_paths``
    helper that was removed when its production caller migrated to
    ``Flags.existing_include_paths``.
    """
    return Flags(cpp=tuple(split_command_cached(flags_str))).existing_include_paths("cpp")


@pytest.mark.parametrize(
    ("flags_str", "expected"),
    [
        pytest.param("-I/usr/include -O2", {"/usr/include"}, id="attached"),
        pytest.param("-I /usr/include -O2", {"/usr/include"}, id="detached"),
        pytest.param("-I/a -I /b -I/c", {"/a", "/b", "/c"}, id="mixed"),
        pytest.param("-DFOO=/usr/include -Wall", set(), id="ignore-d-value"),
        pytest.param("-isystem /opt/inc", set(), id="ignore-isystem"),
        pytest.param("-O2 -I", set(), id="dangling-dash-i"),
        pytest.param("", set(), id="empty"),
    ],
)
def test_existing_include_paths(flags_str, expected):
    assert _existing_include_paths(flags_str) == expected


@pytest.mark.parametrize(
    "existing_flags",
    [
        pytest.param("-I/usr/include", id="attached"),
        pytest.param("-I /usr/include", id="detached"),
    ],
)
def test_add_include_paths_skips_when_already_present(existing_flags):
    args = SimpleNamespace(
        INCLUDE="/usr/include",
        CPPFLAGS=existing_flags,
        CFLAGS=existing_flags,
        CXXFLAGS=existing_flags,
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    assert (existing_flags,) * 3 == (args.CPPFLAGS, args.CFLAGS, args.CXXFLAGS)


def test_add_include_paths_does_not_skip_when_only_in_other_path_flag():
    """Bug fix: a path that appears as another flag's value
    (-isystem, -L) is NOT the same as having an -I /path entry.
    The dedup check must not be fooled by the substring."""
    args = SimpleNamespace(
        INCLUDE="/usr/include",
        CPPFLAGS="-isystem /usr/include",  # /usr/include is here as an -isystem path
        CFLAGS="-L /usr/include",  # and as an -L path
        CXXFLAGS="-O2",  # and absent here
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    assert "-I /usr/include" in args.CPPFLAGS
    assert "-I /usr/include" in args.CFLAGS
    assert "-I /usr/include" in args.CXXFLAGS


def test_add_include_paths_appends_new_path():
    args = SimpleNamespace(
        INCLUDE="/new/include",
        CPPFLAGS="-O2",
        CFLAGS="-O2",
        CXXFLAGS="-O2",
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    assert "-I /new/include" in args.CPPFLAGS
    assert "-I /new/include" in args.CFLAGS
    assert "-I /new/include" in args.CXXFLAGS


def test_add_include_paths_handles_multiple_includes():
    args = SimpleNamespace(
        INCLUDE="/a /b /c",
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    for slot in (args.CPPFLAGS, args.CFLAGS, args.CXXFLAGS):
        assert "-I /a" in slot
        assert "-I /b" in slot
        assert "-I /c" in slot
