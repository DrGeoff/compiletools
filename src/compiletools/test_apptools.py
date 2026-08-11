"""Unit tests for apptools.py utility functions."""

import builtins
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import configargparse
import pytest
import stringzilla as sz

import compiletools.apptools as apptools
import compiletools.apptools_validate as apptools_validate
import compiletools.compilation_database as cdb
import compiletools.configutils as cu
import compiletools.hunter
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.apptools import (
    _AccumulatingConfigFileParser,
    _add_xxpend_argument,
    _add_xxpend_arguments,
    _check_legacy_cas_config_keys,
    _check_legacy_variant_config_keys,
    _ComposingArgumentParser,
    _flatten_variables,
    _pkg_config_provenance_label,
    _safely_unquote_string,
    _strip_quotes,
    _test_compiler_functionality,
    add_base_arguments,
    add_link_arguments,
    add_locking_arguments,
    add_output_directory_arguments,
    add_target_arguments,
    add_target_arguments_ex,
    cached_pkg_config,
    clear_cache,
    compiler_default_cxx_std,
    derive_c_compiler_from_cxx,
    extract_command_line_macros,
    extract_command_line_macros_sz,
    extract_system_include_paths,
    filter_pkg_config_cflags,
    find_system_header,
    terminalcolumns,
    tokenize_compile_flags,
    unsupplied_replacement,
    verbose_print_args,
    verboseprintconfig,
)
from compiletools.build_apply import get_build_state
from compiletools.build_context import BuildContext


@pytest.fixture
def parsers_reset():
    """Wipe the configargparse parser cache around tests that go through
    ``parseargs`` end-to-end. Opt-in via ``@pytest.mark.usefixtures``."""
    uth.reset()
    yield
    uth.reset()


@contextlib.contextmanager
def _temp_repo_with_ct_conf(variant, canonical_order):
    """Enter a TempDirContextNoChange + create `ct.conf.d/` + write a
    project `ct.conf` naming `variant` and `canonical_order`. The
    `exemarkers = [main]` + `testmarkers = unit_test.hpp` lines are
    common to all TestAppendFlagsAccumulateAcrossConfHierarchy fixtures
    and are baked in. Yields (repo_root, conf_d)."""
    with uth.TempDirContextNoChange() as repo_root:
        conf_d = os.path.join(repo_root, "ct.conf.d")
        os.makedirs(conf_d, exist_ok=True)
        with open(os.path.join(repo_root, "ct.conf"), "w") as fh:
            fh.write(f"variant = {variant}\n")
            fh.write(f"variant-canonical-order = {canonical_order}\n")
            fh.write("exemarkers = [main]\n")
            fh.write("testmarkers = unit_test.hpp\n")
        yield repo_root, conf_d


def _parseargs_for_variant(repo_root, argv, *, add_link=False):
    """Run create_parser + add_common_arguments [+ add_link_arguments] +
    parseargs under DirectoryContext + ParserContext. Returns the parsed
    args. Used by TestAppendFlagsAccumulateAcrossConfHierarchy tests that
    each repeat the same 5-line preamble."""
    with uth.DirectoryContext(repo_root):
        cap = apptools.create_parser("regression test", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        if add_link:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            return apptools.parseargs(cap, argv, context=BuildContext())


class _PkgConfigParseResult(SimpleNamespace):
    """``args`` from a ``parseargs`` run plus the ``UserWarning`` texts it
    emitted. Returned by :func:`_parseargs_with_pkg_config_conf`."""


def _parseargs_with_pkg_config_conf(ct_conf_line, *, axis_conf_line=""):
    """Run the full ``parseargs`` pipeline over a one-axis temp repo carrying
    ``ct_conf_line`` in its project ``ct.conf`` and ``axis_conf_line`` in
    ``ct.conf.d/gcc.conf``, capturing every warning raised along the way.

    ``clear_cache()`` runs first because ``cached_pkg_config`` memoises on
    ``(package, option)`` and the warning is raised inside it — a cache hit
    from an earlier test would return the empty string silently and the
    warning assertions would see nothing. ``simplefilter('always')`` defeats
    the per-location ``__warningregistry__`` dedup for the same reason.

    Link arguments are added so LDFLAGS is a registered slot and
    gather's ``want_libs`` branch queries ``pkg-config --libs``.
    """
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(repo_root, "ct.conf"), "a") as fh:
            if ct_conf_line:
                fh.write(ct_conf_line + "\n")
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
            if axis_conf_line:
                fh.write(axis_conf_line + "\n")

        clear_cache()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            args = _parseargs_for_variant(repo_root, ["--variant=gcc", "--no-git-root"], add_link=True)

    return _PkgConfigParseResult(
        args=args,
        warnings=[str(entry.message) for entry in caught],
    )


@pytest.mark.usefixtures("parsers_reset")
def test_strict_pkg_config_parseargs_renders_a_clean_remedy(capsys):
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        with pytest.raises(SystemExit) as excinfo:
            _parseargs_for_variant(
                repo_root,
                [
                    "--variant=gcc",
                    "--no-git-root",
                    "--pkg-config=compiletools-definitely-missing-pkg",
                    "--pkg-config-errors=error",
                ],
                add_link=True,
            )

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "compiletools-definitely-missing-pkg" in error_output
    assert "--pkg-config-errors=warn" in error_output


@pytest.mark.usefixtures("parsers_reset")
def test_unbalanced_quote_in_a_cli_flag_renders_a_clean_remedy(capsys):
    """CLI-level mirror of the pkg-config carve-out above, for the other
    named error this same try/except block now catches (coverage-gaps
    Task 1): a real parseargs run over a --CXXFLAGS value with an
    unbalanced quote must print an attributed, traceback-free message and
    exit(1) rather than let shlex's bare ValueError escape."""
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        with pytest.raises(SystemExit) as excinfo:
            _parseargs_for_variant(
                repo_root,
                ["--variant=gcc", "--no-git-root", '--CXXFLAGS=-DFOO="bar'],
            )

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "CXXFLAGS" in error_output
    assert '-DFOO="bar' in error_output
    assert "Traceback" not in error_output


@pytest.mark.usefixtures("parsers_reset")
def test_unbalanced_quote_in_a_cli_flag_reraises_at_high_verbosity():
    """-vv (verbose >= 2) must surface the real FlagTokenizeError with its
    traceback, mirroring the PkgConfigError carve-out's verbosity gate --
    the debugging mode must not be the one case that hides the failure."""
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        with pytest.raises(compiletools.utils.FlagTokenizeError):
            _parseargs_for_variant(
                repo_root,
                ["--variant=gcc", "--no-git-root", '--CXXFLAGS=-DFOO="bar', "-vv"],
            )


@pytest.mark.usefixtures("parsers_reset")
def test_unbalanced_quote_in_cxx_renders_a_clean_remedy(capsys):
    """coverage-gaps Task 9: _check_resolved_compiler_available tokenizes
    a wrapper-form CC/CXX/LD value (e.g. "ccache g++") OUTSIDE the
    gather_inputs try/except -- it is the first point in parseargs a
    malformed CXX/CC/LD can raise (CC/CXX/LD are exe-name strings, never
    routed through gather_inputs' own flag-slot tokenizing), and every
    CLI tool that calls parseargs reaches it. A real parseargs run over a
    wrapper-form CXX with an unbalanced quote must print an attributed,
    traceback-free message naming CXX and exit(1), not let shlex's bare
    ValueError (or an un-caught FlagTokenizeError) escape.

    add_link=True (registering LD) is load-bearing here: without it,
    args.LD is absent, _effective_link_driver falls back to the
    malformed CXX, and gather_inputs' own compiler_kind() probe (a
    different, earlier, more general call site) raises first with the
    generic "compiler command" slot instead of "CXX" -- still a clean
    SystemExit(1) (gather_inputs' try/except already covers it), just
    not the apptools_validate boundary this test targets.
    """
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write('CC = gcc\nCXX = ccache "g++\nLD = g++\n')

        with pytest.raises(SystemExit) as excinfo:
            _parseargs_for_variant(repo_root, ["--variant=gcc", "--no-git-root"], add_link=True)

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "CXX" in error_output
    assert "Traceback" not in error_output


@pytest.mark.usefixtures("parsers_reset")
def test_unbalanced_quote_in_include_renders_a_clean_remedy(capsys):
    """coverage-gaps Task 9: a real parseargs run over a --INCLUDE value
    with an unbalanced quote must print an attributed, traceback-free
    message naming INCLUDE and exit(1) (reached through gather_inputs'
    existing try/except, unlike the CXX case above)."""
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        with pytest.raises(SystemExit) as excinfo:
            _parseargs_for_variant(
                repo_root,
                ["--variant=gcc", "--no-git-root", '--INCLUDE=/opt/"unterminated'],
            )

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "INCLUDE" in error_output
    assert "Traceback" not in error_output


@pytest.mark.usefixtures("parsers_reset")
def test_strip_quotes_error_names_the_cli_spelling(capsys):
    """quote-followups Task 2: the unbalanced-quote error from the
    quote-stripping pre-pass (_strip_quotes) must name the option as the
    user typed it (prepend-INCLUDE), not the argparse dest
    (prepend_include) -- one option, one spelling, matching what
    gather-side tokenizers (build_inputs._slot_tokens et al.) already say
    for the same option. Before the fix, _strip_quotes passed the raw
    vars(args) attribute name as slot, which for --prepend-INCLUDE /
    --append-INCLUDE is the dest, not the CLI spelling."""
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        with pytest.raises(SystemExit) as excinfo:
            _parseargs_for_variant(
                repo_root,
                ["--variant=gcc", "--no-git-root", '--prepend-INCLUDE="/opt/unclosed'],
            )

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "prepend-INCLUDE" in error_output
    assert "prepend_include" not in error_output
    assert "Traceback" not in error_output


@pytest.mark.usefixtures("parsers_reset")
def test_single_token_quoted_include_path_with_space_survives_real_parseargs():
    """coverage-gaps Task 9 review finding Q1: a SINGLE-TOKEN --INCLUDE
    value containing a space (no manual quoting needed at this layer --
    e.g. a real shell already consumed the user's own quotes, or this is
    one argv element from a test driver) must still land as ONE include
    path after a full parseargs run, not two shredded ``-I`` fragments.

    This is the gap the gather-level positive test
    (test_build_inputs.py::test_quoted_space_containing_include_path_survives_as_one_path)
    didn't catch: it calls gather_inputs directly and bypasses
    _flatten_variables/_strip_quotes entirely. Real parseargs exercises
    the full pipeline: argparse's nargs="+" gives args.INCLUDE the
    one-element list ``["/opt/has space/include"]``; _flatten_variables
    shlex.joins it into the single-token quoted string
    ``"'/opt/has space/include'"`` (quoting is required because the
    element contains a space); _strip_quotes must NOT cosmetically peel
    that quote back off before gather_inputs' own shlex-tokenize ever
    sees it, or the space becomes a token separator again.
    """
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        args = _parseargs_for_variant(
            repo_root,
            ["--variant=gcc", "--no-git-root", "--INCLUDE=/opt/has space/include"],
        )

    cpp = get_build_state(args).flags.cpp
    assert "/opt/has space/include" in cpp, f"path was shredded: {cpp!r}"
    idx = cpp.index("/opt/has space/include")
    assert cpp[idx - 1] == "-I", f"expected -I immediately before the path token, got {cpp!r}"
    assert "/opt/has" not in cpp, f"a shredded fragment leaked into cpp tokens: {cpp!r}"


@pytest.mark.usefixtures("parsers_reset")
def test_double_quoted_include_path_with_space_survives_real_parseargs():
    """Nested-quote variant of the test above: the literal value carries
    its OWN double-quote layer (as a conf file's raw text would, e.g.
    ``INCLUDE = "/opt/has space/include"``) on top of _flatten_variables'
    protective single-quote layer. _safely_unquote_string's round-trip
    check must peel exactly the outer (redundant) layer and stop at the
    inner (load-bearing) one, landing on the bare path with no residual
    quote characters -- not zero layers peeled (stray literal quotes in
    the path) and not both layers peeled (reshredded on the space)."""
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        args = _parseargs_for_variant(
            repo_root,
            ["--variant=gcc", "--no-git-root", '--INCLUDE="/opt/has space/include"'],
        )

    cpp = get_build_state(args).flags.cpp
    assert "/opt/has space/include" in cpp, f"path was shredded or left quoted: {cpp!r}"


@pytest.mark.usefixtures("parsers_reset")
def test_prepend_include_path_with_space_survives_real_parseargs():
    """coverage-gaps final-review-v2 Important #1: the bare-INCLUDE fix
    above (test_double_quoted_include_path_with_space_survives_real_parseargs)
    did not cover --prepend-INCLUDE / --append-INCLUDE -- their argparse
    dest names ("prepend_include" / "append_include") were missing from
    _ATOMIC_TOKEN_REPARSED_ATTRS, so _strip_quotes' list branch cosmetically
    peeled the value's own quote layer (prepend_include isn't one of
    _FLATTENED_REPARSED_ATTRS, so there's no _flatten_variables protective
    re-quote to restore it), and the bare space then re-split into two
    shredded -I fragments at _include_paths_with_gitroots' downstream shlex
    re-tokenize.

    Uses an embedded quote layer (as a conf value ``prepend-INCLUDE =
    "/opt/has space/include"`` would carry, or the CLI equivalent) --
    mirrors the double-quoted bare-INCLUDE test above, since a bare
    unquoted CLI list element never reaches _safely_unquote_string's
    quote-stripping branch at all.

    Mutation guard: remove "prepend_include" from _ATOMIC_TOKEN_REPARSED_ATTRS
    and this test fails (two include paths instead of one).
    """
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        args = _parseargs_for_variant(
            repo_root,
            ["--variant=gcc", "--no-git-root", '--prepend-INCLUDE="/opt/has space/include"'],
        )

    cpp = get_build_state(args).flags.cpp
    assert "/opt/has space/include" in cpp, f"path was shredded: {cpp!r}"
    idx = cpp.index("/opt/has space/include")
    assert cpp[idx - 1] == "-I", f"expected -I immediately before the path token, got {cpp!r}"
    assert "/opt/has" not in cpp, f"a shredded fragment leaked into cpp tokens: {cpp!r}"


@pytest.mark.usefixtures("parsers_reset")
def test_append_include_path_with_space_survives_real_parseargs():
    """coverage-gaps final-review-v2 Important #1: --append-INCLUDE sibling
    of the prepend test above -- same gate, same shredding bug, same fix.

    Mutation guard: remove "append_include" from _ATOMIC_TOKEN_REPARSED_ATTRS
    and this test fails (two include paths instead of one).
    """
    with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")

        args = _parseargs_for_variant(
            repo_root,
            ["--variant=gcc", "--no-git-root", '--append-INCLUDE="/opt/has space/include"'],
        )

    cpp = get_build_state(args).flags.cpp
    assert "/opt/has space/include" in cpp, f"path was shredded: {cpp!r}"
    idx = cpp.index("/opt/has space/include")
    assert cpp[idx - 1] == "-I", f"expected -I immediately before the path token, got {cpp!r}"
    assert "/opt/has" not in cpp, f"a shredded fragment leaked into cpp tokens: {cpp!r}"


class TestExtractCommandLineMacrosSz:
    """Test extract_command_line_macros_sz()."""

    def test_basic_define_with_value(self):
        args = SimpleNamespace(CPPFLAGS=[sz.Str("-DFOO=bar")])
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS")])
        assert result[sz.Str("FOO")] == sz.Str("bar")

    def test_define_no_value(self):
        args = SimpleNamespace(CPPFLAGS=[sz.Str("-DFOO")])
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS")])
        assert result[sz.Str("FOO")] == sz.Str("1")

    def test_empty_flags(self):
        args = SimpleNamespace(CPPFLAGS=[])
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS")])
        assert result == {}

    def test_no_attribute(self):
        args = SimpleNamespace()
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS")])
        assert result == {}

    def test_non_define_flags_ignored(self):
        args = SimpleNamespace(CPPFLAGS=[sz.Str("-I/usr/include"), sz.Str("-DFOO=1"), sz.Str("-O2")])
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS")])
        assert len(result) == 1
        assert result[sz.Str("FOO")] == sz.Str("1")

    def test_multiple_flag_sources(self):
        args = SimpleNamespace(CPPFLAGS=[sz.Str("-DA=1")], CXXFLAGS=[sz.Str("-DB=2")])
        result = extract_command_line_macros_sz(args, [sz.Str("CPPFLAGS"), sz.Str("CXXFLAGS")])
        assert result[sz.Str("A")] == sz.Str("1")
        assert result[sz.Str("B")] == sz.Str("2")


class TestTokenizeCompileFlagsQuoteErrors:
    """tokenize_compile_flags used to silently degrade an unbalanced-quote
    string to str.split() on ValueError, disagreeing with build_inputs'
    (bare-crash, pre-fix) path. It must now raise the same attributed
    FlagTokenizeError build_inputs._slot_tokens raises (coverage-gaps
    Task 1) -- consistent behavior across both consumers of the raw slot
    strings, no silent fallback."""

    def test_well_formed_strings_still_tokenize(self):
        cpp, c, cxx = tokenize_compile_flags("-DA=1 -I/x", "-Wall", "-O2 -std=c++20")
        assert cpp == ["-I/x"]  # -D stripped
        assert c == ["-Wall"]
        assert cxx == ["-O2", "-std=c++20"]

    def test_pretokenized_lists_pass_through_unaffected(self):
        """A pre-tokenized list input must not go anywhere near shlex, so a
        literal embedded quote character in one element is not an error."""
        cpp, _c, _cxx = tokenize_compile_flags(['-DFOO="bar"'], [], [])
        assert cpp == []  # -D stripped, no crash

    def test_unbalanced_quote_in_cppflags_raises_attributed_to_cppflags(self):
        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CPPFLAGS"):
            tokenize_compile_flags('-DFOO="bar', "", "")

    def test_unbalanced_quote_in_cflags_raises_attributed_to_cflags(self):
        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CFLAGS"):
            tokenize_compile_flags("", '-DFOO="bar', "")

    def test_unbalanced_quote_in_cxxflags_raises_attributed_to_cxxflags(self):
        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CXXFLAGS"):
            tokenize_compile_flags("", "", '-DFOO="bar')

    def test_it_no_longer_silently_falls_back_to_str_split(self):
        """Regression pin for the removed fallback: str.split() on the
        malformed value would have produced ['-DFOO=\"bar'] (one token,
        quotes intact) without ever raising -- assert the raise happens
        instead of that silent degrade."""
        with pytest.raises(compiletools.utils.FlagTokenizeError):
            tokenize_compile_flags('-DFOO="bar', "", "")


