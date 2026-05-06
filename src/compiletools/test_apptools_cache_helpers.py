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

import stringzilla as sz

from compiletools.apptools import cmdline_d_macro_names, tokenize_compile_flags


def _make_args(cppflags="", cflags="", cxxflags=""):
    return SimpleNamespace(
        CPPFLAGS=cppflags,
        CFLAGS=cflags,
        CXXFLAGS=cxxflags,
        CXX=None,
        verbose=0,
    )


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


class TestTokenizeCompileFlags:
    def test_tokenize_strips_attached_d(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -DFOO -Wall", "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_strips_attached_d_with_value(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -DFOO=bar -Wall", "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_strips_attached_u(self):
        cpp, _c, _cxx = tokenize_compile_flags("-UFOO -Wall", "", "")
        assert cpp == ["-Wall"]

    def test_tokenize_strips_detached_d(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -D FOO -Wall", "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_strips_detached_d_with_value(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -D FOO=bar -Wall", "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_strips_detached_u(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -U FOO -Wall", "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_dangling_detached_d_at_end(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -D", "", "")
        assert cpp == ["-O2"]

    def test_tokenize_keeps_other_flags(self):
        cpp, _c, _cxx = tokenize_compile_flags("-O2 -Iinclude -std=c++20 -Wall -fPIC", "", "")
        assert cpp == ["-O2", "-Iinclude", "-std=c++20", "-Wall", "-fPIC"]

    def test_tokenize_returns_three_lists(self):
        result = tokenize_compile_flags("-O2", "-g", "-Wall")
        assert isinstance(result, tuple)
        assert len(result) == 3
        cpp, c, cxx = result
        assert cpp == ["-O2"]
        assert c == ["-g"]
        assert cxx == ["-Wall"]

    def test_tokenize_accepts_list_input(self):
        cpp, _c, _cxx = tokenize_compile_flags(["-O2", "-DFOO", "-Wall"], "", "")
        assert cpp == ["-O2", "-Wall"]

    def test_tokenize_does_not_strip_i_capital(self):
        cpp, _c, _cxx = tokenize_compile_flags("-I/usr/include -D FOO", "", "")
        assert cpp == ["-I/usr/include"]

    def test_tokenize_handles_empty_strings(self):
        cpp, c, cxx = tokenize_compile_flags("", "", "")
        assert cpp == []
        assert c == []
        assert cxx == []
