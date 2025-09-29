"""Tests for stringzilla_utils module."""

import stringzilla
from compiletools.stringzilla_utils import (
    strip_sz,
    ends_with_backslash_sz,
    is_alpha_or_underscore_sz,
    join_lines_strip_backslash_sz,
    join_sz
)


class TestStripSz:
    """Test strip_sz function."""

    def test_strip_empty_string(self):
        """Test stripping empty string."""
        sz_str = stringzilla.Str("")
        result = strip_sz(sz_str)
        assert str(result) == ""

    def test_strip_whitespace_only(self):
        """Test stripping string with only whitespace."""
        sz_str = stringzilla.Str("   \t\r\n   ")
        result = strip_sz(sz_str)
        assert str(result) == ""

    def test_strip_no_whitespace(self):
        """Test stripping string with no whitespace."""
        sz_str = stringzilla.Str("hello")
        result = strip_sz(sz_str)
        assert str(result) == "hello"

    def test_strip_leading_whitespace(self):
        """Test stripping leading whitespace."""
        sz_str = stringzilla.Str("  \t hello")
        result = strip_sz(sz_str)
        assert str(result) == "hello"

    def test_strip_trailing_whitespace(self):
        """Test stripping trailing whitespace."""
        sz_str = stringzilla.Str("hello  \r\n")
        result = strip_sz(sz_str)
        assert str(result) == "hello"

    def test_strip_both_whitespace(self):
        """Test stripping both leading and trailing whitespace."""
        sz_str = stringzilla.Str("  \t hello world  \r\n")
        result = strip_sz(sz_str)
        assert str(result) == "hello world"

    def test_strip_custom_chars(self):
        """Test stripping custom characters."""
        sz_str = stringzilla.Str("xyzhelllo worldxyz")
        result = strip_sz(sz_str, "xyz")
        assert str(result) == "helllo world"

    def test_strip_preserves_internal_whitespace(self):
        """Test that internal whitespace is preserved."""
        sz_str = stringzilla.Str("  hello   world  ")
        result = strip_sz(sz_str)
        assert str(result) == "hello   world"


class TestEndsWithBackslashSz:
    """Test ends_with_backslash_sz function."""

    def test_empty_string(self):
        """Test empty string."""
        sz_str = stringzilla.Str("")
        assert not ends_with_backslash_sz(sz_str)

    def test_whitespace_only(self):
        """Test string with only whitespace."""
        sz_str = stringzilla.Str("   \t\r\n   ")
        assert not ends_with_backslash_sz(sz_str)

    def test_ends_with_backslash(self):
        """Test string ending with backslash."""
        sz_str = stringzilla.Str("hello\\")
        assert ends_with_backslash_sz(sz_str)

    def test_ends_with_backslash_and_whitespace(self):
        """Test string ending with backslash followed by whitespace."""
        sz_str = stringzilla.Str("hello\\  \t\r\n")
        assert ends_with_backslash_sz(sz_str)

    def test_no_backslash(self):
        """Test string not ending with backslash."""
        sz_str = stringzilla.Str("hello world")
        assert not ends_with_backslash_sz(sz_str)

    def test_backslash_not_at_end(self):
        """Test string with backslash not at end."""
        sz_str = stringzilla.Str("hello\\world")
        assert not ends_with_backslash_sz(sz_str)

    def test_multiple_backslashes(self):
        """Test string ending with multiple backslashes."""
        sz_str = stringzilla.Str("hello\\\\")
        assert ends_with_backslash_sz(sz_str)

    def test_escaped_backslash(self):
        """Test string ending with escaped backslash."""
        sz_str = stringzilla.Str("hello\\\\\\")
        assert ends_with_backslash_sz(sz_str)