class TestFindHeaderInPaths:
    """Test find_header_in_paths(), the shared helper behind find_system_header
    and the READMACROS resolution path."""

    def test_resolves_through_a_symlinked_include_path(self, tmp_path):
        """A relative header_name containing '..' must resolve against the
        symlink's TARGET, matching plain os.path.join semantics -- not
        against its lexical parent, which is what os.path.abspath /
        os.path.normpath would collapse it to instead.

        A decoy file also sits at the lexical (wrong) location so a
        regression discriminates "picked the wrong file" from "found
        nothing" rather than just failing to resolve."""
        real_dir = tmp_path / "real" / "deep"
        real_dir.mkdir(parents=True)
        (tmp_path / "real" / "wanted.h").write_text("// real\n")
        link_dir = tmp_path / "x"
        link_dir.mkdir()
        link = link_dir / "link"
        link.symlink_to(real_dir)
        (tmp_path / "x" / "wanted.h").write_text("// lexical decoy\n")

        result = apptools.find_header_in_paths("../wanted.h", [str(link)])

        assert result == os.path.realpath(str(tmp_path / "real" / "wanted.h"))

    def test_system_header_wording_warns_even_on_empty_include_paths(self, capsys):
        apptools.find_header_in_paths("h", [], verbose=9, label="System header")
        captured = capsys.readouterr()
        assert captured.out.strip() == "System header 'h' not found in include paths: []"

    def test_readmacros_wording_is_silent_on_empty_include_paths(self, capsys):
        apptools.find_header_in_paths(
            "h", [], verbose=9, label="READMACROS", paths_label="file-declared include paths", warn_on_empty=False
        )
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_readmacros_wording_names_file_declared_paths(self, capsys):
        apptools.find_header_in_paths(
            "h",
            ["/no/such"],
            verbose=9,
            label="READMACROS",
            paths_label="file-declared include paths",
            warn_on_empty=False,
        )
        captured = capsys.readouterr()
        assert captured.out.strip() == "READMACROS 'h' not found in file-declared include paths: ['/no/such']"


class TestFindSystemHeader:
    """Test find_system_header()."""

    def test_header_found(self, tmp_path):
        (tmp_path / "myheader.h").write_text("// header\n")
        args = _finalized_args(CPPFLAGS=f"-I{tmp_path}", CFLAGS="", CXXFLAGS="", INCLUDE="")
        result = find_system_header("myheader.h", args)
        assert result is not None
        assert result.endswith("myheader.h")

    def test_header_not_found(self, tmp_path):
        args = _finalized_args(CPPFLAGS=f"-I{tmp_path}", CFLAGS="", CXXFLAGS="", INCLUDE="")
        result = find_system_header("nonexistent.h", args)
        assert result is None


class TestFilterPkgConfigCflags:
    """Test filter_pkg_config_cflags()."""

    def test_converts_I_to_isystem(self):
        result = filter_pkg_config_cflags("-I/usr/local/include")
        assert "-isystem" in result
        assert "/usr/local/include" in result

    def test_drops_default_usr_include(self):
        result = filter_pkg_config_cflags("-I/usr/include")
        assert result.strip() == ""

    def test_preserves_non_I_flags(self):
        result = filter_pkg_config_cflags("-DFOO")
        assert "-DFOO" in result

    def test_empty_input(self):
        result = filter_pkg_config_cflags("")
        assert result == ""

    def test_mixed_flags(self):
        result = filter_pkg_config_cflags("-I/opt/local/include -DBAR=1 -Wall")
        assert "-isystem" in result
        assert "/opt/local/include" in result


class TestSafelyUnquoteString:
    """Test _safely_unquote_string()."""

    def test_unquote_double_quotes(self):
        assert _safely_unquote_string('"hello"') == "hello"

    def test_unquote_single_quotes(self):
        assert _safely_unquote_string("'hello'") == "hello"

    def test_no_quotes(self):
        assert _safely_unquote_string("hello") == "hello"

    def test_non_string(self):
        assert _safely_unquote_string(42) == 42

    def test_malformed_quotes_fallback(self):
        """An unbalanced quote now raises FlagTokenizeError by default
        (Task 9: DRY unbalanced-quote handling), instead of silently
        stripping a mismatched quote pair."""
        with pytest.raises(compiletools.utils.FlagTokenizeError):
            _safely_unquote_string("'hello")

    def test_malformed_quotes_raise_on_malformed_false_keeps_old_fallback(self):
        """raise_on_malformed=False (as _strip_quotes passes for
        _UNQUOTE_RAISE_EXEMPT attrs, and _note_shadowed_bare_values always
        passes) preserves the pre-Task-9 best-effort strip."""
        result = _safely_unquote_string("'hello", raise_on_malformed=False)
        assert isinstance(result, str)

    def test_flattened_attr_no_whitespace_still_unquotes(self):
        """Regression guard (review finding Q1): the round-trip-safety
        check for _FLATTENED_REPARSED_ATTRS must not become a blanket
        skip. A single-token quoted value with NO internal whitespace
        (e.g. a whole-value-quoted "-DFOO") retokenizes to itself either
        way, so it must still get the ordinary cosmetic strip -- pins
        test_private_stashes_pass_through_untouched's CPPFLAGS assertion
        at the unit level."""
        assert _safely_unquote_string('"-DFOO"', slot="CPPFLAGS") == "-DFOO"
        assert _safely_unquote_string("'/opt/plain'", slot="INCLUDE") == "/opt/plain"

    def test_flattened_attr_with_whitespace_keeps_its_quoting(self):
        """Review finding Q1 fix: for a _FLATTENED_REPARSED_ATTRS slot, a
        single-token quoted value whose UNQUOTED body contains whitespace
        must be returned with its quoting intact -- stripping it here
        would let the whitespace re-split into multiple tokens at
        gather_inputs' own shlex-tokenize a moment later."""
        quoted = "'/opt/has space/include'"
        assert _safely_unquote_string(quoted, slot="INCLUDE") == quoted

    def test_non_flattened_attr_with_whitespace_still_unquotes(self):
        """Control: the round-trip-safety check is scoped to
        _FLATTENED_REPARSED_ATTRS only. Every other attr (a plain scalar
        that is never re-tokenized, e.g. projectname) keeps the ordinary
        cosmetic strip even when the unquoted body has a space --
        matches test_shadow_note_not_fooled_by_quoted_values' prebuild-
        script expectation at the unit level."""
        assert _safely_unquote_string('"My Project"', slot="projectname") == "My Project"
        assert _safely_unquote_string('"./my hook.sh"', slot="prebuild_script") == "./my hook.sh"

    def test_nested_quote_layers_peel_exactly_to_the_load_bearing_one(self):
        """A doubly-quoted _FLATTENED_REPARSED_ATTRS value (an outer
        protective layer _flatten_variables added, wrapping an inner
        layer the user/conf file supplied) must peel exactly the
        redundant outer layer and stop at the inner, load-bearing one --
        not zero layers (stray literal quote characters survive into the
        path) and not both layers (reshredded on the space)."""
        nested = "'\"/opt/has space/include\"'"
        result = _safely_unquote_string(nested, slot="INCLUDE")
        assert result == '"/opt/has space/include"'
        # And that remaining single layer is exactly what a real shlex
        # re-tokenize downstream needs to land on the bare, clean path.
        assert compiletools.utils.split_command_cached(result) == ["/opt/has space/include"]


class TestVerbosePrintArgs:
    """Test verbose_print_args()."""

    def test_prints_args(self):
        args = SimpleNamespace(foo="bar", baz=42, empty=None)
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=120):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "foo" in output
        assert "bar" in output
        assert "baz" in output
        assert "42" in output

    def test_long_value_wraps(self):
        """When value exceeds terminal width, it should be split."""
        args = SimpleNamespace(longattr="word1 word2 word3 word4 word5 word6 word7 word8 word9 word10")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=40):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "longattr" in output

    def test_small_terminal_aborts(self):
        """When terminal is too small, should print abort message."""
        args = SimpleNamespace(x="val")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=3):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "aborted" in output.lower()

    def test_verbose_print_args_redacts_otel_headers(self):
        """Secret-bearing attrs must not leak into -vv output (CI log leak)."""
        args = SimpleNamespace(
            foo="bar",
            otel_headers="x-honeycomb-team=SECRET-TOKEN-DO-NOT-LOG",
            baz=42,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=120):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "SECRET-TOKEN-DO-NOT-LOG" not in output
        assert "REDACTED" in output
        assert "otel_headers" in output
        assert "bar" in output
        assert "42" in output

    def test_verbose_print_args_otel_headers_none_prints_blank(self):
        """A None/empty otel_headers should print the normal blank row, not the placeholder."""
        args = SimpleNamespace(otel_headers=None, foo="bar")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=120):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "REDACTED" not in output
        assert "otel_headers" in output

    def test_verbose_print_args_shows_derived_flag_strings_from_state(self):
        """The -vv banner promises the FINAL aggregated build values, so
        the four flag slots must display the stashed BuildState's derived
        strings (unify copies cxx into cpp), not the raw pre-gather attrs
        (which hold the internal unsupplied sentinel for CPPFLAGS)."""
        from compiletools.build_apply import populate_args
        from compiletools.build_inputs import gather_inputs
        from compiletools.build_state import compute_build_state

        args = SimpleNamespace(
            verbose=0,
            CPPFLAGS="unsupplied_implies_use_CXXFLAGS",
            CXXFLAGS="-O2 -DDERIVED_VISIBLE",
        )
        populate_args(args, compute_build_state(gather_inputs(args, BuildContext())))
        derived = get_build_state(args)
        assert "unsupplied" not in derived.cppflags
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=400):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        cppflags_row = next(line for line in output.splitlines() if line.startswith("CPPFLAGS"))
        assert "unsupplied" not in cppflags_row, "raw sentinel leaked into the -vv CPPFLAGS row"
        assert "-DDERIVED_VISIBLE" in cppflags_row

    def test_verbose_print_args_without_state_prints_raw_attrs(self):
        """A namespace with no stashed BuildState (pre-parseargs or a
        bare fixture) must still print without raising."""
        args = SimpleNamespace(CXXFLAGS="-O2", foo="bar")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("compiletools.apptools.terminalcolumns", return_value=120):
                verbose_print_args(args)
        output = mock_stdout.getvalue()
        assert "-O2" in output
        assert "foo" in output


