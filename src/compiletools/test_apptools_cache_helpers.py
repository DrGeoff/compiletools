"""Tests for apptools cache-key scoping helpers.

Covers cmdline_d_macro_names and tokenize_compile_flags, which support
the per-TU cache-key pollution fix:

- cmdline_d_macro_names returns the set of macro names defined via
  cmdline -D flags (excluding compiler builtins). This is the universe
  of macros eligible for per-TU cache-key filtering.
- tokenize_compile_flags strips -D/-U entries from compile-flag strings
  so the build-context hash does not double-count cmdline -D macros
  (which are hashed separately via the per-TU scoping mechanism).
"""

from types import SimpleNamespace

import configargparse
import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.configutils
import compiletools.testhelper as uth
import compiletools.utils as utils
from compiletools.apptools import cmdline_d_macro_names, strip_d_u_tokens, tokenize_compile_flags
from compiletools.build_context import BuildContext


def _make_args(cppflags="", cflags="", cxxflags=""):
    args = SimpleNamespace(
        CPPFLAGS=cppflags,
        CFLAGS=cflags,
        CXXFLAGS=cxxflags,
        CXX=None,
        verbose=0,
    )
    uth.finalize_flag_state(args)
    return args


class TestCmdlineDMacroNames:
    def test_cmdline_d_macro_names_extracts_attached_form(self):
        args = _make_args(cppflags="-DFOO=1 -DBAR=2")
        result = cmdline_d_macro_names(args)
        assert result == frozenset({sz.Str("FOO"), sz.Str("BAR")})

    def test_cmdline_d_macro_names_extracts_detached_form(self):
        args = _make_args(cxxflags="-D BAZ=3")
        result = cmdline_d_macro_names(args)
        assert result == frozenset({sz.Str("BAZ")})

    def test_cmdline_d_macro_names_excludes_compiler_builtins(self):
        args = _make_args(cppflags="-DFOO=1")
        result = cmdline_d_macro_names(args)
        assert result == frozenset({sz.Str("FOO")})
        assert sz.Str("__GNUC__") not in result

    def test_cmdline_d_macro_names_returns_frozenset_of_sz_Str(self):
        args = _make_args(cppflags="-DFOO=1 -DBAR")
        result = cmdline_d_macro_names(args)
        assert isinstance(result, frozenset)
        for elem in result:
            assert isinstance(elem, sz.Str)

    def test_cmdline_d_macro_names_empty_when_no_d_flags(self):
        args = _make_args(cppflags="-O2 -Iinclude")
        result = cmdline_d_macro_names(args)
        assert result == frozenset()

    def test_cmdline_d_macro_names_aggregates_across_sources(self):
        args = _make_args(cppflags="-DA=1", cxxflags="-DB=2")
        result = cmdline_d_macro_names(args)
        assert result == frozenset({sz.Str("A"), sz.Str("B")})

    def test_cmdline_d_macro_names_strips_value_from_attached_form(self):
        """The set contains the macro NAME only -- never the `=value` half."""
        args = _make_args(cppflags="-DFOO=bar -DBAZ=qux")
        result = cmdline_d_macro_names(args)
        assert result == frozenset({sz.Str("FOO"), sz.Str("BAZ")})
        # Specifically guard against accidentally including the value half:
        assert sz.Str("FOO=bar") not in result
        assert sz.Str("bar") not in result


class TestTokenizeCompileFlags:
    @pytest.mark.parametrize(
        ("cppflags", "expected"),
        [
            pytest.param("-O2 -DFOO -Wall", ["-O2", "-Wall"], id="attached-d"),
            pytest.param("-O2 -DFOO=bar -Wall", ["-O2", "-Wall"], id="attached-d-value"),
            pytest.param("-UFOO -Wall", ["-Wall"], id="attached-u"),
            pytest.param("-O2 -D FOO -Wall", ["-O2", "-Wall"], id="detached-d"),
            pytest.param("-O2 -D FOO=bar -Wall", ["-O2", "-Wall"], id="detached-d-value"),
            pytest.param("-O2 -U FOO -Wall", ["-O2", "-Wall"], id="detached-u"),
            pytest.param("-O2 -D", ["-O2"], id="dangling-d"),
            pytest.param(
                "-O2 -Iinclude -std=c++20 -Wall -fPIC",
                ["-O2", "-Iinclude", "-std=c++20", "-Wall", "-fPIC"],
                id="keep-other-flags",
            ),
            pytest.param("-I/usr/include -D FOO", ["-I/usr/include"], id="keep-capital-i"),
        ],
    )
    def test_tokenize_cpp_flags(self, cppflags, expected):
        cpp = tokenize_compile_flags(cppflags, "", "")[0]
        assert cpp == expected

    def test_tokenize_returns_three_lists(self):
        result = tokenize_compile_flags("-O2", "-g", "-Wall")
        assert isinstance(result, tuple)
        assert len(result) == 3
        cpp, c, cxx = result
        assert cpp == ["-O2"]
        assert c == ["-g"]
        assert cxx == ["-Wall"]

    def test_tokenize_accepts_list_input(self):
        cpp = tokenize_compile_flags(["-O2", "-DFOO", "-Wall"], "", "")[0]
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_handles_empty_strings(self):
        cpp, c, cxx = tokenize_compile_flags("", "", "")
        assert cpp == []
        assert c == []
        assert cxx == []

    def test_tokenize_strips_d_in_all_three_slots(self):
        """Stripping logic applies symmetrically to cpp, c, and cxx flags."""
        cpp, c, cxx = tokenize_compile_flags(
            "-O0 -DAAA -Wall",
            "-O1 -DBBB=val -Wextra",
            "-O2 -D CCC -Wpedantic",
        )
        assert cpp == ["-O0", "-Wall"]
        assert c == ["-O1", "-Wextra"]
        assert cxx == ["-O2", "-Wpedantic"]

    def test_tokenize_strip_unhashed_drops_warnings(self):
        """``strip_unhashed=True`` drops both ``-D``/``-U`` and
        diagnostic-only tokens (``-Wall`` etc.) from each slot."""
        _, _, cxx = tokenize_compile_flags("", "", "-O2 -Wall -DFOO", strip_unhashed=True)
        assert cxx == ["-O2"]

    def test_tokenize_strip_unhashed_default_false(self):
        """Default behavior (``strip_unhashed=False``) preserves
        ``-W`` warnings; only ``-D``/``-U`` are stripped."""
        _, _, cxx = tokenize_compile_flags("", "", "-O2 -Wall -DFOO")
        assert cxx == ["-O2", "-Wall"]