class TestIsAlphaOrUnderscoreSz:
    """Test is_alpha_or_underscore_sz function."""

    def test_empty_string(self):
        """Test empty string."""
        sz_str = stringzilla.Str("")
        assert not is_alpha_or_underscore_sz(sz_str, 0)

    def test_position_out_of_bounds(self):
        """Test position beyond string length."""
        sz_str = stringzilla.Str("abc")
        assert not is_alpha_or_underscore_sz(sz_str, 5)

    def test_lowercase_letter(self):
        """Test lowercase letter."""
        sz_str = stringzilla.Str("abc")
        assert is_alpha_or_underscore_sz(sz_str, 0)
        assert is_alpha_or_underscore_sz(sz_str, 1)
        assert is_alpha_or_underscore_sz(sz_str, 2)

    def test_uppercase_letter(self):
        """Test uppercase letter."""
        sz_str = stringzilla.Str("ABC")
        assert is_alpha_or_underscore_sz(sz_str, 0)
        assert is_alpha_or_underscore_sz(sz_str, 1)
        assert is_alpha_or_underscore_sz(sz_str, 2)

    def test_underscore(self):
        """Test underscore character."""
        sz_str = stringzilla.Str("_abc")
        assert is_alpha_or_underscore_sz(sz_str, 0)

    def test_digit(self):
        """Test digit character."""
        sz_str = stringzilla.Str("123")
        assert not is_alpha_or_underscore_sz(sz_str, 0)

    def test_special_characters(self):
        """Test special characters."""
        sz_str = stringzilla.Str("@#$")
        assert not is_alpha_or_underscore_sz(sz_str, 0)
        assert not is_alpha_or_underscore_sz(sz_str, 1)
        assert not is_alpha_or_underscore_sz(sz_str, 2)

    def test_mixed_string(self):
        """Test mixed string with various characters."""
        sz_str = stringzilla.Str("a1_B@")
        assert is_alpha_or_underscore_sz(sz_str, 0)  # 'a'
        assert not is_alpha_or_underscore_sz(sz_str, 1)  # '1'
        assert is_alpha_or_underscore_sz(sz_str, 2)  # '_'
        assert is_alpha_or_underscore_sz(sz_str, 3)  # 'B'
        assert not is_alpha_or_underscore_sz(sz_str, 4)  # '@'


class TestJoinLinesStripBackslashSz:
    """Test join_lines_strip_backslash_sz function."""

    def test_empty_list(self):
        """Test empty list of lines."""
        result = join_lines_strip_backslash_sz([])
        assert str(result) == ""

    def test_single_line_no_backslash(self):
        """Test single line without backslash."""
        lines = [stringzilla.Str("hello world")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world"

    def test_single_line_with_backslash(self):
        """Test single line with backslash."""
        lines = [stringzilla.Str("hello world\\")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world"

    def test_multiple_lines_no_backslash(self):
        """Test multiple lines without backslashes."""
        lines = [stringzilla.Str("hello"), stringzilla.Str("world"), stringzilla.Str("test")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world test"

    def test_multiple_lines_with_backslashes(self):
        """Test multiple lines with backslashes."""
        lines = [stringzilla.Str("hello\\"), stringzilla.Str("world\\"), stringzilla.Str("test")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world test"

    def test_mixed_lines(self):
        """Test mix of lines with and without backslashes."""
        lines = [stringzilla.Str("hello\\"), stringzilla.Str("world"), stringzilla.Str("test\\")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world test"

    def test_backslash_with_whitespace(self):
        """Test backslash with trailing whitespace."""
        lines = [stringzilla.Str("hello\\  \t"), stringzilla.Str("world\\  \r\n"), stringzilla.Str("test")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world test"

    def test_whitespace_only_lines(self):
        """Test lines with only whitespace."""
        lines = [stringzilla.Str("  "), stringzilla.Str("\t"), stringzilla.Str("hello")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "  hello"

    def test_stringzilla_str_input(self):
        """Test with StringZilla.Str objects as input."""
        lines = [stringzilla.Str("hello\\"), stringzilla.Str("world")]
        result = join_lines_strip_backslash_sz(lines)
        assert str(result) == "hello world"


class TestJoinSz:
    """Test join_sz function."""

    def test_empty_list(self):
        """Test joining empty list."""
        result = join_sz("\n", [])
        assert result == ""

    def test_single_item(self):
        """Test joining single item."""
        items = [stringzilla.Str("hello")]
        result = join_sz("\n", items)
        assert result == "hello"

    def test_multiple_strings(self):
        """Test joining multiple string items."""
        items = ["hello", "world", "test"]
        result = join_sz("\n", items)
        assert result == "hello\nworld\ntest"

    def test_multiple_stringzilla_strs(self):
        """Test joining multiple StringZilla.Str items."""
        items = [stringzilla.Str("hello"), stringzilla.Str("world"), stringzilla.Str("test")]
        result = join_sz("\n", items)
        assert result == "hello\nworld\ntest"

    def test_mixed_types(self):
        """Test joining mixed string and StringZilla.Str items."""
        items = ["hello", stringzilla.Str("world"), "test"]
        result = join_sz("\n", items)
        assert result == "hello\nworld\ntest"

    def test_different_separators(self):
        """Test different separator strings."""
        items = [stringzilla.Str("a"), stringzilla.Str("b"), stringzilla.Str("c")]

        # Space separator
        result = join_sz(" ", items)
        assert result == "a b c"

        # Comma separator
        result = join_sz(", ", items)
        assert result == "a, b, c"

        # Empty separator
        result = join_sz("", items)
        assert result == "abc"

    def test_compatibility_with_str_join(self):
        """Test that join_sz produces same results as str.join() for string inputs."""
        items = ["hello", "world", "test"]
        separator = "\n"

        # Standard str.join()
        expected = separator.join(items)

        # Our join_sz function
        result = join_sz(separator, items)

        assert result == expected