class TestUnsuppliedReplacement:
    def test_unsupplied_returns_default(self):
        result = unsupplied_replacement(apptools._UNSUPPLIED_USE_CXX, "g++", 0, "CPP")
        assert result == "g++"

    def test_supplied_returns_original(self):
        result = unsupplied_replacement("clang++", "g++", 0, "CXX")
        assert result == "clang++"

    def test_verbose_prints(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            unsupplied_replacement(apptools._UNSUPPLIED_USE_CXX, "g++", 6, "CPP")
        assert "unsupplied" in mock_stdout.getvalue()


class TestGatherGitrootIncludeOrderDeterministic:
    """Regression: gather's gitroot widening must emit git roots in a
    deterministic order. Set iteration order depends on ``PYTHONHASHSEED``, so
    a naive ``list(set)`` join shifts the ``-I`` order between processes,
    invalidating the cas-objdir cache key (cxxflags_tokens hash component) on
    no-op rebuilds."""

    _SCRIPT = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "import compiletools.git_utils\n"
        "import compiletools.apptools as apptools\n"
        "# Mock find_git_root to return distinct roots per call. The first\n"
        "# (no-arg) call returns the cwd-root; subsequent (per-filename)\n"
        "# calls return alternating roots. Using strings designed to hash\n"
        "# differently under different PYTHONHASHSEEDs.\n"
        "_ROOTS = ['/repo/alpha', '/repo/beta', '/repo/gamma', '/repo/delta']\n"
        "_calls = {'n': 0}\n"
        "def _fake_find_git_root(filename=None):\n"
        "    if filename is None:\n"
        "        return _ROOTS[0]\n"
        "    idx = (_calls['n'] % (len(_ROOTS) - 1)) + 1\n"
        "    _calls['n'] += 1\n"
        "    return _ROOTS[idx]\n"
        "compiletools.git_utils.find_git_root = _fake_find_git_root\n"
        "import compiletools.build_inputs as bi\n"
        "args = SimpleNamespace(\n"
        "    git_root=True,\n"
        "    INCLUDE='',\n"
        "    filename=['a.cpp', 'b.cpp', 'c.cpp'],\n"
        "    tests=[], static=[], dynamic=[],\n"
        "    verbose=0,\n"
        ")\n"
        "paths = bi._include_paths_with_gitroots(args, _ROOTS[0])\n"
        "sys.stdout.write(' '.join(paths))\n"
    )

    def _run_with_seed(self, seed):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(
            [sys.executable, "-c", self._SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def test_include_order_is_deterministic_across_pythonhashseeds(self):
        outputs = [self._run_with_seed(seed) for seed in (0, 1, 2, 3, 4, 5, 6, 7)]
        # All runs must produce identical INCLUDE strings; otherwise the
        # cxxflags_tokens hash component of cas-objdir keys shifts between
        # processes and forces a full re-link on no-op rebuilds.
        assert len(set(outputs)) == 1, f"Non-deterministic git-root ordering across PYTHONHASHSEEDs: {outputs!r}"


def _finalized_args(**slots):
    """SimpleNamespace routed through finalize_flag_state so the
    BuildState-reading apptools helpers accept it."""
    args = SimpleNamespace(verbose=0, **slots)
    uth.finalize_flag_state(args)
    return args


class TestExtractSystemIncludePaths:
    def test_dash_I_attached(self):
        args = _finalized_args(CPPFLAGS="-I/foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_dash_I_detached(self):
        args = _finalized_args(CPPFLAGS="-I /foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_isystem_detached(self):
        args = _finalized_args(CPPFLAGS="-isystem /foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_no_flags(self):
        args = _finalized_args(CPPFLAGS="", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert result == []

    def test_deduplicates(self):
        args = _finalized_args(CPPFLAGS="-I/foo", CXXFLAGS="-I/foo")
        result = extract_system_include_paths(args)
        assert result.count("/foo") == 1

    def test_custom_flag_sources(self):
        args = _finalized_args(CFLAGS="-I/bar")
        result = extract_system_include_paths(args, flag_sources=["CFLAGS"])
        assert "/bar" in result

    def test_missing_attribute(self):
        args = _finalized_args()
        result = extract_system_include_paths(args, flag_sources=["CPPFLAGS"])
        assert result == []

    def test_dangling_flag_does_not_swallow_next_slot(self):
        """Slot boundaries are walk boundaries: a malformed dangling -I at
        the end of CPPFLAGS must not consume the first CXXFLAGS token."""
        args = _finalized_args(CPPFLAGS="-DFOO -I", CXXFLAGS="/spurious -I/real")
        result = extract_system_include_paths(args)
        assert result == ["/real"]


class TestExtractCommandLineMacros:
    def test_basic_define(self):
        args = _finalized_args(CPPFLAGS="-DFOO=bar", CFLAGS="", CXXFLAGS="", CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["FOO"] == "bar"

    def test_define_no_value(self):
        args = _finalized_args(CPPFLAGS="-DFOO", CFLAGS="", CXXFLAGS="", CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["FOO"] == "1"

    def test_multiple_defines(self):
        args = _finalized_args(CPPFLAGS="-DA=1 -DB=2", CFLAGS="", CXXFLAGS="", CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["A"] == "1"
        assert result["B"] == "2"

    def test_empty_flags(self):
        args = _finalized_args(CPPFLAGS="", CFLAGS="", CXXFLAGS="", CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result == {}

    def test_non_define_ignored(self):
        args = _finalized_args(CPPFLAGS="-Wall -O2", CFLAGS="", CXXFLAGS="", CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result == {}

    def test_extract_command_line_macros_handles_detached_d_form(self):
        """Detached -D form (separate -D and value tokens) was previously
        silently dropped by extract_command_line_macros. Must now be
        recognized so it's consistent with cmdline_d_macro_names."""
        args = _finalized_args(CPPFLAGS="-D FOO=1 -D BAR", CFLAGS="", CXXFLAGS="", CXX=None)
        macros = extract_command_line_macros(args, include_compiler_macros=False)
        assert macros == {"FOO": "1", "BAR": "1"}  # bare -D BAR defaults value to "1"


class TestFlattenVariables:
    def test_flattens_lists(self):
        args = SimpleNamespace(CPPFLAGS=["-Wall", "-O2"], CFLAGS="-g", CXXFLAGS="-Wall", INCLUDE=["/foo", "/bar"])
        _flatten_variables(args)
        assert args.CPPFLAGS == "-Wall -O2"
        assert args.INCLUDE == "/foo /bar"
        assert args.CFLAGS == "-g"  # Already a string, unchanged

    def test_no_change_for_strings(self):
        args = SimpleNamespace(CPPFLAGS="-Wall", CFLAGS="-g", CXXFLAGS="-Wall", INCLUDE="/foo")
        _flatten_variables(args)
        assert args.CPPFLAGS == "-Wall"

    def test_token_with_embedded_space_survives_roundtrip(self):
        """A token containing an embedded space must survive the _flatten_variables →
        shlex.split round-trip.

        When the user passes '--CPPFLAGS' with a quoted value to the CLI, the shell
        consumes the outer quotes before argv reaches argparse.  With ``nargs='+'``,
        configargparse stores the already-shell-split token directly in the list — e.g.
        ``['-DFOO=bar baz', '-Wall']`` — where ``'-DFOO=bar baz'`` is a single token
        that happens to contain a space.

        ``' '.join(['-DFOO=bar baz', '-Wall'])`` produces ``'-DFOO=bar baz -Wall'``; a
        downstream ``shlex.split`` then splits on the internal space and yields
        ``['-DFOO=bar', 'baz', '-Wall']`` — three tokens instead of two.

        ``shlex.join`` re-adds quoting around the space-containing token so the
        round-trip is lossless.  Cousin fix to commit 5cd77781 which patched the same
        pattern in ``_unify_cpp_cxx_flags`` and ``_deduplicate_all_flags``.
        """

        args = SimpleNamespace(
            CPPFLAGS=["-DFOO=bar baz", "-Wall"],
            CFLAGS=["-DFOO=bar baz", "-Wall"],
            CXXFLAGS=["-DFOO=bar baz", "-Wall"],
            INCLUDE=["/foo", "/bar"],
        )
        _flatten_variables(args)

        for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
            tokens = shlex.split(getattr(args, slot))
            assert tokens == ["-DFOO=bar baz", "-Wall"], (
                f"token with embedded space was mangled in {slot} after _flatten_variables → "
                f"shlex.split round-trip: expected ['-DFOO=bar baz', '-Wall'], got {tokens!r}.  "
                f"Likely cause: _flatten_variables uses ' '.join instead of shlex.join to "
                f"reconstruct the raw flag string.  See cousin commit 5cd77781."
            )


class TestStripQuotes:
    def test_strips_string_quotes(self):
        args = SimpleNamespace(foo='"hello"', bar="'world'", num=42, lst=['"a"', "'b'"])
        _strip_quotes(args)
        assert args.foo == "hello"
        assert args.bar == "world"
        assert args.num == 42
        assert args.lst == ["a", "b"]

    def test_none_values_skipped(self):
        args = SimpleNamespace(foo=None)
        _strip_quotes(args)
        assert args.foo is None

    def test_private_stashes_pass_through_untouched(self):
        """Private attrs are internal stashes, not user-supplied values.
        Iterating-and-assigning a dict stash crashes (RuntimeError from
        mid-iteration key inserts) and a tuple stash rejects item
        assignment (TypeError), so _strip_quotes must skip private
        attributes entirely."""
        args = SimpleNamespace(
            CPPFLAGS='"-DFOO"',
            _dict_stash={"CPPFLAGS": '"-DFOO"'},
            _tuple_stash=(("CPPFLAGS", '"-DFOO"'),),
            _argv=['--CPPFLAGS="-DFOO"'],
        )
        _strip_quotes(args)
        assert args.CPPFLAGS == "-DFOO"
        assert args._dict_stash == {"CPPFLAGS": '"-DFOO"'}
        assert args._tuple_stash == (("CPPFLAGS", '"-DFOO"'),)
        assert args._argv == ['--CPPFLAGS="-DFOO"']


def test_cli_spelling_for_dest_maps_and_falls_back():
    """quote-followups Task 2: _cli_spelling_for_dest maps an argparse
    dest to the longest registered CLI option string (stripped of its
    leading dashes) for the option that owns it, and falls back to the
    dest itself when the parser is unavailable or the dest is unknown --
    the fallback is what keeps every existing direct _strip_quotes(args)
    caller (no cap) working unchanged."""
    cap = apptools.create_parser("mapper test", argv=[], include_config=False)
    compiletools.hunter.add_arguments(cap)
    assert apptools._cli_spelling_for_dest("prepend_include", cap) == "prepend-INCLUDE"
    assert apptools._cli_spelling_for_dest("INCLUDE", cap) == "INCLUDE"
    assert apptools._cli_spelling_for_dest("no_such_dest", cap) == "no_such_dest"
    assert apptools._cli_spelling_for_dest("prepend_include", None) == "prepend_include"


class TestDeriveCCompilerFromCxx:
    def test_gpp_to_gcc(self):
        assert derive_c_compiler_from_cxx("g++") == "gcc"

    def test_clangpp_to_clang(self):
        assert derive_c_compiler_from_cxx("clang++") == "clang"

    def test_unknown_returns_same(self):
        assert derive_c_compiler_from_cxx("icc") == "icc"


class TestTerminalColumns:
    def test_returns_int(self):
        result = terminalcolumns()
        assert isinstance(result, int)
        assert result > 0

    def test_fallback_on_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = terminalcolumns()
        assert result == 80


class TestClearCache:
    def test_clear_cache_runs(self):
        # Populate the cache with a dummy call
        clear_cache()

        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                cached_pkg_config("nonexistent_test_pkg_clear_cache", "--cflags")

        assert [w for w in caught if "nonexistent_test_pkg_clear_cache" in str(w.message)], (
            f"expected the absent package to be named in a warning, got {[str(w.message) for w in caught]!r}"
        )

        assert cached_pkg_config.cache_info().currsize > 0, "Cache should be populated"

        clear_cache()

        assert cached_pkg_config.cache_info().currsize == 0, "Cache should be cleared"


class TestAddArguments:
    """Test the various add_*_arguments functions create valid parsers."""

    def test_add_base_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_base_arguments(cap, argv=[], variant="gcc.debug")
        args = cap.parse_args([])
        assert args.variant == "gcc.debug"
        assert args.verbose == 0

    def test_add_locking_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_base_arguments(cap, argv=[], variant="test")
        add_locking_arguments(cap)
        args = cap.parse_args([])
        assert args.lock_cross_host_timeout == 600
        assert args.lock_warn_interval == 60
        assert args.sleep_interval_lockdir is None

    def test_add_link_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_base_arguments(cap, argv=[], variant="test")
        add_link_arguments(cap)
        args = cap.parse_args([])
        assert "unsupplied" in args.LD

    def test_add_output_directory_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_output_directory_arguments(cap, variant="gcc.debug")
        args = cap.parse_args([])
        assert "gcc.debug" in args.bindir
        # cas-*dir defaults are the literal sentinel; the real path is
        # computed by resolve_cas_directory_arguments (which is called
        # post-parse by apptools.parseargs / explicit diagnostic-tool
        # callers). Until then, the value carries the sentinel.
        assert args.cas_objdir == "unsupplied"
        # Confirm the resolver turns the sentinel into a real path that
        # mentions the obj kind segment.
        args.variant = "gcc.debug"
        args.verbose = 0
        apptools.resolve_cas_directory_arguments(args)
        assert "obj" in args.cas_objdir

    def test_add_output_directory_arguments_unsupplied_variant_uses_sentinel(self):
        """Regression: ``Namer.add_arguments(cap)`` (called from cake.py
        before the variant has been resolved) passes ``variant="unsupplied"``
        through to ``add_output_directory_arguments``. The bindir default
        must register as the bare sentinel ``"unsupplied"`` -- NOT
        ``"bin/unsupplied"`` -- so gather's ``_raw_dir_value`` maps it to
        unsupplied (exact-membership check against
        ``_UNSUPPLIED_SENTINELS``) and ``stage_resolve_names`` applies the
        ``bin/<variant>`` default. Otherwise every build lands in
        ``bin/unsupplied/`` instead of ``bin/<variant>/``.
        """
        cap = configargparse.ArgParser(default_config_files=[])
        add_output_directory_arguments(cap, variant="unsupplied")
        args = cap.parse_args([])
        assert args.bindir == "unsupplied", (
            f"bindir default should be the bare sentinel when variant is 'unsupplied', got {args.bindir!r}"
        )
        swapped = unsupplied_replacement(args.bindir, "bin/gcc.debug", 0, "bindir")
        assert swapped == "bin/gcc.debug"

    def test_add_output_directory_arguments_registers_use_mtime(self):
        """--use-mtime must be registered for every backend that
        calls add_output_directory_arguments — without this, ``ct-cake
        --backend=ninja --use-mtime`` is rejected by argparse even
        though ninja_backend.py reads ``args.use_mtime``.
        """
        cap = configargparse.ArgParser(default_config_files=[])
        add_output_directory_arguments(cap, variant="gcc.debug")
        args = cap.parse_args(["--use-mtime"])
        assert args.use_mtime is True
        args = cap.parse_args(["--no-use-mtime"])
        assert args.use_mtime is False

    def test_add_target_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_target_arguments(cap)
        args = cap.parse_args([])
        assert args.filename == []

    def test_add_target_arguments_ex(self):
        cap = configargparse.ArgParser(default_config_files=[])
        add_target_arguments_ex(cap)
        args = cap.parse_args([])
        assert hasattr(args, "projectversion")
        assert hasattr(args, "projectversioncmd")
        assert hasattr(args, "projectname")
        assert hasattr(args, "projectnamecmd")

    def test_add_target_arguments_ex_registers_test_xml_dir(self):
        """The --test-xml-dir flag must be registered next to --TESTPREFIX
        on every parser that calls add_target_arguments_ex(), so ct-cake
        and ct-cmakelists both pick it up. Default is None (no XML)."""
        cap = configargparse.ArgParser(default_config_files=[])
        add_target_arguments_ex(cap)
        args = cap.parse_args([])
        assert args.test_xml_dir is None
        args = cap.parse_args(["--test-xml-dir", "test-results"])
        assert args.test_xml_dir == "test-results"

    def test_add_xxpend_argument(self):
        cap = configargparse.ArgParser(default_config_files=[])
        _add_xxpend_argument(cap, "cppflags")
        args = cap.parse_args([])
        assert args.prepend_cppflags == []
        assert args.append_cppflags == []

    def test_add_xxpend_arguments(self):
        cap = configargparse.ArgParser(default_config_files=[])
        _add_xxpend_arguments(cap, ("cppflags", "cflags"))
        args = cap.parse_args([])
        assert args.prepend_cppflags == []
        assert args.append_cflags == []

    def test_add_xxpend_with_destname(self):
        cap = configargparse.ArgParser(default_config_files=[])
        _add_xxpend_argument(cap, "linkflags", destname="ldflags", extrahelp="Synonym.")
        args = cap.parse_args([])
        assert args.prepend_ldflags == []


class TestFilterPkgConfigCflagsExtended:
    def test_detached_I_flag(self):
        result = filter_pkg_config_cflags("-I /opt/include")
        assert "-isystem" in result
        assert "/opt/include" in result

    def test_trailing_I_flag(self):
        result = filter_pkg_config_cflags("-I")
        # Should preserve trailing -I as-is
        assert result != ""

    def test_verbose_drops_system(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            filter_pkg_config_cflags("-I/usr/include", verbose=6)
        assert "Dropping" in mock_stdout.getvalue()

    def test_malformed_output_degrades_to_whitespace_split_with_warning_at_verbose_1(self, capsys):
        """coverage-gaps Task 10: pkg-config subprocess output (a third-party
        .pc file's --cflags text) is not user input -- an unbalanced quote
        must degrade to a plain whitespace split, never raise, with a
        verbose>=1 diagnostic naming the offending package so the
        degradation isn't silent."""
        malformed = '-DFOO="bar -I/opt/x/include'
        result = filter_pkg_config_cflags(malformed, verbose=1, package="mypkg")
        # Degraded via whitespace split: the -I flag is still recognised and
        # rewritten to -isystem by the per-token loop that runs afterwards.
        assert "-isystem" in result
        assert "/opt/x/include" in result
        error_output = capsys.readouterr().err
        assert "mypkg" in error_output

    def test_malformed_output_is_silent_at_verbose_0(self, capsys):
        """final-review-v2 Minor #4: this test used to discard the return
        value and assert only silence, so a hypothetical refactor that made
        filter_pkg_config_cflags silently return "" on malformed input
        (dropping the degrade-to-whitespace-split half while staying quiet)
        would keep it green -- the degrade half at verbose 0 was pinned
        only by the sibling verbose>=1 test above and the build_inputs/
        magicflags callers. Assert the same degraded-tokens shape that
        sibling asserts, at verbose 0, so both halves (silence AND degrade)
        are pinned here too.
        """
        malformed = '-DFOO="bar -I/opt/x/include'
        result = filter_pkg_config_cflags(malformed, verbose=0, package="mypkg")
        # Degraded via whitespace split: the -I flag is still recognised and
        # rewritten to -isystem by the per-token loop that runs afterwards.
        assert "-isystem" in result
        assert "/opt/x/include" in result
        error_output = capsys.readouterr().err
        assert error_output == ""


class TestCachedPkgConfig:
    def test_missing_package(self):
        clear_cache()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = cached_pkg_config("nonexistent_pkg_12345", "--cflags")
        assert result == ""
        assert [str(w.message) for w in caught] == ["pkg-config package 'nonexistent_pkg_12345' not found"], (
            f"a missing package must be classified as missing and named: {[str(w.message) for w in caught]!r}"
        )
        clear_cache()

    def test_existing_package(self):
        clear_cache()
        with patch("subprocess.run") as mock_run:

            def side_effect(cmd, **kwargs):
                if "--exists" in cmd:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=0, stdout="-I/opt/pkg/include\n")

            mock_run.side_effect = side_effect
            result = cached_pkg_config("test_pkg_99999", "--cflags")
        assert "/opt/pkg/include" in result
        clear_cache()

    def test_override_pc_takes_priority(self, monkeypatch, tmp_path):
        """A .pc file in the override dir takes priority over one in the base dir."""
        clear_cache()

        override_dir = tmp_path / "override"
        override_dir.mkdir()
        base_dir = tmp_path / "base"
        base_dir.mkdir()

        pc_content_override = (
            "Name: TestPkg\nDescription: Override\nVersion: 1.0\n"
            "Cflags: -I/override/include -DOVERRIDE\n"
            "Libs: -L/override/lib -loverride\n"
        )
        pc_content_base = (
            "Name: TestPkg\nDescription: Base\nVersion: 1.0\nCflags: -I/base/include -DBASE\nLibs: -L/base/lib -lbase\n"
        )

        (override_dir / "testoverridepkg.pc").write_text(pc_content_override)
        (base_dir / "testoverridepkg.pc").write_text(pc_content_base)

        monkeypatch.setenv("PKG_CONFIG_PATH", f"{override_dir}{os.pathsep}{base_dir}")

        result = cached_pkg_config("testoverridepkg", "--cflags")
        assert "-DOVERRIDE" in result
        assert "-DBASE" not in result

        result_libs = cached_pkg_config("testoverridepkg", "--libs")
        assert "-loverride" in result_libs
        assert "-lbase" not in result_libs
        clear_cache()


class TestTestCompilerFunctionality:
    def test_nonexistent_compiler(self):
        assert _test_compiler_functionality("nonexistent_compiler_xyz_999") is False

    def test_version_check_fails(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert _test_compiler_functionality("fake_compiler") is False

    def test_timeout_returns_false(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            assert _test_compiler_functionality("fake_compiler") is False


class TestCompilerDefaultCxxStd:
    """Tests for ``compiler_default_cxx_std`` — the helper that asks
    a compiler what its natural default C++ dialect is, used by
    ``bazel_backend`` to align bazel's ``--cxxopt=-std=`` with the
    compiler's actual default so prebuilt PCH/BMI artefacts match
    consumer compiles inside bazel's sandbox."""

    def test_returns_none_for_empty_input(self):
        assert compiler_default_cxx_std(None) is None
        assert compiler_default_cxx_std("") is None

    def test_returns_none_for_nonexistent_compiler(self):
        assert compiler_default_cxx_std("nonexistent_compiler_xyz_999") is None

    def test_returns_none_when_compiler_exits_nonzero(self):
        clear_cache()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert compiler_default_cxx_std("fake_cxx") is None
        clear_cache()

    def test_returns_none_when_macro_missing(self):
        clear_cache()
        # Compiler ran but its -dM output didn't include __cplusplus
        # (would happen with a bogus -x mode, or a compiler that
        # doesn't speak C++).
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="#define __STDC_VERSION__ 201112L\n"),
        ):
            assert compiler_default_cxx_std("fake_cxx") is None
        clear_cache()

    @pytest.mark.parametrize(
        "cplusplus_value,expected",
        [
            ("199711L", "-std=gnu++98"),
            ("201103L", "-std=gnu++11"),
            ("201402L", "-std=gnu++14"),
            ("201703L", "-std=gnu++17"),
            ("202002L", "-std=gnu++20"),
            ("202302L", "-std=gnu++23"),
            ("202602L", "-std=gnu++26"),
        ],
    )
    def test_maps_cplusplus_to_gnu_dialect(self, cplusplus_value, expected):
        """Each canonical ``__cplusplus`` value maps to a ``gnu++NN``
        dialect — never strict ``c++NN``, because both gcc and clang
        default to gnu mode and switching to strict mode would
        undefine non-ISO built-ins (``unix``, ``linux``) and invalidate
        any prebuilt PCH that recorded them."""

        clear_cache()
        with patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout=f"#define __cplusplus {cplusplus_value}\n",
            ),
        ):
            assert compiler_default_cxx_std("fake_cxx") == expected
        clear_cache()

    def test_unknown_future_value_falls_back_to_closest_known(self):
        """A ``__cplusplus`` value newer than any in our dialect map
        (e.g. a hypothetical c++29 with value 202902) falls back to
        the closest known value below — ``gnu++NN`` is forward-
        compatible with future minor revisions."""

        clear_cache()
        with patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout="#define __cplusplus 202902L\n",  # hypothetical c++29
            ),
        ):
            assert compiler_default_cxx_std("fake_cxx") == "-std=gnu++26"
        clear_cache()


class TestVerbosePrintConfig:
    def test_verbose_level_2(self):
        args = SimpleNamespace(verbose=2, variant="gcc.debug")
        with patch("compiletools.apptools.verbose_print_args") as mock_vpa:
            verboseprintconfig(args)
        mock_vpa.assert_called_once_with(args)

    def test_verbose_level_0(self, capsys):
        args = SimpleNamespace(verbose=0, variant="gcc.debug")
        verboseprintconfig(args)
        captured = capsys.readouterr()
        assert captured.out == "", "verbose=0 should produce no output"


class TestSetupPkgConfigOverrides:
    """Tests for _setup_pkg_config_overrides()."""

    @pytest.fixture
    def pkgconfig_dir(self, tmp_path):
        """`<tmp_path>/ct.conf.d/pkgconfig/` with parents created. Used by
        the 8 tests that exercise the gitroot/cwd auto-discovery path."""
        d = tmp_path / "ct.conf.d" / "pkgconfig"
        d.mkdir(parents=True)
        return d

    def test_setup_pkg_config_overrides_emits_provenance_at_verbose_4(self, monkeypatch, tmp_path, capsys):
        """A prepend-PKG-CONFIG-PATH set in a conf file produces an
        attribution line of the form ``(from <abs_conf_path>:<lineno>)``
        at verbose>=4. Confirms the conf-file provenance side channel
        is wired through ``_setup_pkg_config_overrides_locked``."""

        conf_dir = tmp_path / "ct.conf.d"
        conf_dir.mkdir(parents=True)
        # Note the line number we assert on must match the line where
        # ``prepend-PKG-CONFIG-PATH`` appears in the file.
        conf_file = conf_dir / "myaxis.conf"
        conf_file.write_text("prepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig-foo\n")
        target_dir = conf_dir / "pkgconfig-foo"
        target_dir.mkdir()

        # Synthesize a project-level ct.conf that selects the axis, so
        # the conf-file value flows through configargparse.
        (tmp_path / "ct.conf").write_text("variant = myaxis\n")

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))

        # Don't auto-discover ct.conf.d/pkgconfig at gitroot — keep this
        # test focused on the provenance attribution path.
        argv = ["--variant=myaxis", "--no-git-root", "-vvvv"]
        with uth.DirectoryContext(str(tmp_path)):
            cap = apptools.create_parser("provenance test", argv=argv)
            apptools.add_common_arguments(cap, argv=argv)
            with uth.ParserContext():
                ctx = BuildContext()
                args = apptools.parseargs(cap, argv, context=ctx)

        captured = capsys.readouterr()
        # The conf file's prepend value resolved to <conf_dir>/pkgconfig-foo,
        # and the attribution must name the source conf and the line number.
        # Because ${CONF_DIR} was expanded, the label also includes the
        # pre-expansion literal — check for the two parts independently.
        assert "Prepended pkg-config path:" in captured.out
        assert str(target_dir) in captured.out
        assert f"from {conf_file}:1" in captured.out, (
            f"Expected attribution 'from {conf_file}:1' in stdout, got:\n{captured.out!r}"
        )
        assert "literal: ${CONF_DIR}/pkgconfig-foo" in captured.out, (
            f"Expected 'literal: ${{CONF_DIR}}/pkgconfig-foo' in stdout, got:\n{captured.out!r}"
        )
        # Sanity: ensure args propagated the prepend value.
        assert any(os.path.normpath(p) == str(target_dir) for p in (args.prepend_pkg_config_path or [])), (
            f"prepend value didn't reach args: {args.prepend_pkg_config_path!r}"
        )

    def test_pkg_config_provenance_label_returns_from_cli_for_no_match(self):
        """When the path isn't in the provenance dict, the label degrades to
        '(from CLI)'. Covers the lookup-miss branch directly so the
        user-visible 'from CLI' tag survives any future refactor of the
        verbose emission loop."""

        # Empty provenance — any prepend path falls back to CLI.
        assert _pkg_config_provenance_label("/abs/path", "prepend", {}) == "(from CLI)"

        # Provenance present but no match for this path — still CLI.
        prov = {"prepend-PKG-CONFIG-PATH": [("/different/path", "/conf/a.conf", 1)]}
        assert _pkg_config_provenance_label("/abs/path", "prepend", prov) == "(from CLI)"

        # Symmetric for append origin.
        prov = {"append-PKG-CONFIG-PATH": [("/different/path", "/conf/a.conf", 1)]}
        assert _pkg_config_provenance_label("/abs/path", "append", prov) == "(from CLI)"

        # Provenance contains a match — labels the conf-file:line.
        prov = {"prepend-PKG-CONFIG-PATH": [("/abs/path", "/conf/a.conf", 7)]}
        assert _pkg_config_provenance_label("/abs/path", "prepend", prov) == "(from /conf/a.conf:7)"

    def test_two_layered_conf_files_axis_wins_through_parseargs(self, monkeypatch, tmp_path, capsys):
        """End-to-end repro: project ``ct.conf`` and a higher-priority
        axis conf each set ``prepend-PKG-CONFIG-PATH``. After running
        through the real ``parseargs`` pipeline (configargparse +
        ``${CONF_DIR}`` expansion + ``_setup_pkg_config_overrides``),
        the axis-conf directory must land leftmost in
        ``PKG_CONFIG_PATH``. Before the fix, the project ct.conf's
        prepend was leftmost and silently shadowed the axis override,
        causing the wrong ABI flavor of a pinned ``.pc`` to be selected
        by downstream consumers.
        """
        conf_dir = tmp_path / "ct.conf.d"
        conf_dir.mkdir(parents=True)
        # Project ct.conf is lower-priority than the axis conf inside
        # the variant composition; its prepend should land second.
        base_pkgconfig = conf_dir / "pkgconfig-base"
        base_pkgconfig.mkdir()
        (tmp_path / "ct.conf").write_text(
            "variant = axisX\nprepend-PKG-CONFIG-PATH = ${CONF_DIR}/ct.conf.d/pkgconfig-base\n"
        )
        # Axis conf is higher priority — its prepend must win.
        axis_conf = conf_dir / "axisX.conf"
        axis_pkgconfig = conf_dir / "pkgconfig-axisX"
        axis_pkgconfig.mkdir()
        axis_conf.write_text("prepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig-axisX\n")

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root",
            lambda filename=None: str(tmp_path),
        )

        # --no-git-root keeps the test focused on the layered conf
        # prepends — without it, ct.conf.d/pkgconfig (auto-discovered)
        # would also land in PKG_CONFIG_PATH and dedup against
        # pkgconfig-base by realpath, muddying the assertion.
        argv = ["--variant=axisX", "--no-git-root"]
        with uth.DirectoryContext(str(tmp_path)):
            cap = apptools.create_parser("layered conf test", argv=argv)
            apptools.add_common_arguments(cap, argv=argv)
            with uth.ParserContext():
                ctx = BuildContext()
                args = apptools.parseargs(cap, argv, context=ctx)

        # The accumulator carries both prepends, in conf-hierarchy
        # order (project ct.conf first, axis conf second).
        prepends = [os.path.normpath(p) for p in (args.prepend_pkg_config_path or [])]
        assert str(base_pkgconfig) in prepends, f"project ct.conf's prepend didn't reach args: {prepends!r}"
        assert str(axis_pkgconfig) in prepends, f"axis conf's prepend didn't reach args: {prepends!r}"

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        axis_idx = dirs.index(str(axis_pkgconfig))
        base_idx = dirs.index(str(base_pkgconfig))
        assert axis_idx < base_idx, (
            f"Axis-conf prepend must land leftmost (winning) over project "
            f"ct.conf prepend; got axis@{axis_idx}, base@{base_idx}, "
            f"PKG_CONFIG_PATH={dirs!r}"
        )

    def test_two_layered_conf_files_axis_wins_append_through_parseargs(self, monkeypatch, tmp_path, capsys):
        """Append-group mirror of test_two_layered_conf_files_axis_wins_through_parseargs:
        project ``ct.conf`` and a higher-priority axis conf each set
        ``append-PKG-CONFIG-PATH``. After running through the real
        ``parseargs`` pipeline, the axis-conf directory must land leftmost
        *within the appended tail* of ``PKG_CONFIG_PATH`` — the documented
        reversal in ``_merged_pkg_config_path_entries`` (highest-priority
        source ends up leftmost in its group) applies symmetrically to
        append, not just prepend. Before this test, only single-element
        append lists were ever exercised end-to-end, so this within-append-
        group ordering had zero real-``parseargs`` coverage.
        """
        conf_dir = tmp_path / "ct.conf.d"
        conf_dir.mkdir(parents=True)
        # Project ct.conf is lower-priority than the axis conf inside
        # the variant composition; its append should land second (rightmost)
        # within the appended tail.
        base_pkgconfig = conf_dir / "pkgconfig-base"
        base_pkgconfig.mkdir()
        (tmp_path / "ct.conf").write_text(
            "variant = axisY\nappend-PKG-CONFIG-PATH = ${CONF_DIR}/ct.conf.d/pkgconfig-base\n"
        )
        # Axis conf is higher priority — its append must win within the group.
        axis_conf = conf_dir / "axisY.conf"
        axis_pkgconfig = conf_dir / "pkgconfig-axisY"
        axis_pkgconfig.mkdir()
        axis_conf.write_text("append-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig-axisY\n")

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root",
            lambda filename=None: str(tmp_path),
        )

        # --no-git-root keeps the test focused on the layered conf
        # appends — without it, ct.conf.d/pkgconfig (auto-discovered)
        # would also land in PKG_CONFIG_PATH, muddying the assertion.
        argv = ["--variant=axisY", "--no-git-root"]
        with uth.DirectoryContext(str(tmp_path)):
            cap = apptools.create_parser("layered conf append test", argv=argv)
            apptools.add_common_arguments(cap, argv=argv)
            with uth.ParserContext():
                ctx = BuildContext()
                args = apptools.parseargs(cap, argv, context=ctx)

        # The accumulator carries both appends, in conf-hierarchy
        # order (project ct.conf first, axis conf second).
        appends = [os.path.normpath(p) for p in (args.append_pkg_config_path or [])]
        assert str(base_pkgconfig) in appends, f"project ct.conf's append didn't reach args: {appends!r}"
        assert str(axis_pkgconfig) in appends, f"axis conf's append didn't reach args: {appends!r}"

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        axis_idx = dirs.index(str(axis_pkgconfig))
        base_idx = dirs.index(str(base_pkgconfig))
        assert axis_idx < base_idx, (
            f"Axis-conf append must land leftmost (winning) within the "
            f"appended tail over project ct.conf append; got "
            f"axis@{axis_idx}, base@{base_idx}, PKG_CONFIG_PATH={dirs!r}"
        )

    def test_gitroot_pkgconfig_dir_auto_discovered_through_real_parseargs(self, monkeypatch, tmp_path, pkgconfig_dir):
        """{gitroot}/ct.conf.d/pkgconfig on disk lands in
        ``os.environ["PKG_CONFIG_PATH"]`` after a REAL ``parseargs`` run —
        i.e. the gather-side auto-discovery in
        ``build_inputs._compute_pkg_config_path`` (exercised via
        ``apply_effects``' ``SetEnv``), not the legacy
        ``_setup_pkg_config_overrides`` writer the other tests in this
        class exercise directly. ``cwd`` is a subdirectory distinct from
        gitroot with no ``pkgconfig`` dir of its own, isolating the
        gitroot-candidate discovery specifically (as opposed to the
        cwd-candidate one)."""
        (tmp_path / "ct.conf").write_text("variant = myaxis\n")
        (tmp_path / "ct.conf.d" / "myaxis.conf").write_text("# empty axis, no CC/CXX needed\n")

        subdir = tmp_path / "subdir"
        subdir.mkdir()

        monkeypatch.chdir(subdir)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root",
            lambda filename=None: str(tmp_path),
        )

        argv = ["--variant=myaxis"]
        with uth.DirectoryContext(str(subdir)):
            cap = apptools.create_parser("gitroot auto-discovery test", argv=argv)
            apptools.add_common_arguments(cap, argv=argv)
            with uth.ParserContext():
                ctx = BuildContext()
                apptools.parseargs(cap, argv, context=ctx)

        entries = os.environ.get("PKG_CONFIG_PATH", "").split(os.pathsep)
        assert str(pkgconfig_dir) in entries, (
            f"gitroot ct.conf.d/pkgconfig was not auto-discovered into PKG_CONFIG_PATH: {entries!r}"
        )

    def test_gitroot_pkgconfig_dir_deduplicated_when_cwd_equals_gitroot(self, monkeypatch, tmp_path, pkgconfig_dir):
        """When ``cwd`` IS the gitroot, the on-disk ``ct.conf.d/pkgconfig``
        dir is independently discovered by both the cwd-candidate and the
        gitroot-candidate probes in
        ``build_inputs._compute_pkg_config_path``; the
        ``repo_pkgconfig not in cwd_candidates`` guard must keep it out of
        the final ``PKG_CONFIG_PATH`` twice."""
        (tmp_path / "ct.conf").write_text("variant = myaxis\n")
        (tmp_path / "ct.conf.d" / "myaxis.conf").write_text("# empty axis, no CC/CXX needed\n")

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root",
            lambda filename=None: str(tmp_path),
        )

        argv = ["--variant=myaxis"]
        with uth.DirectoryContext(str(tmp_path)):
            cap = apptools.create_parser("cwd==gitroot dedup test", argv=argv)
            apptools.add_common_arguments(cap, argv=argv)
            with uth.ParserContext():
                ctx = BuildContext()
                apptools.parseargs(cap, argv, context=ctx)

        entries = os.environ.get("PKG_CONFIG_PATH", "").split(os.pathsep)
        assert entries.count(str(pkgconfig_dir)) == 1, (
            f"cwd==gitroot pkgconfig dir must appear exactly once in PKG_CONFIG_PATH, "
            f"got {entries.count(str(pkgconfig_dir))}: {entries!r}"
        )


@pytest.mark.usefixtures("parsers_reset")
class TestAppendFlagsAccumulateAcrossConfHierarchy:
    """Regression tests for the multi-conf ``append-*`` / ``prepend-*`` bug.

    Stock configargparse processes each conf file independently and discards
    any ``action='append'`` key already injected by a higher-priority conf,
    so only the highest-priority conf's value reaches ``args.append_cxxflags``.
    That broke ``--variant=gcc,release,extras`` style compositions: gcc.conf's
    and release.conf's ``append-CXXFLAGS`` values were silently dropped and
    only ``extras.conf``'s tokens survived. ``_ComposingArgumentParser`` +
    ``_AccumulatingConfigFileParser`` in apptools fix this by merging the
    full conf hierarchy into a single stream and accumulating duplicate
    append-/prepend- keys into a list.
    """

    def _setup_three_axis_conf_tree(self, repo_root):
        """Create gcc.conf, release.conf, extras.conf with distinct
        ``append-CXXFLAGS`` markers and a project ct.conf that names
        ``extras`` as a known axis (so the resolver treats the third
        token as an axis rather than an unknown).
        """
        conf_d = os.path.join(repo_root, "ct.conf.d")
        os.makedirs(conf_d, exist_ok=True)
        with open(os.path.join(repo_root, "ct.conf"), "w") as fh:
            fh.write("variant = gcc.release.extras\n")
            fh.write("variant-canonical-order = gcc, release, extras\n")
            fh.write("exemarkers = [main]\n")
            fh.write("testmarkers = unit_test.hpp\n")
        with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
            fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
            fh.write("append-CXXFLAGS = -DFROM_GCC_AXIS\n")
            fh.write("append-CFLAGS   = -DFROM_GCC_AXIS\n")
        with open(os.path.join(conf_d, "release.conf"), "w") as fh:
            fh.write("append-CXXFLAGS = -DFROM_RELEASE_AXIS\n")
            fh.write("append-CFLAGS   = -DFROM_RELEASE_AXIS\n")
        with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
            fh.write("append-CXXFLAGS = -DFROM_EXTRAS_AXIS\n")
            fh.write("append-CFLAGS   = -DFROM_EXTRAS_AXIS\n")

    def test_three_axis_append_cxxflags_all_present(self):
        """All three axis confs' ``append-CXXFLAGS`` values reach
        ``args.append_cxxflags`` (and therefore the final ``get_build_state(args).cxxflags``).
        """

        with uth.TempDirContextNoChange() as repo_root:
            self._setup_three_axis_conf_tree(repo_root)
            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in get_build_state(args).cxxflags, (
                    f"{marker} missing from cxxflags={get_build_state(args).cxxflags!r}. "
                    f"Multi-conf append-CXXFLAGS composition is broken — only "
                    f"the highest-priority conf's value survived. "
                    f"args.append_cxxflags={args.append_cxxflags!r}"
                )
                assert marker in get_build_state(args).cflags, (
                    f"{marker} missing from cflags={get_build_state(args).cflags!r}; "
                    f"append-CFLAGS suffers the same bug as append-CXXFLAGS."
                )

    def test_cli_append_combines_with_conf_append(self):
        """A ``--append-CXXFLAGS`` token on the CLI accumulates with the conf
        file's ``append-CXXFLAGS`` rather than replacing it.

        With three conf files contributing append values and one CLI value,
        all four should reach the final ``get_build_state(args).cxxflags``. (Stock
        configargparse drops the conf-file values when the CLI flag is
        present too.)
        """

        with uth.TempDirContextNoChange() as repo_root:
            self._setup_three_axis_conf_tree(repo_root)
            argv = [
                "--variant=gcc,release,extras",
                "--append-CXXFLAGS=-DFROM_CLI",
                "--no-git-root",
            ]
            args = _parseargs_for_variant(repo_root, argv)

            # The CLI value is always honored. The three conf values are
            # the regression target: at least one of them MUST survive
            # alongside the CLI value (this is the user's reported bug).
            assert "-DFROM_CLI" in get_build_state(args).cxxflags, get_build_state(args).cxxflags
            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in get_build_state(args).cxxflags, (
                    f"CLI --append-CXXFLAGS swallowed {marker} from the conf "
                    f"hierarchy. CXXFLAGS={get_build_state(args).cxxflags!r}, "
                    f"append_cxxflags={args.append_cxxflags!r}"
                )

    def test_three_axis_append_ldflags_all_present(self):
        """``append-LDFLAGS`` is registered by ``add_link_arguments`` (not
        ``add_common_arguments``) but uses the same ``_add_xxpend_argument``
        machinery. The fix must cover it too. Many bundled axis confs
        contribute LDFLAGS (gcc.conf ``-Werror -Xlinker --build-id``,
        gold.conf ``-fuse-ld=gold``, pgo-gen.conf ``-fprofile-generate``)
        so this is the most exercised flag slot in practice.
        """

        with _temp_repo_with_ct_conf("gcc.release.extras", "gcc, release, extras") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("append-LDFLAGS = -Wl,--build-id\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write("append-LDFLAGS = -Wl,-O1\n")
            with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
                fh.write("append-LDFLAGS = -Wl,--as-needed\n")

            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv, add_link=True)

            for marker in ("-Wl,--build-id", "-Wl,-O1", "-Wl,--as-needed"):
                assert marker in get_build_state(args).ldflags, (
                    f"{marker} missing from LDFLAGS={get_build_state(args).ldflags!r}; "
                    f"append-LDFLAGS did not accumulate across the hierarchy. "
                    f"args.append_ldflags={args.append_ldflags!r}"
                )

    def test_conf_list_form_syntax_still_works(self):
        """A conf file using configargparse's native list-form syntax
        (``append-CXXFLAGS = [-X, -Y]``) must still work and compose with
        scalar-form values from other confs. The fix's
        ``_AccumulatingConfigFileParser`` overrides ``parse`` and must
        preserve the list parsing path (it inherited from
        ``DefaultConfigFileParser``).
        """

        with _temp_repo_with_ct_conf("gcc.release", "gcc, release") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                # Scalar form
                fh.write("append-CXXFLAGS = -DSCALAR_FROM_GCC\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                # List form — two values in one assignment
                fh.write("append-CXXFLAGS = [-DLIST_VAL1, -DLIST_VAL2]\n")

            argv = ["--variant=gcc,release", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            for marker in ("-DSCALAR_FROM_GCC", "-DLIST_VAL1", "-DLIST_VAL2"):
                assert marker in get_build_state(args).cxxflags, (
                    f"{marker} missing from CXXFLAGS={get_build_state(args).cxxflags!r}; "
                    f"list-form syntax may have broken the accumulating "
                    f"parser. args.append_cxxflags={args.append_cxxflags!r}"
                )

    def test_append_order_lower_priority_before_higher(self):
        """Lower-priority axis values must appear BEFORE higher-priority axis
        values in the merged flag string, so that compilers' "last occurrence
        wins" rule resolves conflicting flags (e.g. ``-O0`` vs ``-O3``) in
        favor of the higher-priority axis. With the conf-file hierarchy
        ``gcc < release < extras``, an ``-O0`` in gcc.conf must end up to
        the LEFT of an ``-O3`` in release.conf in get_build_state(args).cxxflags.
        """

        with _temp_repo_with_ct_conf("gcc.release.extras", "gcc, release, extras") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("append-CXXFLAGS = -O0\n")  # lowest priority
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write("append-CXXFLAGS = -O3\n")  # mid priority
            with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
                fh.write("append-CXXFLAGS = -Os\n")  # highest priority

            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            cxx = get_build_state(args).cxxflags
            o0 = cxx.find("-O0")
            o3 = cxx.find("-O3")
            os_ = cxx.find("-Os")
            assert -1 not in (o0, o3, os_), (
                f"Missing one or more markers in CXXFLAGS={cxx!r}: -O0@{o0}, -O3@{o3}, -Os@{os_}"
            )
            assert o0 < o3 < os_, (
                f"Order broken: expected -O0 < -O3 < -Os in CXXFLAGS, "
                f"got -O0@{o0}, -O3@{o3}, -Os@{os_}. The compiler honors the "
                f"LAST occurrence of conflicting -O flags, so the higher-"
                f"priority axis (extras) must come after lower-priority ones "
                f"(gcc, release). CXXFLAGS={cxx!r}"
            )

    def test_three_axis_append_include_all_present(self):
        """``append-include`` follows the same code path as ``append-CXXFLAGS``
        but registers a different option string (``--append-INCLUDE``). The
        fix must handle every ``--append-*`` / ``--prepend-*`` option that
        ``_add_xxpend_arguments`` registers, not just the FLAGS family.
        ``args.append_include`` reaches the computed include paths via
        gather's ``_include_paths_with_gitroots``.
        """

        with _temp_repo_with_ct_conf("gcc.release.extras", "gcc, release, extras") as (repo_root, conf_d):
            inc_a = os.path.join(repo_root, "inc_gcc")
            inc_b = os.path.join(repo_root, "inc_release")
            inc_c = os.path.join(repo_root, "inc_extras")
            for d in (inc_a, inc_b, inc_c):
                os.makedirs(d, exist_ok=True)
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write(f"append-INCLUDE ={inc_a}\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write(f"append-INCLUDE ={inc_b}\n")
            with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
                fh.write(f"append-INCLUDE ={inc_c}\n")

            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            # args.INCLUDE keeps the un-merged raw string (the xxpend
            # merge happens inside gather_inputs), so the observable is the
            # -I tokens the core folded into the compile slots.
            for inc_dir in (inc_a, inc_b, inc_c):
                assert inc_dir in get_build_state(args).flags.cpp, (
                    f"{inc_dir} missing from get_build_state(args).flags.cpp={get_build_state(args).flags.cpp!r}; "
                    f"append-include did not accumulate across the hierarchy. "
                    f"args.append_include={args.append_include!r}"
                )

    def test_cli_space_separated_append_combines_with_conf(self):
        """The CLI extractor must handle ``--append-CXXFLAGS <value>`` (space
        form) as well as ``--append-CXXFLAGS=<value>`` (equals form). Both
        forms accept exactly one value (the registered action has no
        ``nargs``), so the next argv token is consumed as the value.
        """

        with uth.TempDirContextNoChange() as repo_root:
            self._setup_three_axis_conf_tree(repo_root)
            argv = [
                "--variant=gcc,release,extras",
                "--append-CXXFLAGS",  # space form, not '='
                "-DFROM_CLI_SPACE",
                "--no-git-root",
            ]
            args = _parseargs_for_variant(repo_root, argv)

            assert "-DFROM_CLI_SPACE" in get_build_state(args).cxxflags, get_build_state(args).cxxflags
            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in get_build_state(args).cxxflags, (
                    f"Space-form CLI --append-CXXFLAGS swallowed {marker}. "
                    f"cxxflags={get_build_state(args).cxxflags!r}, "
                    f"args.append_cxxflags={args.append_cxxflags!r}"
                )

    def test_three_axis_prepend_cxxflags_all_present(self):
        """``prepend-CXXFLAGS`` follows the same accumulation rule as
        ``append-CXXFLAGS``: when three axis confs each ``prepend-CXXFLAGS``,
        all three values reach ``args.prepend_cxxflags`` and the final
        ``get_build_state(args).cxxflags``. ``prepend-*`` uses ``action='append'`` under the
        hood (same as ``append-*``), so the underlying configargparse bug
        affects both — and the fix must cover both.
        """

        with _temp_repo_with_ct_conf("gcc.release.extras", "gcc, release, extras") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("prepend-CXXFLAGS = -DPREPEND_GCC\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write("prepend-CXXFLAGS = -DPREPEND_RELEASE\n")
            with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
                fh.write("prepend-CXXFLAGS = -DPREPEND_EXTRAS\n")

            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            for marker in ("-DPREPEND_GCC", "-DPREPEND_RELEASE", "-DPREPEND_EXTRAS"):
                assert marker in get_build_state(args).cxxflags, (
                    f"{marker} missing from cxxflags={get_build_state(args).cxxflags!r}. "
                    f"prepend-CXXFLAGS values are not accumulating across the "
                    f"conf hierarchy. args.prepend_cxxflags={args.prepend_cxxflags!r}"
                )

    def test_append_prepend_pkg_config_cli_flags_registered(self):
        """``--append-PKG-CONFIG`` and ``--prepend-PKG-CONFIG`` must exist as
        CLI options, mirroring the ``--append-PKG-CONFIG-PATH`` /
        ``--prepend-PKG-CONFIG-PATH`` pair already registered for the
        environment-variable path. Without them, ``pkg-config = foo`` keys
        in conf files are last-writer-wins (every other ``append-*`` /
        ``prepend-*`` key accumulates across the hierarchy), and there is
        no CLI way to add to the package list without also clobbering any
        conf-file values via configargparse's ``already_on_command_line``
        suppression.
        """
        cap = apptools.create_parser("registration test", argv=[], include_config=False)
        apptools.add_common_arguments(cap, argv=[])
        opts = {opt for a in cap._actions for opt in a.option_strings}
        assert "--append-PKG-CONFIG" in opts, (
            f"--append-PKG-CONFIG not registered; feature parity gap with "
            f"--append-PKG-CONFIG-PATH. Registered options containing 'PKG-CONFIG': "
            f"{sorted(o for o in opts if 'PKG-CONFIG' in o)!r}"
        )
        assert "--prepend-PKG-CONFIG" in opts, (
            f"--prepend-PKG-CONFIG not registered; feature parity gap with "
            f"--prepend-PKG-CONFIG-PATH. Registered options containing 'PKG-CONFIG': "
            f"{sorted(o for o in opts if 'PKG-CONFIG' in o)!r}"
        )

    @pytest.mark.filterwarnings("ignore:.*'(pkg_gcc_axis|pkg_release_axis|pkg_extras_axis)'.*:UserWarning")
    def test_three_axis_append_pkg_config_all_present(self):
        """``append-PKG-CONFIG = <pkg>`` in three layered axis confs must
        accumulate into ``args.pkg_config`` end-to-end — same accumulation
        rule that ``append-CXXFLAGS`` / ``append-INCLUDE`` already get.

        Without the ``append-*`` accumulator key, a bare ``pkg-config = foo``
        in conf files is last-writer-wins, so per-axis pkg-config additions
        (think: ``release.conf`` adding a runtime tracing lib, ``debug.conf``
        adding a leak checker) get silently dropped.

        The suppression is scoped to the three names this test deliberately
        invents. It used to read ``pkg-config package .* not found``, which
        also swallowed a warning naming the *joined* value
        ``'pkg_gcc_axis pkg_release_axis pkg_extras_axis'`` — the fingerprint
        of the conf whitespace collapse. Keep the names quoted in the pattern:
        the quotes are what stop a joined value from matching.
        """
        with _temp_repo_with_ct_conf("gcc.release.extras", "gcc, release, extras") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("append-PKG-CONFIG = pkg_gcc_axis\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write("append-PKG-CONFIG = pkg_release_axis\n")
            with open(os.path.join(conf_d, "extras.conf"), "w") as fh:
                fh.write("append-PKG-CONFIG = pkg_extras_axis\n")

            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            # The composed list is produced by gather from the raw
            # attrs; args.pkg_config keeps the un-merged conf shape.
            from compiletools.build_inputs import _merged_pkg_config_specs

            composed = _merged_pkg_config_specs(args)
            for pkg in ("pkg_gcc_axis", "pkg_release_axis", "pkg_extras_axis"):
                assert pkg in composed, (
                    f"{pkg!r} missing from composed pkg-config list={composed!r}; "
                    f"append-PKG-CONFIG did not accumulate across the hierarchy. "
                    f"args.append_pkg_config={getattr(args, 'append_pkg_config', '<unset>')!r}"
                )

    @pytest.mark.filterwarnings("ignore:.*'(pkg_from_gcc|pkg_from_release|pkg_from_cli)'.*:UserWarning")
    def test_cli_append_pkg_config_combines_with_conf(self):
        """A CLI ``--append-PKG-CONFIG=<pkg>`` token must combine with conf-file
        ``append-PKG-CONFIG = ...`` values rather than suppressing them via
        configargparse's ``already_on_command_line`` check. Mirrors the
        existing ``test_cli_append_combines_with_conf_append`` regression
        for the CXXFLAGS slot.

        Suppression narrowed from ``pkg-config package .* not found`` to the
        three invented names, for the reason given in
        ``test_three_axis_append_pkg_config_all_present``.
        """
        with _temp_repo_with_ct_conf("gcc.release", "gcc, release") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("append-PKG-CONFIG = pkg_from_gcc\n")
            with open(os.path.join(conf_d, "release.conf"), "w") as fh:
                fh.write("append-PKG-CONFIG = pkg_from_release\n")

            argv = [
                "--variant=gcc,release",
                "--append-PKG-CONFIG=pkg_from_cli",
                "--no-git-root",
            ]
            args = _parseargs_for_variant(repo_root, argv)

            # Composed at gather from the raw attrs.
            from compiletools.build_inputs import _merged_pkg_config_specs

            composed = _merged_pkg_config_specs(args)
            assert "pkg_from_cli" in composed, composed
            for pkg in ("pkg_from_gcc", "pkg_from_release"):
                assert pkg in composed, (
                    f"CLI --append-PKG-CONFIG swallowed {pkg!r} from the conf "
                    f"hierarchy. composed={composed!r}, "
                    f"args.append_pkg_config={getattr(args, 'append_pkg_config', '<unset>')!r}"
                )

    @pytest.mark.filterwarnings("ignore:.*'(pkg_base|pkg_prepended|pkg_appended)'.*:UserWarning")
    def test_prepend_pkg_config_lands_before_existing(self):
        """``--prepend-PKG-CONFIG=foo`` must land ahead of any existing
        ``--pkg-config`` / ``append-PKG-CONFIG`` entries in
        ``args.pkg_config``. Order matters for the rare cases where two
        ``.pc`` files declare the same library and `pkg-config` resolves
        them in argument order — and the symmetry with the CXXFLAGS slot
        (prepend = leftmost, append = rightmost) is the contract every
        ``_add_xxpend_argument`` consumer relies on.
        """
        with _temp_repo_with_ct_conf("gcc", "gcc") as (repo_root, conf_d):
            with open(os.path.join(conf_d, "gcc.conf"), "w") as fh:
                fh.write("CC = gcc\nCXX = g++\nLD = g++\n")
                fh.write("append-PKG-CONFIG = pkg_appended\n")

            argv = [
                "--variant=gcc",
                "--pkg-config=pkg_base",
                "--prepend-PKG-CONFIG=pkg_prepended",
                "--no-git-root",
            ]
            args = _parseargs_for_variant(repo_root, argv)

            # Composed at gather from the raw attrs.
            from compiletools.build_inputs import _merged_pkg_config_specs

            pkgs = list(_merged_pkg_config_specs(args))
            assert "pkg_prepended" in pkgs and "pkg_appended" in pkgs and "pkg_base" in pkgs, pkgs
            assert pkgs.index("pkg_prepended") < pkgs.index("pkg_base"), (
                f"prepend should land before base --pkg-config: {pkgs!r}"
            )
            assert pkgs.index("pkg_base") < pkgs.index("pkg_appended"), (
                f"append should land after base --pkg-config: {pkgs!r}"
            )


@pytest.mark.usefixtures("parsers_reset")
class TestPkgConfigConfValueSplitting:
    """Regression coverage for the conf-surface pkg-config whitespace collapse.

    Whitespace is not a value separator for list-valued conf keys: a
    ``pkg-config = a b c`` line arrives at ``args.pkg_config`` as the single
    element ``'a b c'``. That shape survived review because the batch fast
    path in ``_batch_pkg_config`` hands the element to pkg-config as one argv
    token and pkg-config splits it itself. The per-package fallback — taken
    whenever *any* listed package is missing — instead queries a package
    literally named ``'a b c'``, so every co-listed present package loses its
    cflags and libs and the warning names a package the user never wrote.

    The naive repair (``entry.split()``) is wrong: pkg-config specs carry
    whitespace-bearing version constraints, so ``'zlib >= 1.2'`` would become
    three bogus package names and the version floor would be dropped on both
    paths. ``tokenize_pkg_config_specs`` is the spec-aware split these tests
    exercise end-to-end through ``parseargs``.

    Every test drives the real conf hierarchy and the real ``pkg-config``
    binary against the in-repo ``examples-features/pkgs/*.pc`` fixtures
    (``conditional`` 1.0.0, ``nested`` 1.0.0, ``modified`` 2.0.0), so nothing
    depends on which packages the host happens to have installed.
    """

    MISSING = "ct_bogus_absent_pkg"

    @staticmethod
    def _categories(warning_texts):
        """Strip the supplementary native stderr from each warning.

        Core owns three stable message forms and appends pkg-config's own
        stderr after a ``': '`` separator. Only the owned part is a contract;
        the native prose varies between pkg-config and pkgconf and across
        locales, so assertions read this rather than the raw text.
        """
        return [text.split(": ", 1)[0] for text in warning_texts]

    # Cross-temp-repo CPPFLAGS equality assertions must strip the
    # auto-injected prefix-map token via uth.without_prefix_map — each
    # _parseargs_with_pkg_config_conf run builds its own temp repo, so the
    # token embeds a different gitroot per run by design.
    def test_whitespace_and_list_literal_conf_forms_yield_the_same_packages(self, pkgconfig_env):
        """``pkg-config = conditional nested`` and ``pkg-config =
        [conditional, nested]`` must contribute identical flags.

        The list literal is the only form that splits at the configargparse
        layer — whitespace is not a value separator, so the first form arrives
        as the single element ``'conditional nested'``. Users write both, and
        the batch path happens to make them agree while the fallback path does
        not, so the equivalence is asserted on the flags rather than on the
        raw namespace shape.

        This is the whole-list form of the collapse; the missing-package tests
        below are the per-package form.
        """
        whitespace = _parseargs_with_pkg_config_conf(f"pkg-config = conditional nested {self.MISSING}")
        listliteral = _parseargs_with_pkg_config_conf(f"pkg-config = [conditional, nested, {self.MISSING}]")

        assert uth.without_prefix_map(get_build_state(whitespace.args).cppflags) == uth.without_prefix_map(
            get_build_state(listliteral.args).cppflags
        ), (
            f"whitespace and list-literal conf forms produced different CPPFLAGS.\n"
            f"  whitespace  args.pkg_config={whitespace.args.pkg_config!r}\n"
            f"              CPPFLAGS={get_build_state(whitespace.args).cppflags!r}\n"
            f"  listliteral args.pkg_config={listliteral.args.pkg_config!r}\n"
            f"              CPPFLAGS={get_build_state(listliteral.args).cppflags!r}"
        )
        assert get_build_state(whitespace.args).ldflags == get_build_state(listliteral.args).ldflags, (
            f"whitespace and list-literal conf forms produced different LDFLAGS.\n"
            f"  whitespace  LDFLAGS={get_build_state(whitespace.args).ldflags!r}\n"
            f"  listliteral LDFLAGS={get_build_state(listliteral.args).ldflags!r}"
        )
        for marker in ("-DTEST_PKG_ENABLED", "-DTEST_PKG1_ENABLED"):
            assert marker in get_build_state(whitespace.args).cppflags, (
                f"{marker} missing from the whitespace form: {get_build_state(whitespace.args).cppflags!r}"
            )

    @pytest.mark.parametrize(
        ("ct_conf_line", "axis_conf_line", "expected"),
        [
            ("pkg-config = conditional nested", "", ["conditional", "nested"]),
            ("pkg-config = conditional, nested", "", ["conditional", "nested"]),
            ("pkg-config = [conditional, nested]", "", ["conditional", "nested"]),
            ("pkg-config = conditional >= 1.0.0, nested", "", ["conditional >= 1.0.0", "nested"]),
            ("", "append-PKG-CONFIG = conditional nested", ["conditional", "nested"]),
            ("", "prepend-PKG-CONFIG = conditional nested", ["conditional", "nested"]),
        ],
    )
    def test_namespace_carries_one_element_per_spec_after_parseargs(
        self, pkgconfig_env, ct_conf_line, axis_conf_line, expected
    ):
        """The composed pkg-config list holds one element per specification.

        The core never mutates the namespace — the raw attrs keep the conf
        shape and ``build_inputs._merged_pkg_config_specs`` owns the
        tokenize+merge at the point of consumption. The shape contract
        therefore holds on the composed list gather produces from the
        post-parseargs namespace, not on the namespace attrs themselves.

        A version constraint keeps its internal space: ``conditional >=
        1.0.0`` is one specification, not three. Constraint *spelling* is
        deliberately not asserted here — that boundary belongs to
        ``apptools_pkgconfig``'s classifier and is pinned in
        test_apptools_pkgconfig.py. This test is about list shape only.
        """
        from compiletools.build_inputs import _merged_pkg_config_specs

        result = _parseargs_with_pkg_config_conf(ct_conf_line, axis_conf_line=axis_conf_line)

        composed = _merged_pkg_config_specs(result.args)
        assert composed == expected, (
            f"composed pkg-config list was not one element per specification: "
            f"got {composed!r}, expected {expected!r} "
            f"(raw args.pkg_config={result.args.pkg_config!r})"
        )

    def test_comma_separated_bare_conf_value_is_equivalent(self, pkgconfig_env):
        """``pkg-config = conditional, nested`` is the third of the three
        equivalent conf forms README.ct-config.rst documents.

        The comma reaches the tokenizer as ordinary spec text rather than as
        the configargparse list separator, because a bare value is never
        parsed as a list literal. Whitespace, comma, and bracket forms are
        documented as interchangeable for this key alone, so all three are
        pinned here against the docs going stale in either direction.
        """
        comma = _parseargs_with_pkg_config_conf(f"pkg-config = conditional, nested, {self.MISSING}")
        whitespace = _parseargs_with_pkg_config_conf(f"pkg-config = conditional nested {self.MISSING}")

        assert uth.without_prefix_map(get_build_state(comma.args).cppflags) == uth.without_prefix_map(
            get_build_state(whitespace.args).cppflags
        ), (
            f"comma and whitespace conf forms produced different CPPFLAGS.\n"
            f"  comma      args.pkg_config={comma.args.pkg_config!r}\n"
            f"             CPPFLAGS={get_build_state(comma.args).cppflags!r}\n"
            f"  whitespace CPPFLAGS={get_build_state(whitespace.args).cppflags!r}"
        )
        assert get_build_state(comma.args).ldflags == get_build_state(whitespace.args).ldflags, (
            f"comma and whitespace conf forms produced different LDFLAGS: "
            f"{get_build_state(comma.args).ldflags!r} vs {get_build_state(whitespace.args).ldflags!r}"
        )
        assert not [c for c in self._categories(comma.warnings) if "," in c], (
            f"a comma survived into a queried package name: {comma.warnings!r}"
        )

    def test_constraint_and_comma_combine_in_a_bare_conf_value(self, pkgconfig_env):
        """``pkg-config = conditional >= 1.0.0, nested`` — the form
        README.ct-config.rst gives as equivalent to the bracket spelling.

        What this pins is the space inside the constraint: a tokenizer that
        split on whitespace turns ``conditional >= 1.0.0`` into three bogus
        package names, and both markers disappear.

        It does not pin the comma, despite the name. With the version
        operand present, collapsing the comma to a space leaves the scanner
        with the same four tokens and the same two specs, so the assertions
        below pass either way. The comma only becomes load-bearing when the
        operand is missing and a dangling operator can reach across it —
        that case is
        :meth:`test_dangling_operator_does_not_reach_across_a_comma`.
        """
        bare = _parseargs_with_pkg_config_conf("pkg-config = conditional >= 1.0.0, nested")
        bracket = _parseargs_with_pkg_config_conf("pkg-config = [conditional >= 1.0.0, nested]")

        for marker in ("-DTEST_PKG_ENABLED", "-DTEST_PKG1_ENABLED"):
            assert marker in get_build_state(bare.args).cppflags, (
                f"{marker} missing from the bare constraint+comma form. "
                f"args.pkg_config={bare.args.pkg_config!r}, CPPFLAGS={get_build_state(bare.args).cppflags!r}"
            )
        assert uth.without_prefix_map(get_build_state(bare.args).cppflags) == uth.without_prefix_map(
            get_build_state(bracket.args).cppflags
        ), (
            f"bare and bracket forms disagree: {get_build_state(bare.args).cppflags!r} vs {get_build_state(bracket.args).cppflags!r}"
        )
        assert not bare.warnings, f"a documented equivalent form must warn about nothing, got {bare.warnings!r}"

    def test_missing_package_does_not_discard_co_listed_present_package(self, pkgconfig_env):
        """THE defect. A whitespace-joined value naming one present and one
        absent package must still contribute the present package's cflags and
        libs.

        The absent package forces ``_batch_pkg_config`` onto its per-package
        fallback, which is the only path that ever sees the collapsed element.
        """
        result = _parseargs_with_pkg_config_conf(f"pkg-config = conditional {self.MISSING}")

        assert "-DTEST_PKG_ENABLED" in get_build_state(result.args).cppflags, (
            f"conditional's cflags were discarded because {self.MISSING!r} is absent. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert "-ltestpkg" in get_build_state(result.args).ldflags, (
            f"conditional's libs were discarded because {self.MISSING!r} is absent. "
            f"args.pkg_config={result.args.pkg_config!r}, LDFLAGS={get_build_state(result.args).ldflags!r}"
        )

    def test_missing_package_warning_names_only_the_missing_package(self, pkgconfig_env):
        """The warning must name the absent package alone, never the joined
        conf value.

        Pre-fix this warned about a package called ``'conditional
        ct_bogus_absent_pkg'`` — a name that appears in no ``.pc`` file and in
        no conf key, which sends the reader looking for a package that does
        not exist instead of the one that does.
        """
        result = _parseargs_with_pkg_config_conf(f"pkg-config = conditional {self.MISSING}")

        assert result.warnings, "expected a warning naming the absent package, got none"
        joined = [w for w in result.warnings if f"conditional {self.MISSING}" in w]
        assert not joined, f"warning names the collapsed conf value rather than a package: {joined!r}"
        assert any(self.MISSING in w for w in result.warnings), (
            f"no warning names the absent package {self.MISSING!r}: {result.warnings!r}"
        )
        assert not [w for w in result.warnings if "conditional" in w and self.MISSING not in w], (
            f"warned about the present package 'conditional': {result.warnings!r}"
        )

    def test_satisfied_version_floor_resolves_on_conf_surface(self, pkgconfig_env):
        """``pkg-config = conditional >= 1.0.0`` (fixture is 1.0.0) must
        resolve and contribute flags, with the operator and version never
        queried as package names.

        Unlike its siblings this passes on the unfixed code — the batch fast
        path hands the whole element to pkg-config, which parses the
        constraint itself. It is here as the guard against the naive repair:
        an ``entry.split()`` fix turns this green case red by looking up
        ``'>='`` and ``'1.0.0'`` as packages and dropping the floor, trading a
        fallback-only bug for an always-on one.
        """
        result = _parseargs_with_pkg_config_conf("pkg-config = conditional >= 1.0.0")

        assert "-DTEST_PKG_ENABLED" in get_build_state(result.args).cppflags, (
            f"satisfied version floor did not resolve: CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert "-ltestpkg" in get_build_state(result.args).ldflags, (
            f"satisfied version floor did not resolve: LDFLAGS={get_build_state(result.args).ldflags!r}"
        )
        assert not result.warnings, f"a satisfied version floor must warn about nothing, got {result.warnings!r}"

    def test_unsatisfied_version_floor_reported_as_version_failure(self, pkgconfig_env):
        """``pkg-config = conditional >= 2.0`` against the 1.0.0 fixture must
        be reported as an unsatisfied requirement, not as a missing package.

        The distinction has to survive or the user goes hunting for a ``.pc``
        file that is sitting right there with the wrong version. It is
        asserted against compiletools' own message category rather than
        pkg-config's prose, which differs between pkg-config and pkgconf.
        """
        result = _parseargs_with_pkg_config_conf("pkg-config = conditional >= 2.0")

        categories = self._categories(result.warnings)
        assert result.warnings, "expected a warning for the unsatisfied version floor, got none"
        assert "pkg-config version requirement 'conditional >= 2.0' not satisfied" in categories, (
            f"unsatisfied version floor was not reported as a version failure: {categories!r}"
        )
        assert not [c for c in categories if "not found" in c], (
            f"unsatisfied version floor misreported as a missing package: {categories!r}"
        )

    def test_version_constrained_spec_survives_fallback_alongside_missing_package(self, pkgconfig_env):
        """A version-constrained spec co-listed with an absent package must
        still resolve.

        This is the intersection the two halves of the fix can each pass
        alone and still fail together: the absent package forces the
        per-package fallback, and the fallback is where the constraint has to
        stay attached to its package name.
        """
        result = _parseargs_with_pkg_config_conf(f"pkg-config = conditional >= 1.0.0 {self.MISSING}")

        assert "-DTEST_PKG_ENABLED" in get_build_state(result.args).cppflags, (
            f"version-constrained spec lost its flags on the fallback path. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert any(self.MISSING in w for w in result.warnings), (
            f"no warning names the absent package {self.MISSING!r}: {result.warnings!r}"
        )
        assert not any("1.0.0" in w and "conditional" not in w for w in result.warnings), (
            f"the version operand was queried as a package name: {result.warnings!r}"
        )

    @pytest.mark.parametrize("spec", [">= 1.2", ">=1.2", "conditional >=", "conditional >"])
    def test_operator_without_an_operand_reported_as_malformed(self, pkgconfig_env, spec):
        """A comparison with no package before it, or no version after it, is
        a malformed specification and must be named as one.

        Passing these through reaches pkg-config, which answers ``Package >=
        was not found in the pkg-config search path`` — inventing a package
        name out of an operator, which is the failure class this whole change
        exists to remove. The classification happens before any subprocess.
        """
        result = _parseargs_with_pkg_config_conf(f"pkg-config = {spec}")

        categories = self._categories(result.warnings)
        assert f"pkg-config malformed package specification {spec!r}" in categories, (
            f"{spec!r} was not classified as malformed: {categories!r}"
        )

    def test_separate_conf_entries_do_not_bleed_across_the_element_boundary(self, pkgconfig_env):
        """A trailing operator at the end of one conf entry must not absorb
        the first package of the next entry.

        Two conf entries for this key arrive as two list elements. Tokenizing
        the concatenation rather than each element lets ``conditional >=``
        swallow ``nested`` into a version comparison against the literal
        string ``nested`` — which changes the resulting flags with no warning
        at all, the worst shape this defect class takes. ``nested`` must
        resolve on its own and the dangling operator must be named malformed.
        """
        result = _parseargs_with_pkg_config_conf(
            "pkg-config = conditional >=",
            axis_conf_line="append-PKG-CONFIG = nested",
        )

        categories = self._categories(result.warnings)
        assert "-DTEST_PKG1_ENABLED" in get_build_state(result.args).cppflags, (
            f"'nested' was absorbed by the preceding entry's dangling operator. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert "pkg-config malformed package specification 'conditional >='" in categories, (
            f"the dangling operator was not reported as malformed: {categories!r}"
        )
        assert not [c for c in categories if "nested" in c], (
            f"'nested' was queried as part of another spec: {categories!r}"
        )

    def test_dangling_operator_does_not_reach_across_a_comma(self, pkgconfig_env):
        """A comma ends a spec, so a trailing operator cannot take the next
        package as its version operand.

        This is the element-boundary case one line further in: both are
        documented separators, but only the element boundary was guarded.
        It is the worst-behaved member of the family — pkg-config compares
        ``conditional``'s version against the literal string ``nested`` and
        answers success, so the build loses ``nested``'s cflags and libs
        with no diagnostic of any kind. The dangling operator must instead
        be named malformed and ``nested`` must resolve on its own.
        """
        result = _parseargs_with_pkg_config_conf("pkg-config = conditional >=, nested")

        categories = self._categories(result.warnings)
        assert "-DTEST_PKG1_ENABLED" in get_build_state(result.args).cppflags, (
            f"'nested' was swallowed as a version operand across the comma. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert "pkg-config malformed package specification 'conditional >='" in categories, (
            f"the dangling operator was not reported as malformed: {categories!r}"
        )

    def test_missing_space_after_the_operator_never_silently_drops_the_floor(self, pkgconfig_env):
        """``pkg-config = conditional >=2.0.0`` against the 1.0.0 fixture
        must not resolve.

        This is the sharpest case in the module and the reason the
        half-spaced form is rejected rather than passed through. pkg-config
        swallows the version's first character into the operator token, so
        it enforces ``>= .0.0`` — which 1.0.0 satisfies — and exits 0.
        Measured on pkgconf 1.4.2::

            conditional >= 2.0.0    rc=1   required version is '>= 2.0.0'
            conditional >=2.0.0     rc=0   floor decayed to '>= .0.0'
            conditional >=0.5       rc=1   required version is '>= .5'

        So the failure is not an invented package name that someone
        eventually notices in a warning. It is a build that links 1.0.0
        while its conf file asks for 2.0.0 or newer, with an empty warning
        list. The sibling assertion is the one that fails loudly if the
        classifier ever lets this spelling reach a probe again.

        A version-floor test needs a floor the fixture does NOT meet:
        ``conditional >=1.0.0`` passes either way, because 1.0.0 satisfies
        both the requested floor and the corrupted one.
        """
        result = _parseargs_with_pkg_config_conf("pkg-config = conditional >=2.0.0")

        assert "-DTEST_PKG_ENABLED" not in get_build_state(result.args).cppflags, (
            f"a 2.0.0 floor resolved against the 1.0.0 fixture — the floor was dropped. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert result.warnings, "an unmet version floor produced no diagnostic at all"
        assert "pkg-config malformed package specification 'conditional >=2.0.0'" in self._categories(
            result.warnings
        ), f"the half-spaced constraint was not rejected before the probe: {result.warnings!r}"

    def test_absent_package_carrying_a_constraint_reported_as_absent(self, pkgconfig_env):
        """``<absent> >= 1.0`` is a missing package, not an unsatisfied floor.

        Both failures arrive from pkg-config as the same non-zero exit on the
        same spec, so they are only distinguishable by re-probing the bare
        name. Getting this backwards tells the user to go upgrade a package
        that was never installed.
        """
        result = _parseargs_with_pkg_config_conf(f"pkg-config = {self.MISSING} >= 1.0")

        categories = self._categories(result.warnings)
        assert [c for c in categories if self.MISSING in c and "not found" in c], (
            f"absent package carrying a constraint was not reported as absent: {categories!r}"
        )
        assert not [c for c in categories if "not satisfied" in c], (
            f"absent package misreported as an unsatisfied version floor: {categories!r}"
        )

    def test_append_pkg_config_whitespace_joined_value_splits_like_bare_key(self, pkgconfig_env):
        """``append-PKG-CONFIG = conditional <absent>`` must behave exactly as
        the bare ``pkg-config`` key does.

        The accumulating surface is the one the ``--pkg-config`` help text
        steers conf files towards, so it must not be the one that keeps the
        collapse.
        """
        result = _parseargs_with_pkg_config_conf("", axis_conf_line=f"append-PKG-CONFIG = conditional {self.MISSING}")

        assert "-DTEST_PKG_ENABLED" in get_build_state(result.args).cppflags, (
            f"append-PKG-CONFIG dropped the present package. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert not [w for w in result.warnings if f"conditional {self.MISSING}" in w], (
            f"append-PKG-CONFIG warned about the collapsed value: {result.warnings!r}"
        )

    def test_prepend_pkg_config_whitespace_joined_value_splits_like_bare_key(self, pkgconfig_env):
        """``prepend-PKG-CONFIG`` gets the same treatment as
        ``append-PKG-CONFIG``.

        Both route through ``_add_xxpend_argument`` and ``_do_xxpend_list``,
        so a fix applied to one and not the other leaves a surface behind.
        """
        result = _parseargs_with_pkg_config_conf("", axis_conf_line=f"prepend-PKG-CONFIG = conditional {self.MISSING}")

        assert "-DTEST_PKG_ENABLED" in get_build_state(result.args).cppflags, (
            f"prepend-PKG-CONFIG dropped the present package. "
            f"args.pkg_config={result.args.pkg_config!r}, CPPFLAGS={get_build_state(result.args).cppflags!r}"
        )
        assert not [w for w in result.warnings if f"conditional {self.MISSING}" in w], (
            f"prepend-PKG-CONFIG warned about the collapsed value: {result.warnings!r}"
        )


@pytest.mark.usefixtures("parsers_reset")
class TestVariantResolutionRespectsArgv:
    """parseargs must resolve the variant from ITS OWN argv, never from
    ambient sys.argv — a design pothole to guard: any variant-resolution
    helper that defaults its argv (extract_variant with no argument reads
    sys.argv) silently clobbers the parsed value for embedded callers and
    test harnesses whose argv differs from the process's. Both tests
    exercise the parseargs pipeline with argv that does NOT match sys.argv.
    """

    def test_argv_variant_preserved_when_not_aliased(self):
        """A --variant=<canonical-name> in argv survives parseargs even
        when sys.argv does not contain that flag."""

        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())  # defines dbg/rls aliases
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                argv = [
                    "--config=" + temp_config_name,
                    "--variant=gcc.debug",  # not in any alias map
                    "--no-git-root",
                ]
                cap = apptools.create_parser("regression test", argv=argv)
                cdb.CompilationDatabaseCreator.add_arguments(cap)
                compiletools.hunter.add_arguments(cap)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                assert args.variant == "gcc.debug", (
                    f"Expected --variant=gcc.debug from argv to survive parseargs, "
                    f"got {args.variant!r}. Reading sys.argv (which lacks --variant "
                    f"in pytest) would clobber the parsed value."
                )

    def test_argv_composite_variant_is_canonicalized(self):
        """A composite --variant on the CLI (comma/space separated) is
        canonicalized to its dotted form by stage_resolve_names, so
        downstream consumers see the canonical name in args.variant."""

        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                argv = [
                    "--config=" + temp_config_name,
                    "--variant=debug,gcc",
                    "--no-git-root",
                ]
                cap = apptools.create_parser("regression test", argv=argv)
                cdb.CompilationDatabaseCreator.add_arguments(cap)
                compiletools.hunter.add_arguments(cap)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                assert args.variant == "gcc.debug", (
                    f"Composite 'debug,gcc' on the CLI should canonicalize to "
                    f"'gcc.debug' (gcc sorts before debug in the canonical order); "
                    f"got {args.variant!r}."
                )


@pytest.mark.usefixtures("parsers_reset")
class TestVariableHandlingMethod:
    """End-to-end coverage for --variable-handling-method through the full
    parseargs pipeline (both parse paths — the plain parse and the append-mode
    reparse in _fix_variable_handling_method).

    Restores the coverage lost when test_environment_appends_config was
    deleted in 49dfd43e (2026-03-17): 17 days later, 1237b7b3 + 76bb739b
    regressed append mode (the reparse returned a namespace lacking the
    _parser/_context/_argv stashes, crashing _commonsubstitutions) and no
    test caught it — the regression walked in through this exact coverage
    hole. The three-part _stash_private_attrs assertion below pins the fix.
    """

    def _parse_with_env_cxxflags(self, method, extra_argv=None):
        """Run full parseargs with env CXXFLAGS set and the given
        variable-handling-method configured in ct.conf.

        Returns (args, cap, argv) so callers can assert the private-attr
        stashes point at the exact parser/argv this parse used, not merely
        that the attributes exist."""
        with uth.TempDirContext(), uth.EnvironmentContext({"CXXFLAGS": "-DVARFROMENV"}):
            uth.create_temp_ct_conf(os.getcwd(), extralines=[f"variable-handling-method={method}"])
            with uth.TempConfigContext(
                tempdir=os.getcwd(), extralines=['CXXFLAGS="-DVARFROMFILE"']
            ) as temp_config_name:
                argv = ["--config=" + temp_config_name, "--no-git-root"]
                if extra_argv:
                    argv.extend(extra_argv)
                cap = apptools.create_parser("variable handling test", argv=argv)
                apptools.add_common_arguments(cap, argv=argv)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                return args, cap, argv

    def test_environment_overrides_config(self):
        """Default method: env CXXFLAGS replaces the conf-file value."""
        args, _, _ = self._parse_with_env_cxxflags("override")
        assert args.variable_handling_method == "override"
        assert "-DVARFROMENV" in get_build_state(args).cxxflags
        assert "-DVARFROMFILE" not in get_build_state(args).cxxflags

    def test_environment_appends_config(self):
        """Append method: env CXXFLAGS accumulates onto the conf-file value.
        This is the test whose deletion opened the regression window."""
        args, _, _ = self._parse_with_env_cxxflags("append")
        assert args.variable_handling_method == "append"
        assert "-DVARFROMENV" in get_build_state(args).cxxflags
        assert "-DVARFROMFILE" in get_build_state(args).cxxflags

    def test_append_mode_at_high_verbosity(self, capsys):
        """verboseprintconfig (cake.py calls it post-parseargs at verbose>=3)
        reads args._parser.print_values() — the first stage of the two-stage
        regression (1237b7b3). The reparse namespace must carry the _parser
        stash for this not to crash with AttributeError."""
        args, _, _ = self._parse_with_env_cxxflags("append", extra_argv=["-vvv"])
        assert "-DVARFROMENV" in get_build_state(args).cxxflags
        apptools.verboseprintconfig(args)  # crashed pre-fix: no args._parser
        out = capsys.readouterr().out
        assert "Using variant =" in out

    def test_append_mode_restashes_private_attrs(self):
        """The append-mode reparse returns a FRESH namespace; parseargs must
        re-stash all three private attrs on it (the two-stage regression was
        exactly these going missing — _context crashed _commonsubstitutions
        unconditionally, _parser crashed verbose>=3, and a missing _argv
        silently drops CLI --variant-canonical-order at re-canonicalization).
        Identity/equality assertions, not hasattr: a stash of None or of a
        stale parser would still pass a presence check while reintroducing
        the silent _argv drop."""
        args, cap, argv = self._parse_with_env_cxxflags("append")
        assert args._parser is cap, "reparse namespace lost or replaced args._parser"
        assert args._context is not None, "reparse namespace lost args._context"
        assert args._argv == argv, "reparse namespace lost or replaced args._argv"

    def test_append_reparse_private_attr_set_matches_override_path(self):
        """Completeness guard for _stash_private_attrs: catch a fourth
        pre-reparse stash that bypasses it.

        _stash_private_attrs re-stashes exactly _parser/_context/_argv on
        the fresh namespace the append-mode reparse returns. The identity
        test above pins only those three known names — a future commit that
        stashes a NEW args._* attribute directly in parseargs before the
        reparse point (and forgets the reparse branch, exactly how the
        f67555b8 crash was born) would slip past it. Here we run the same
        argv/config through both parse paths and require the underscore-
        prefixed attribute sets to be IDENTICAL: any pre-reparse stash added
        outside _stash_private_attrs appears only in the override path and
        fails with a self-explanatory set diff. Same argv/config means the
        conditional stashes set during substitutions (latches, wild-B, ...)
        take the same branches on both paths, so full set equality holds —
        no allowlist needed."""
        args_override, _, _ = self._parse_with_env_cxxflags("override")
        args_append, _, _ = self._parse_with_env_cxxflags("append")
        override_private = {k for k in vars(args_override) if k.startswith("_")}
        append_private = {k for k in vars(args_append) if k.startswith("_")}
        assert override_private == append_private, (
            "Private-attr sets diverge between the override and append parse "
            f"paths.\nOnly in override: {sorted(override_private - append_private)}\n"
            f"Only in append: {sorted(append_private - override_private)}\n"
            "An attr present only in the override path means something stashed "
            "it on the namespace BEFORE the append-mode reparse without going "
            "through _stash_private_attrs — add it there (not at the call "
            "sites) so the reparse namespace gets it too."
        )

    def test_append_mode_does_not_mutate_os_environ(self, monkeypatch):
        """The env re-route (CXXFLAGS -> APPEND_CXXFLAGS) must happen in a
        local dict passed to parse_args(env_vars=...), never in os.environ:
        hook scripts and other children launched after parseargs must still
        see the flag vars the user exported."""
        # APPEND_CXXFLAGS is a legitimate user env interface (auto env var
        # for --append-CXXFLAGS); clear any ambient value so the not-in
        # assertion below tests for a leak, not for shell hygiene.
        monkeypatch.delenv("APPEND_CXXFLAGS", raising=False)
        with uth.TempDirContext(), uth.EnvironmentContext({"CXXFLAGS": "-DVARFROMENV"}):
            uth.create_temp_ct_conf(os.getcwd(), extralines=["variable-handling-method=append"])
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                argv = ["--config=" + temp_config_name, "--no-git-root"]
                cap = apptools.create_parser("env preservation test", argv=argv)
                apptools.add_common_arguments(cap, argv=argv)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                assert "-DVARFROMENV" in get_build_state(args).cxxflags
                assert os.environ.get("CXXFLAGS") == "-DVARFROMENV", (
                    "parseargs in append mode mutated os.environ: CXXFLAGS "
                    f"is now {os.environ.get('CXXFLAGS')!r}. Children launched "
                    "after parseargs (hook scripts) would see it vanish."
                )
                assert "APPEND_CXXFLAGS" not in os.environ, (
                    "parseargs in append mode leaked APPEND_CXXFLAGS into os.environ"
                )

    def test_append_mode_merges_with_existing_append_env_var(self):
        """If the user sets both CXXFLAGS and APPEND_CXXFLAGS in their
        environment under --variable-handling-method=append, both values
        must survive the CXXFLAGS -> APPEND_CXXFLAGS re-route: the
        pre-existing APPEND_CXXFLAGS must not be silently overwritten by
        the re-routed CXXFLAGS value."""
        with (
            uth.TempDirContext(),
            uth.EnvironmentContext({"CXXFLAGS": "-DVARFROMENV", "APPEND_CXXFLAGS": "-DFROMAPPENDENV"}),
        ):
            uth.create_temp_ct_conf(os.getcwd(), extralines=["variable-handling-method=append"])
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                argv = ["--config=" + temp_config_name, "--no-git-root"]
                cap = apptools.create_parser("append env merge test", argv=argv)
                apptools.add_common_arguments(cap, argv=argv)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                assert "-DVARFROMENV" in get_build_state(args).cxxflags, (
                    f"Expected re-routed CXXFLAGS value in cxxflags={get_build_state(args).cxxflags!r}"
                )
                assert "-DFROMAPPENDENV" in get_build_state(args).cxxflags, (
                    "Pre-existing APPEND_CXXFLAGS was discarded when CXXFLAGS was "
                    f"re-routed onto it: cxxflags={get_build_state(args).cxxflags!r}"
                )
                # Merge order is policy: existing APPEND_* first, re-routed
                # bare value last, so the bare env var wins conflicting
                # tokens under the compiler's last-token-wins rule.
                assert get_build_state(args).cxxflags.index("-DFROMAPPENDENV") < get_build_state(args).cxxflags.index(
                    "-DVARFROMENV"
                ), f"Merge order flipped: cxxflags={get_build_state(args).cxxflags!r}"

    def test_append_mode_via_cli_flag(self):
        """--variable-handling-method=append on the CLI (not just conf file)
        triggers the same reparse path."""
        with uth.TempDirContext(), uth.EnvironmentContext({"CXXFLAGS": "-DVARFROMENV"}):
            uth.create_temp_ct_conf(os.getcwd())
            with uth.TempConfigContext(
                tempdir=os.getcwd(), extralines=['CXXFLAGS="-DVARFROMFILE"']
            ) as temp_config_name:
                argv = [
                    "--config=" + temp_config_name,
                    "--variable-handling-method=append",
                    "--no-git-root",
                ]
                cap = apptools.create_parser("cli append test", argv=argv)
                apptools.add_common_arguments(cap, argv=argv)
                with uth.ParserContext():
                    args = apptools.parseargs(cap, argv, context=BuildContext())
                assert "-DVARFROMENV" in get_build_state(args).cxxflags
                assert "-DVARFROMFILE" in get_build_state(args).cxxflags


def _resolved_compiler_args(value, *, variant="gcc.debug"):
    """SimpleNamespace for _check_resolved_compiler_available with CC/CXX/LD
    all set to the same value — the common case across these tests."""
    return SimpleNamespace(variant=variant, CC=value, CXX=value, LD=value)


class TestResolvedCompilerAvailable:
    """The functional-compiler auto-detect kicks in only when args.CXX is
    None. A toolchain axis (e.g. gcc.conf) sets CXX=g++ explicitly, so on
    a system without gcc the build fails late and opaquely. The check
    catches it at parseargs end with a clear pointer at the variant chain.
    """

    def test_missing_binary_raises_with_variant_hint(self):
        args = _resolved_compiler_args("this-compiler-does-not-exist-7f3a")
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_resolved_compiler_available(args)
        msg = str(excinfo.value)
        assert "not on PATH" in msg
        assert "gcc.debug" in msg  # variant must appear in the diagnostic

    def test_existing_binary_passes_silently(self):
        real_cxx = shutil.which("g++") or shutil.which("clang++") or shutil.which("sh")
        assert real_cxx, "test environment lacks any usable executable"
        # Must not raise.
        apptools._check_resolved_compiler_available(_resolved_compiler_args(real_cxx))

    def test_unsupplied_sentinel_is_skipped(self):
        # The "unsupplied_implies_use_CXX" sentinel means a downstream
        # substitution replaces this with a real CXX value — the check
        # must not flag it as a missing binary.
        real_cxx = shutil.which("g++") or shutil.which("clang++") or shutil.which("sh")
        args = SimpleNamespace(
            variant="x",
            CC="unsupplied_implies_use_CXX",
            CXX=real_cxx,
            LD="unsupplied_implies_use_CXX",
        )
        apptools._check_resolved_compiler_available(args)

    def test_wrapper_invocation_checks_first_token(self):
        # Toolchain axes like ccache-gcc.conf set CXX="ccache g++". The
        # validator must tokenize and resolve the first token (the actual
        # executable to invoke) instead of feeding the whole string to
        # shutil.which, which would return None and false-positive raise.
        real_cxx = shutil.which("g++") or shutil.which("clang++") or shutil.which("sh")
        assert real_cxx, "test environment lacks any usable executable"
        # Use `env` (POSIX, ubiquitous on PATH) as a stand-in wrapper so
        # the test doesn't require ccache to be installed.
        wrapper = shutil.which("env")
        assert wrapper, "POSIX `env` must be on PATH for this test"
        # Must not raise — the first token (`env`) is on PATH.
        apptools._check_resolved_compiler_available(
            _resolved_compiler_args(f"env {real_cxx}", variant="ccache-gcc.debug")
        )

    def test_wrapper_with_missing_first_token_raises(self):
        # Mirror case: when the wrapper itself isn't on PATH, the validator
        # must still surface the failure (don't accidentally pass by ignoring
        # the resolved value).
        args = _resolved_compiler_args("this-wrapper-does-not-exist-7f3a g++", variant="ccache-gcc.debug")
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_resolved_compiler_available(args)
        msg = str(excinfo.value)
        assert "not on PATH" in msg
        assert "ccache-gcc.debug" in msg

    def test_wrapper_with_unbalanced_quote_raises_flag_tokenize_error(self):
        """coverage-gaps Task 9: an unbalanced quote in a wrapper-form
        CC/CXX/LD value raises the shared, attributed FlagTokenizeError
        (not a bare shlex ValueError) naming the offending slot. Only CXX
        is malformed here (CC/LD use the unsupplied sentinel, skipped)
        so the raise is unambiguously attributed to CXX -- the loop in
        _check_resolved_compiler_available checks CC before CXX, so a
        shared malformed value across all three (as
        _resolved_compiler_args gives) would raise on CC first instead.
        """
        args = SimpleNamespace(
            variant="ccache-gcc.debug",
            CC=apptools._UNSUPPLIED_USE_CXX,
            CXX='ccache "g++',
            LD=apptools._UNSUPPLIED_USE_CXX,
        )
        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CXX"):
            apptools._check_resolved_compiler_available(args)


class TestCompilerMinimumVersion:
    """compiletools assumes a C++20-capable toolchain, but a toolchain axis
    pinning CXX explicitly bypasses the functional-compiler probe and the
    bundled gcc.conf pins no -std=, so nothing else catches gcc 8. The floor
    check refuses at parseargs with a pointer at the toolchain."""

    def test_gcc_below_floor_raises(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 8))
        args = SimpleNamespace(variant="gcc.debug", CXX="g++")
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_compiler_minimum_version(args)
        msg = str(excinfo.value)
        assert "below compiletools' minimum supported toolchain" in msg
        assert "gcc >= 10" in msg
        assert "gcc.debug" in msg

    def test_gcc_at_floor_passes(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 10))
        apptools._check_compiler_minimum_version(SimpleNamespace(variant="gcc.debug", CXX="g++"))

    def test_old_clang_raises(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("clang", 9))
        with pytest.raises(RuntimeError, match="clang >= 10"):
            apptools._check_compiler_minimum_version(SimpleNamespace(variant="clang.debug", CXX="clang++"))

    def test_unknown_driver_skips_silently(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: None)
        apptools._check_compiler_minimum_version(SimpleNamespace(variant="x", CXX="some-cross-compiler"))

    def test_unset_cxx_skips_silently(self):
        apptools._check_compiler_minimum_version(SimpleNamespace(variant="x", CXX=None))


def _std_check_args(*, variant="x", cc="g++", cxx="g++", cflags="-O0", cxxflags=""):
    """Finalized namespace for _check_compiler_supports_requested_standard.
    Defaults match the most common shape (gcc-style driver, -O0 cflags)."""
    args = SimpleNamespace(variant=variant, CC=cc, CXX=cxx, CFLAGS=cflags, CXXFLAGS=cxxflags, verbose=0)
    uth.finalize_flag_state(args)
    return args


class TestCompilerSupportsRequestedStandard:
    """Static (compiler, version) -> max-std table is the cheap way to
    catch "user picked cxx26 on gcc 11" before the compile error surfaces
    with no pointer at the variant chain."""

    def test_too_old_for_requested_std_raises(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 11))
        args = _std_check_args(variant="gcc.cxx26.debug", cxxflags="-std=c++26 -O0")
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_compiler_supports_requested_standard(args)
        msg = str(excinfo.value)
        assert "does not support -std=c++26" in msg
        assert "gcc >= 14" in msg

    def test_recent_compiler_passes(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 14))
        args = _std_check_args(variant="gcc.cxx26.debug", cxxflags="-std=c++26 -O0")
        # 14 >= 14 — passes.
        apptools._check_compiler_supports_requested_standard(args)

    def test_unknown_driver_skips_silently(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: None)
        args = _std_check_args(cc="some-cross-compiler", cxx="some-cross-compiler", cflags="", cxxflags="-std=c++26")
        # Unknown driver → skip silently rather than false-positive.
        apptools._check_compiler_supports_requested_standard(args)

    def test_no_std_flag_skips_silently(self, monkeypatch):
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 4))
        # No -std= in flags → nothing to check.
        args = _std_check_args(variant="blank.debug", cc="gcc", cxxflags="-O0")
        apptools._check_compiler_supports_requested_standard(args)

    def test_alt_spelling_cxx2c_normalised_to_cxx26(self, monkeypatch):
        # gcc <14 / clang <18 spelled C++26 as -std=c++2c. The check should
        # normalise that to c++26 for the version lookup.
        monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda path, **_kw: ("gcc", 11))
        args = _std_check_args(cflags="", cxxflags="-std=c++2c -O0")
        with pytest.raises(RuntimeError, match=r"does not support -std=c\+\+2c"):
            apptools._check_compiler_supports_requested_standard(args)

    def test_compiler_major_version_handles_wrapper(self):
        # ccache-gcc.conf sets CXX="ccache g++". _compiler_major_version
        # must tokenize the wrapper invocation rather than feeding it to
        # subprocess as a single argv0 (which raises OSError and silently
        # degrades the check to "unknown driver, skip"). Use `env <cxx>`
        # as a portable stand-in for the ccache wrapper so the test runs
        # everywhere — `env --version` prints recognisable output, but
        # `env <gcc>` will forward --version to the real compiler.

        real_cxx = shutil.which("g++") or shutil.which("clang++")
        if not real_cxx:
            pytest.skip("no real C++ compiler on PATH")
        wrapper = shutil.which("env")
        assert wrapper, "POSIX `env` must be on PATH"

        bare = apptools._compiler_major_version(real_cxx)
        wrapped = apptools._compiler_major_version(f"env {real_cxx}")
        assert wrapped == bare, (
            f"Wrapper invocation must resolve the same (family, major) as the "
            f"bare compiler: bare={bare!r}, wrapped={wrapped!r}. A None on the "
            f"wrapped side means subprocess raised OSError on the compound string."
        )

    def test_compiler_major_version_probes_once_across_slot_spellings(self):
        # slot= is error attribution, not identity: the four parseargs
        # guards ask about the same compiler string under different slots,
        # and each must hit ONE cached --version probe. The cache keys on
        # the tokenized argv (slot stays outside), so keying drift that
        # fragments it into one subprocess per guard shows up here as
        # misses > 1.
        import compiletools.apptools_compiler as ac

        real_cxx = shutil.which("g++") or shutil.which("clang++")
        if not real_cxx:
            pytest.skip("no real C++ compiler on PATH")

        ac._compiler_version_probe.cache_clear()
        try:
            first = apptools._compiler_major_version(real_cxx, slot="CXX")
            second = apptools._compiler_major_version(real_cxx, slot="LD")
            third = apptools._compiler_major_version(real_cxx)
            assert first == second == third
            assert first is not None, "Precondition: the real compiler must be recognised."
            info = ac._compiler_version_probe.cache_info()
            assert info.currsize == 1 and info.misses == 1, (
                f"cache fragmented on slot: {info.misses} --version probes for one compiler string ({info})"
            )
        finally:
            ac._compiler_version_probe.cache_clear()


