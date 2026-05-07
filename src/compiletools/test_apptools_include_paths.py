"""Tests for apptools include-path dedup helpers.

Covers _existing_include_paths and _add_include_paths_to_flags. These
support the include-path token-based dedup fix:

- _existing_include_paths walks tokens and recognizes -I as either
  attached (-I/path) or detached (-I /path). Other tokens (e.g.,
  -DFOO=/path, -isystem /path, -L/path) are NOT treated as -I paths.
- _add_include_paths_to_flags appends -I entries from args.INCLUDE
  into CPPFLAGS/CFLAGS/CXXFLAGS without re-adding paths that are
  already present as -I entries; the dedup check inspects tokens,
  not raw substrings.
"""

from types import SimpleNamespace

from compiletools.apptools import _add_include_paths_to_flags, _existing_include_paths


def test_existing_include_paths_attached_form():
    assert _existing_include_paths("-I/usr/include -O2") == {"/usr/include"}


def test_existing_include_paths_detached_form():
    assert _existing_include_paths("-I /usr/include -O2") == {"/usr/include"}


def test_existing_include_paths_mixed_forms():
    assert _existing_include_paths("-I/a -I /b -I/c") == {"/a", "/b", "/c"}


def test_existing_include_paths_ignores_d_with_path_value():
    assert _existing_include_paths("-DFOO=/usr/include -Wall") == set()


def test_existing_include_paths_ignores_other_dash_i_like_tokens():
    # -isystem looks like it starts with "-i" (lowercase), not "-I"
    # (uppercase). The helper is for -I only.
    assert _existing_include_paths("-isystem /opt/inc") == set()


def test_existing_include_paths_dangling_dash_I():
    # "-I" with no following token must not crash.
    assert _existing_include_paths("-O2 -I") == set()


def test_existing_include_paths_empty():
    assert _existing_include_paths("") == set()


def test_add_include_paths_skips_when_already_attached():
    args = SimpleNamespace(
        INCLUDE="/usr/include",
        CPPFLAGS="-I/usr/include",
        CFLAGS="-I/usr/include",
        CXXFLAGS="-I/usr/include",
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    assert args.CPPFLAGS == "-I/usr/include"
    assert args.CFLAGS == "-I/usr/include"
    assert args.CXXFLAGS == "-I/usr/include"


def test_add_include_paths_skips_when_already_detached():
    args = SimpleNamespace(
        INCLUDE="/usr/include",
        CPPFLAGS="-I /usr/include",
        CFLAGS="-I /usr/include",
        CXXFLAGS="-I /usr/include",
        verbose=0,
    )
    _add_include_paths_to_flags(args)
    assert args.CPPFLAGS == "-I /usr/include"
    assert args.CFLAGS == "-I /usr/include"
    assert args.CXXFLAGS == "-I /usr/include"


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