class TestStripDUTokens:
    """Test the standalone strip_d_u_tokens helper.

    This helper is the strip-only half of tokenize_compile_flags;
    it is invoked separately by call sites that already have a
    pre-tokenized flag list (e.g. magicflags._parse, _pch_command_hash)
    and just need the -D/-U entries removed.
    """

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            pytest.param(["-O2", "-DFOO", "-Wall"], ["-O2", "-Wall"], id="attached-d"),
            pytest.param(["-O2", "-DFOO=bar", "-Wall"], ["-O2", "-Wall"], id="attached-d-value"),
            pytest.param(["-O2", "-D", "FOO", "-Wall"], ["-O2", "-Wall"], id="detached-d"),
            pytest.param(["-O2", "-U", "FOO", "-Wall"], ["-O2", "-Wall"], id="detached-u"),
            pytest.param(["-O2", "-D"], ["-O2"], id="dangling-d"),
            pytest.param(["-O2", "-Iinclude", "-Wall"], ["-O2", "-Iinclude", "-Wall"], id="keep-other-flags"),
            pytest.param([], [], id="empty"),
            pytest.param(["-I/usr/include", "-DFOO"], ["-I/usr/include"], id="keep-capital-i"),
        ],
    )
    def test_strip_d_u_tokens(self, tokens, expected):
        assert strip_d_u_tokens(tokens) == expected


class TestStateTokensAfterParseargs:
    """The stashed BuildState's token tuples must reflect the final,
    post-compute flag state (env var append, INCLUDE injection,
    pkg-config), and the string forms must round-trip the tokens.
    """

    @pytest.fixture(autouse=True)
    def _reset_parsers(self):
        uth.delete_existing_parsers()
        yield
        uth.delete_existing_parsers()

    def _parse(self, extra_args=None, tempdir=None):
        """Run parseargs end-to-end against the standard test parser.

        Mirrors create_magic_parser but skips the magicflags surface,
        which we don't need for these tests.
        """

        extra_args = extra_args or []
        temp_config_name = uth.create_temp_config(tempdir)
        argv = ["--config=" + temp_config_name] + extra_args
        config_files = compiletools.configutils.config_files_from_variant(argv=argv, exedir=uth.cakedir())

        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="TestArgsTokensAfterParseargs",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.apptools.add_common_arguments(cap)
        compiletools.apptools.add_link_arguments(cap)
        return compiletools.apptools.parseargs(cap, argv, context=BuildContext())

    def test_state_tokens_round_trip_the_strings(self, tmp_path):
        from compiletools.build_apply import get_build_state

        args = self._parse(tempdir=str(tmp_path))
        state = get_build_state(args)

        # The string forms are shlex.join of the flag token tuples:
        # splitting them back must reproduce the tokens exactly.
        assert list(state.flags.cpp) == utils.split_command_cached(state.cppflags)
        assert list(state.flags.c) == utils.split_command_cached(state.cflags)
        assert list(state.flags.cxx) == utils.split_command_cached(state.cxxflags)
        assert list(state.flags.ld) == utils.split_command_cached(state.ldflags)

    def test_state_tokens_reflect_appended_cppflags(self, tmp_path):
        """append-CPPFLAGS contributions must appear in the state tokens."""
        from compiletools.build_apply import get_build_state

        args = self._parse(["--append-CPPFLAGS=-DAFTER_TOKENIZE=42"], tempdir=str(tmp_path))

        assert "-DAFTER_TOKENIZE=42" in get_build_state(args).flags.cpp, (
            "Appended -D entries must be present in the state's cpp tokens; "
            "the state is computed AFTER all gather-time contributions."
        )