# ---------------------------------------------------------------------------
# Round 3: --ffile-prefix-map-target CLI flag (cross-user CAS sharing)
# ---------------------------------------------------------------------------


class TestFfilePrefixMapTargetArg:
    """The --ffile-prefix-map-target CLI argument controls the RHS of the
    auto-injected ``-ffile-prefix-map=<gitroot>=<target>`` flag added to
    the cxx/c token slots by ``build_state.stage_prefix_map``.

    Default ``.`` matches the Debian fixfilepath convention; gdb resolves
    via ``$cwd`` when run from the workspace. VSCode-heavy teams may
    prefer a sentinel like ``/__ct__/``.
    """

    def _build_parser(self):
        cap = configargparse.ArgParser(default_config_files=[])

        # add_common_arguments is where compile/link-related flags live
        # (--CXXFLAGS / --CFLAGS / --git-root / --ffile-prefix-map-target).
        # add_base_arguments only carries the variant/verbose/help skeleton.
        apptools.add_common_arguments(cap)
        return cap

    def test_default_is_dot(self):
        cap = self._build_parser()
        args = cap.parse_args([])
        assert args.ffile_prefix_map_target == "."

    def test_user_override_to_sentinel(self):
        cap = self._build_parser()
        args = cap.parse_args(["--ffile-prefix-map-target=/__ct__/"])
        assert args.ffile_prefix_map_target == "/__ct__/"

    def test_user_override_to_empty_string(self):
        cap = self._build_parser()
        args = cap.parse_args(["--ffile-prefix-map-target="])
        assert args.ffile_prefix_map_target == ""


