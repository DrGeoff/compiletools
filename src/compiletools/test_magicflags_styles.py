"""Tests for magicflags output styles and CLI-adjacent code."""

from collections import defaultdict
from unittest.mock import Mock, patch

import stringzilla as sz

import compiletools.magicflags as magicflags


def _make_args(strip_git_root=False, verbose=0):
    args = Mock()
    args.strip_git_root = strip_git_root
    args.verbose = verbose
    return args


class TestNullStyle:
    def test_output(self, capsys):
        style = magicflags.NullStyle(_make_args())
        flags = defaultdict(list, {sz.Str("CPPFLAGS"): [sz.Str("-I/usr")]})
        style("/path/file.cpp", flags)
        out = capsys.readouterr().out
        assert "/path/file.cpp" in out


class TestPrettyStyle:
    def test_with_flags(self, capsys):
        style = magicflags.PrettyStyle(_make_args())
        flags = defaultdict(list, {sz.Str("CPPFLAGS"): [sz.Str("-I/usr")]})
        style("/path/file.cpp", flags)
        out = capsys.readouterr().out
        assert "/path/file.cpp" in out
        assert "CPPFLAGS" in out
        assert "-I/usr" in out

    def test_with_none_flags(self, capsys):
        """PrettyStyle handles None (non-iterable) magicflags gracefully."""
        style = magicflags.PrettyStyle(_make_args())
        style("/path/file.cpp", None)
        out = capsys.readouterr().out
        assert "/path/file.cpp" in out
        assert "None" in out


class TestMagicFlagsFactory:
    def test_create_verbose_message(self, capsys):
        """Factory prints message at verbose >= 4."""
        args = Mock()
        args.magic = "direct"
        args.verbose = 4
        # Patch the class constructor to avoid deep initialization
        with patch.object(magicflags.DirectMagicFlags, "__init__", return_value=None):
            magicflags.create(args, Mock(), Mock())
        out = capsys.readouterr().out
        assert "Creating DirectMagicFlags" in out
