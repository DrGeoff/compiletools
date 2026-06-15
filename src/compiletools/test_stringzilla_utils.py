"""Tests for stringzilla_utils module."""

import pytest
import stringzilla

from compiletools.stringzilla_utils import (
    ends_with_backslash_sz,
    is_alpha_or_underscore_sz,
    join_lines_strip_backslash_sz,
    join_sz,
    strip_sz,
)


class TestStripSz:
    """Test strip_sz function."""

    @pytest.mark.parametrize(
        ("source", "expected", "chars"),
        [
            pytest.param("", "", None, id="empty"),
            pytest.param("   \t\r\n   ", "", None, id="whitespace-only"),
            pytest.param("hello", "hello", None, id="no-whitespace"),
            pytest.param("  \t hello", "hello", None, id="leading-whitespace"),
            pytest.param("hello  \r\n", "hello", None, id="trailing-whitespace"),
            pytest.param("  \t hello world  \r\n", "hello world", None, id="both-sides-whitespace"),
            pytest.param("xyzhelllo worldxyz", "helllo world", "xyz", id="custom-chars"),
            pytest.param("  hello   world  ", "hello   world", None, id="internal-whitespace"),
        ],
    )
    def test_strip(self, source, expected, chars):
        sz_str = stringzilla.Str(source)
        result = strip_sz(sz_str) if chars is None else strip_sz(sz_str, chars)
        assert str(result) == expected


class TestEndsWithBackslashSz:
    """Test ends_with_backslash_sz function."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param("", False, id="empty"),
            pytest.param("   \t\r\n   ", False, id="whitespace-only"),
            pytest.param("hello\\", True, id="ends-with-backslash"),
            pytest.param("hello\\  \t\r\n", True, id="backslash-before-whitespace"),
            pytest.param("hello world", False, id="no-backslash"),
            pytest.param("hello\\world", False, id="backslash-not-at-end"),
            pytest.param("hello\\\\", True, id="multiple-backslashes"),
            pytest.param("hello\\\\\\", True, id="escaped-backslash"),
            # The last non-whitespace byte falls inside a multi-byte UTF-8
            # sequence (emoji 🎯 ends in 0xaf). Indexing that single byte must
            # not raise a UnicodeDecodeError; it is simply not a backslash.
            pytest.param("// trailing emoji \U0001f3af", False, id="multibyte-tail-emoji"),
            pytest.param("see the em—dash", False, id="multibyte-tail-emdash"),
        ],
    )
    def test_ends_with_backslash(self, source, expected):
        assert ends_with_backslash_sz(stringzilla.Str(source)) is expected


class TestIsAlphaOrUnderscoreSz:
    """Test is_alpha_or_underscore_sz function."""

    @pytest.mark.parametrize(
        ("source", "pos", "expected"),
        [
            pytest.param("", 0, False, id="empty"),
            pytest.param("abc", 5, False, id="out-of-bounds"),
            pytest.param("abc", 0, True, id="lowercase-a"),
            pytest.param("abc", 1, True, id="lowercase-b"),
            pytest.param("abc", 2, True, id="lowercase-c"),
            pytest.param("ABC", 0, True, id="uppercase-a"),
            pytest.param("ABC", 1, True, id="uppercase-b"),
            pytest.param("ABC", 2, True, id="uppercase-c"),
            pytest.param("_abc", 0, True, id="underscore"),
            pytest.param("123", 0, False, id="digit"),
            pytest.param("@#$", 0, False, id="special-at"),
            pytest.param("@#$", 1, False, id="special-hash"),
            pytest.param("@#$", 2, False, id="special-dollar"),
            pytest.param("a1_B@", 0, True, id="mixed-a"),
            pytest.param("a1_B@", 1, False, id="mixed-digit"),
            pytest.param("a1_B@", 2, True, id="mixed-underscore"),
            pytest.param("a1_B@", 3, True, id="mixed-b"),
            pytest.param("a1_B@", 4, False, id="mixed-at"),
        ],
    )
    def test_is_alpha_or_underscore(self, source, pos, expected):
        assert is_alpha_or_underscore_sz(stringzilla.Str(source), pos) is expected


class TestJoinLinesStripBackslashSz:
    """Test join_lines_strip_backslash_sz function."""

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            pytest.param([], "", id="empty"),
            pytest.param(["hello world"], "hello world", id="single-line"),
            pytest.param(["hello world\\"], "hello world", id="single-line-backslash"),
            pytest.param(["hello", "world", "test"], "hello world test", id="multiple-lines"),
            pytest.param(["hello\\", "world\\", "test"], "hello world test", id="multiple-backslashes"),
            pytest.param(["hello\\", "world", "test\\"], "hello world test", id="mixed-backslashes"),
            pytest.param(["hello\\  \t", "world\\  \r\n", "test"], "hello world test", id="backslash-whitespace"),
            pytest.param(["  ", "\t", "hello"], "  hello", id="whitespace-only-lines"),
            pytest.param(["hello\\", "world"], "hello world", id="stringzilla-input"),
            # A joined physical line whose trimmed content ends in a multi-byte
            # UTF-8 character (emoji 🎯 ends in byte 0xaf). The trailing-backslash
            # probe must not index that single tail byte (UnicodeDecodeError);
            # such a line simply has no backslash to strip.
            pytest.param(["start \\", "end \U0001f3af"], "start end \U0001f3af", id="multibyte-tail-emoji"),
            pytest.param(["trailing dash —"], "trailing dash —", id="multibyte-tail-emdash"),
        ],
    )
    def test_join_lines_strip_backslash(self, lines, expected):
        result = join_lines_strip_backslash_sz([stringzilla.Str(line) for line in lines])
        assert str(result) == expected


class TestJoinSz:
    """Test join_sz function."""

    @pytest.mark.parametrize(
        ("separator", "items", "expected"),
        [
            pytest.param("\n", [], "", id="empty"),
            pytest.param("\n", [stringzilla.Str("hello")], "hello", id="single"),
            pytest.param("\n", ["hello", "world", "test"], "hello\nworld\ntest", id="strings"),
            pytest.param(
                "\n",
                [stringzilla.Str("hello"), stringzilla.Str("world"), stringzilla.Str("test")],
                "hello\nworld\ntest",
                id="stringzilla-strs",
            ),
            pytest.param("\n", ["hello", stringzilla.Str("world"), "test"], "hello\nworld\ntest", id="mixed"),
            pytest.param(" ", [stringzilla.Str("a"), stringzilla.Str("b"), stringzilla.Str("c")], "a b c", id="space"),
            pytest.param(
                ", ",
                [stringzilla.Str("a"), stringzilla.Str("b"), stringzilla.Str("c")],
                "a, b, c",
                id="comma",
            ),
            pytest.param("", [stringzilla.Str("a"), stringzilla.Str("b"), stringzilla.Str("c")], "abc", id="empty-sep"),
        ],
    )
    def test_join(self, separator, items, expected):
        assert join_sz(separator, items) == expected

    def test_compatibility_with_str_join(self):
        """Test that join_sz produces same results as str.join() for string inputs."""
        items = ["hello", "world", "test"]
        separator = "\n"

        # Standard str.join()
        expected = separator.join(items)

        # Our join_sz function
        result = join_sz(separator, items)

        assert result == expected