class TestConfFileEncodingTolerance:
    """Regression: conf-file readers must tolerate non-ASCII bytes (e.g.
    em-dash U+2014 = 0xE2 0x80 0x94 in a comment) even when Python's
    default text encoding is ASCII.

    A user hit ``UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2``
    from ct-cake after editing a ct.conf comment with an em-dash. The bug
    is that conf readers called ``open(path)`` without an explicit
    encoding, so when the process was launched under ``PYTHONUTF8=0`` +
    ``LC_ALL=C`` (or any non-UTF-8 locale) Python decoded the file as
    ASCII and the em-dash byte sequence killed the parser.

    These tests simulate that environment by forcing ``builtins.open`` to
    default to ASCII whenever a caller omits ``encoding=`` for text mode.
    Each conf-file reader must succeed regardless.
    """

    @pytest.fixture
    def ascii_default_open(self, monkeypatch):
        """Make every ``open()`` that doesn't specify ``encoding=`` default
        to ASCII for text mode. Mirrors PYTHONUTF8=0 + LC_ALL=C."""

        real_open = builtins.open

        def open_with_ascii_default(*args, **kwargs):
            mode = kwargs.get("mode")
            if mode is None and len(args) > 1:
                mode = args[1]
            if mode is None:
                mode = "r"
            if "b" not in mode and "encoding" not in kwargs:
                kwargs["encoding"] = "ascii"
            return real_open(*args, **kwargs)

        monkeypatch.setattr(builtins, "open", open_with_ascii_default)
        return real_open

    def test_parse_conf_file_cached_tolerates_emdash_in_comment(self, ascii_default_open, tmp_path):
        conf = tmp_path / "ct.conf"
        with ascii_default_open(str(conf), "w", encoding="utf-8") as f:
            f.write("# Comment with em-dash — like this\n")
            f.write("variant = gcc.debug\n")

        cu.clear_cache()
        try:
            items = cu._parse_conf_file_cached(str(conf))
        finally:
            cu.clear_cache()
        assert dict(items).get("variant") == "gcc.debug"

    def test_check_legacy_variant_keys_tolerates_emdash_in_comment(self, ascii_default_open, tmp_path):
        conf = tmp_path / "ct.conf"
        with ascii_default_open(str(conf), "w", encoding="utf-8") as f:
            f.write("# Author note — reminds us why this exists\n")
            f.write("variant = gcc.debug\n")

        # Must not raise UnicodeDecodeError. The function only raises
        # RuntimeError when it finds an actual `variantaliases = {...}`
        # key, which this conf does not contain.
        _check_legacy_variant_config_keys([str(conf)])

    def test_check_legacy_cas_keys_tolerates_emdash_in_comment(self, ascii_default_open, tmp_path):
        conf = tmp_path / "ct.conf"
        with ascii_default_open(str(conf), "w", encoding="utf-8") as f:
            f.write("# Why we picked this dir — see README\n")
            f.write("cas-objdir = /tmp/objs\n")

        # Must not raise UnicodeDecodeError. The function only raises
        # RuntimeError when it finds legacy `objdir`/`pchdir` keys.
        _check_legacy_cas_config_keys([str(conf)])

    def test_composing_parser_opens_emdash_conf_via_configargparse(self, ascii_default_open, tmp_path):
        """End-to-end: ``_ComposingArgumentParser`` resolves a conf file
        with an em-dash comment via configargparse's own file-open path.
        This is the path ct-cake actually traverses on every invocation.
        """

        conf = tmp_path / "ct.conf"
        with ascii_default_open(str(conf), "w", encoding="utf-8") as f:
            f.write("# Pinned to gcc.debug — see ticket #4242\n")
            f.write("variant = gcc.debug\n")

        parser = _ComposingArgumentParser(
            default_config_files=[str(conf)],
            config_file_parser_class=_AccumulatingConfigFileParser,
            ignore_unknown_config_file_keys=True,
        )
        parser.add_argument("--variant", default="")
        args, _ = parser.parse_known_args([])
        assert args.variant == "gcc.debug"


def _wild_args(cxx, ldflags, variant="gcc.wild.release", ld=None):
    """Minimal namespace for unit-testing the wild normalization helpers.

    ``ld`` mirrors the ``--LD`` override. Left at the default ``None`` the
    namespace gets no ``LD`` attribute at all (the shape every pre-existing
    test in this module exercises: LD simply never supplied). Pass an
    explicit driver name, or one of the ``_UNSUPPLIED_USE_CXX*`` sentinels,
    to exercise ``_effective_link_driver``'s LD-vs-CXX precedence.
    """
    args = SimpleNamespace(CXX=cxx, LDFLAGS=ldflags, variant=variant, verbose=0)
    if ld is not None:
        args.LD = ld
    uth.finalize_flag_state(args)
    return args


def test_check_wild_b_with_bazel_backend_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    args = _wild_args("g++", "", "gcc.wild-B.release")
    args.backend = "bazel"
    with pytest.raises(RuntimeError, match=r"wild-B.*--backend=bazel"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_b_with_make_backend_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("gcc", 11))
    args = _wild_args("g++", "", "gcc.wild-B.release")
    args.backend = "make"
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_usable_missing_wild_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    with pytest.raises(RuntimeError, match="wild-linker"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_old_gcc_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("gcc", 15))
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    with pytest.raises(RuntimeError, match="gcc >= 16"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_gcc16_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("gcc", 16))
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_usable_clang_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("clang", 22))
    # post-rewrite form on clang
    args = _wild_args("clang++", "--ld-path=wild", "clang.wild.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_b_old_gcc_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("gcc", 11))
    # wild-B has no version gate — that's its whole purpose.
    args = _wild_args("g++", "", "gcc.wild-B.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_usable_ld_wins_clang_over_cxx_gcc(monkeypatch):
    """LD=clang++ with CXX=g++: the effective link driver is LD (clang), so
    the gcc<16 version gate must not fire even though CXX names a gcc
    binary. Asserting on the probed compiler name (not just "no raise")
    pins that ``_effective_link_driver`` actually consulted LD, not CXX --
    a version-gate bug that silently fell back to CXX would still pass a
    bare no-raise check whenever the CXX-derived family also happened to
    dodge the gate.
    """
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")

    def _fake_version(compiler, **_kw):
        assert compiler == "clang++", f"expected the LD override to be probed, got {compiler!r}"
        return ("clang", 22)

    monkeypatch.setattr(apptools_validate, "_compiler_major_version", _fake_version)
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release", ld="clang++")
    apptools._check_wild_linker_usable(args)  # no raise: LD (clang) wins over CXX (gcc)


def test_check_wild_usable_ld_wins_gcc_over_cxx_clang(monkeypatch):
    """LD=g++ with CXX=clang++: the effective link driver is LD (gcc), so
    an old-gcc raise fires even though CXX names clang -- the mirror image
    of the clang-wins case above, again pinning which argument actually
    gets probed.
    """
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")

    def _fake_version(compiler, **_kw):
        assert compiler == "g++", f"expected the LD override to be probed, got {compiler!r}"
        return ("gcc", 15)

    monkeypatch.setattr(apptools_validate, "_compiler_major_version", _fake_version)
    args = _wild_args("clang++", "-fuse-ld=wild", "gcc.wild.release", ld="g++")
    with pytest.raises(RuntimeError, match="gcc >= 16"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_ld_sentinel_falls_back_to_cxx(monkeypatch):
    """LD explicitly left at the ``_UNSUPPLIED_USE_CXX`` sentinel (as the
    real --LD argparse default is, not just an absent attribute) must still
    fall back to CXX for the version gate. Every other test in this module
    exercises the "LD attribute never set" shape; this pins the actual
    production sentinel value that ``_effective_link_driver`` checks for.
    """
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools_validate, "_compiler_major_version", lambda c, **_kw: ("gcc", 15))
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release", ld=apptools._UNSUPPLIED_USE_CXX)
    with pytest.raises(RuntimeError, match="gcc >= 16"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_not_selected_noop(monkeypatch):
    def _boom(name):
        raise AssertionError("should not probe when wild is not selected")

    # Build the fixture BEFORE arming the probe trap: finalize_flag_state
    # legitimately resolves the compiler identity via shutil.which.
    args = _wild_args("g++", "-O2 -lm", "gcc.release")
    monkeypatch.setattr(shutil, "which", _boom)
    apptools._check_wild_linker_usable(args)  # returns before any probe


class TestValidateOtelTimingPair:
    """Truth table for ``validate_otel_timing_pair``:

    | otel_export | --no-timing in argv | timing (in) | outcome                            |
    |-------------|---------------------|-------------|------------------------------------|
    | False       | -                   | -           | silent no-op, args unchanged       |
    | True        | no                  | False       | args.timing flipped to True        |
    | True        | no                  | True        | args.timing stays True (no-op)     |
    | True        | yes                 | False       | SystemExit (hard error)            |

    The "explicit --no-timing" signal is recovered by scanning ``args._argv``
    because the parsed ``args.timing`` value alone cannot distinguish
    ``--no-timing`` from "no flag passed, default False".
    """

    @staticmethod
    def _ns(*, otel_export, timing, argv):
        ns = SimpleNamespace(otel_export=otel_export, timing=timing)
        ns._argv = list(argv)
        return ns

    def test_no_op_when_otel_export_absent(self):
        args = self._ns(otel_export=False, timing=False, argv=[])
        apptools.validate_otel_timing_pair(args)
        assert args.timing is False
        assert args.otel_export is False

    def test_no_op_when_otel_export_absent_even_if_no_timing_passed(self):
        # --no-timing on its own is a perfectly valid request; validator
        # only cares about the pair.
        args = self._ns(otel_export=False, timing=False, argv=["--no-timing"])
        apptools.validate_otel_timing_pair(args)
        assert args.timing is False

    def test_otel_export_implies_timing_when_no_timing_not_passed(self):
        # The headline case: user typed --otel-export and forgot --timing.
        # Implication fires; args.timing flips True.
        args = self._ns(otel_export=True, timing=False, argv=["--otel-export"])
        apptools.validate_otel_timing_pair(args)
        assert args.timing is True

    def test_otel_export_and_timing_both_explicit_is_no_op(self):
        # Both already on: nothing to do; timing stays True.
        args = self._ns(otel_export=True, timing=True, argv=["--otel-export", "--timing"])
        apptools.validate_otel_timing_pair(args)
        assert args.timing is True

    def test_explicit_no_timing_with_otel_export_hard_errors(self):
        args = self._ns(otel_export=True, timing=False, argv=["--otel-export", "--no-timing"])
        with pytest.raises(SystemExit) as excinfo:
            apptools.validate_otel_timing_pair(args)
        msg = str(excinfo.value)
        assert "--otel-export" in msg
        assert "--no-timing" in msg
        assert "mutually exclusive" in msg

    def test_missing_argv_attr_treated_as_no_explicit_no_timing(self):
        # Defensive: a caller that builds args without going through
        # parseargs (e.g. SimpleNamespace in a test) must not crash the
        # validator. Treat the missing _argv as "no explicit --no-timing"
        # so the implication still fires.
        ns = SimpleNamespace(otel_export=True, timing=False)
        apptools.validate_otel_timing_pair(ns)
        assert ns.timing is True

    def test_no_timing_inside_a_quoted_value_not_treated_as_flag(self):
        # Sanity: the substring match is on whole argv tokens, not a
        # regex over the joined string. A value like ``--config=--no-timing``
        # (silly but legal) must not trigger the hard error.
        args = self._ns(
            otel_export=True,
            timing=False,
            argv=["--otel-export", "--config=--no-timing"],
        )
        apptools.validate_otel_timing_pair(args)
        assert args.timing is True


class TestCasDirAllowFakeGitPropagation:
    """Regression: ``--allow-fake-git`` must influence CAS dir defaults.

    Previously ``add_cas_directory_arguments`` baked the absolute gitroot-
    anchored default at parser-registration time, BEFORE
    ``apptools.parseargs`` had a chance to propagate the parsed
    ``--allow-fake-git`` flag into ``git_utils.set_allow_fake_git``. So
    the strict-mode pre-parse resolution of ``find_git_root()`` got
    baked into ``argparse`` defaults, and ``unsupplied_replacement``
    only swaps when the value contains the literal ``"unsupplied"`` --
    concrete absolute paths pass through unchanged.

    Failure mode: user runs ``ct-cake --allow-fake-git`` from
    ``/tmp/proj/subdir`` with a bare ``/tmp/proj/.git/`` placeholder.
    Registrar-time strict ``find_git_root`` rejects the fake ``.git``
    and falls through to the cwd ``/tmp/proj/subdir``; the post-parse
    permissive walker (every other callsite) returns ``/tmp/proj`` --
    but ``args.cas_objdir`` still holds the wrong subdir-anchored path.

    The fix moves default-computation into
    ``resolve_cas_directory_arguments`` (which runs AFTER the
    ``set_allow_fake_git`` propagation), using the literal sentinel
    ``"unsupplied"`` as the registrar-time default.
    """

    def test_allow_fake_git_propagates_to_cas_dir_defaults(self, tmp_path, monkeypatch):
        # Build a fake repo: <tmp>/proj/.git (bare empty dir, no HEAD), cwd in subdir.
        repo = tmp_path / "proj"
        sub = repo / "subdir"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()  # bare placeholder, no HEAD -> strict mode rejects
        monkeypatch.chdir(sub)

        import compiletools.git_utils

        compiletools.git_utils.clear_cache()
        compiletools.git_utils.set_allow_fake_git(False)
        try:
            cap = apptools.create_parser("test", include_config=False)
            apptools.add_cas_directory_arguments(cap, variant="gcc.debug")
            args = cap.parse_args(["--allow-fake-git", "--variant=gcc.debug"])
            args.variant = "gcc.debug"
            args.verbose = 0
            apptools.resolve_cas_directory_arguments(args)

            # Cas dirs must anchor at the fake gitroot (repo), not the cwd subdir.
            repo_real = os.path.realpath(str(repo))
            sub_real = os.path.realpath(str(sub))
            for attr in ("cas_objdir", "cas_pchdir", "cas_pcmdir", "cas_exedir"):
                value = getattr(args, attr)
                value_real = os.path.realpath(value)
                assert value_real.startswith(repo_real), (
                    f"{attr}={value!r} (realpath={value_real!r}) should anchor at fake "
                    f"gitroot {repo_real!r} after --allow-fake-git propagated"
                )
                assert not value_real.startswith(sub_real + os.sep), (
                    f"{attr}={value!r} (realpath={value_real!r}) is anchored at cwd subdir "
                    f"{sub_real!r}; the registrar-time strict-mode default was not overridden"
                )
        finally:
            compiletools.git_utils.set_allow_fake_git(False)
            compiletools.git_utils.clear_cache()

    def test_resolver_is_idempotent_after_fix(self, tmp_path, monkeypatch):
        """Calling resolve_cas_directory_arguments twice must not double-suffix."""
        repo = tmp_path / "proj"
        sub = repo / "subdir"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()
        monkeypatch.chdir(sub)

        import compiletools.git_utils

        compiletools.git_utils.clear_cache()
        compiletools.git_utils.set_allow_fake_git(False)
        try:
            cap = apptools.create_parser("test", include_config=False)
            apptools.add_cas_directory_arguments(cap, variant="gcc.debug")
            args = cap.parse_args(["--allow-fake-git", "--variant=gcc.debug"])
            args.variant = "gcc.debug"
            args.verbose = 0
            apptools.resolve_cas_directory_arguments(args)
            first = {a: getattr(args, a) for a in ("cas_objdir", "cas_pchdir", "cas_pcmdir", "cas_exedir")}
            apptools.resolve_cas_directory_arguments(args)
            second = {a: getattr(args, a) for a in ("cas_objdir", "cas_pchdir", "cas_pcmdir", "cas_exedir")}
            assert first == second, f"resolver not idempotent: {first!r} vs {second!r}"
        finally:
            compiletools.git_utils.set_allow_fake_git(False)
            compiletools.git_utils.clear_cache()


class TestNoteShadowedBareHookValuesProvenanceFailure:
    """Regression: a raising ``get_conf_file_provenance`` must leave a trace.

    Before the fix, an exception from the provenance side channel was
    silently swallowed, so at ``verbose >= 1`` a user would see no
    "ct: note: ..." shadow warning and have no way to tell whether that
    was because nothing was shadowed or because the lookup itself failed.
    """

    @staticmethod
    def _make_args(verbose):
        parser = SimpleNamespace(get_conf_file_provenance=MagicMock(side_effect=RuntimeError("boom")))
        return SimpleNamespace(
            verbose=verbose,
            _parser=parser,
            prebuild_scripts=[],
        )

    def test_breadcrumb_emitted_at_verbose_1(self, capsys):
        # Same verbose >= 1 gate as the shadow note itself: at any verbosity
        # where the note could have appeared, its failure must be visible too.
        args = self._make_args(verbose=1)
        apptools._note_shadowed_bare_values(args, "prebuild-script", "prebuild_scripts")
        captured = capsys.readouterr()
        assert "hook-shadow provenance lookup failed" in captured.err
        assert "'prebuild-script'" in captured.err

    def test_breadcrumb_not_emitted_at_verbose_0(self, capsys):
        args = self._make_args(verbose=0)
        apptools._note_shadowed_bare_values(args, "prebuild-script", "prebuild_scripts")
        captured = capsys.readouterr()
        assert "hook-shadow provenance lookup failed" not in captured.err
        assert captured.err == ""
