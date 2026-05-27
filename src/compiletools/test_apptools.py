"""Unit tests for apptools.py utility functions."""

import builtins
import contextlib
import io
import os
import shlex
import shutil
import subprocess
import tempfile
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import configargparse
import pytest
import stringzilla as sz

import compiletools.apptools as apptools
import compiletools.compilation_database as cdb
import compiletools.configutils as cu
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.apptools import (
    _AccumulatingConfigFileParser,
    _add_include_paths_to_flags,
    _add_xxpend_argument,
    _add_xxpend_arguments,
    _check_legacy_cas_config_keys,
    _check_legacy_variant_config_keys,
    _ComposingArgumentParser,
    _deduplicate_all_flags,
    _do_xxpend,
    _flatten_variables,
    _has_prefix_map_flag,
    _pkg_config_provenance_label,
    _safely_unquote_string,
    _set_project_name,
    _set_project_version,
    _setup_pkg_config_overrides,
    _strip_quotes,
    _substitute_CXX_for_missing,
    _test_compiler_functionality,
    _unify_cpp_cxx_flags,
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
    registercallback,
    resetcallbacks,
    substitutions,
    terminalcolumns,
    unsupplied_replacement,
    verbose_print_args,
    verboseprintconfig,
)
from compiletools.build_context import BuildContext
from compiletools.utils import split_command_cached


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


def _stub_gitroot_and_chdir(monkeypatch, target):
    """Stub git_utils.find_git_root to return str(target) and chdir into target."""
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(target))
    monkeypatch.chdir(target)


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


class TestFindSystemHeader:
    """Test find_system_header()."""

    def test_header_found(self, tmp_path):
        (tmp_path / "myheader.h").write_text("// header\n")
        args = SimpleNamespace(CPPFLAGS=f"-I{tmp_path}", CFLAGS="", CXXFLAGS="", INCLUDE="")
        result = find_system_header("myheader.h", args)
        assert result is not None
        assert result.endswith("myheader.h")

    def test_header_not_found(self, tmp_path):
        args = SimpleNamespace(CPPFLAGS=f"-I{tmp_path}", CFLAGS="", CXXFLAGS="", INCLUDE="")
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
        result = _safely_unquote_string("'hello")
        assert isinstance(result, str)


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


class TestUnsuppliedReplacement:
    def test_unsupplied_returns_default(self):
        result = unsupplied_replacement("unsupplied_use_CXX", "g++", 0, "CPP")
        assert result == "g++"

    def test_supplied_returns_original(self):
        result = unsupplied_replacement("clang++", "g++", 0, "CXX")
        assert result == "clang++"

    def test_verbose_prints(self):
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            unsupplied_replacement("unsupplied_use_CXX", "g++", 6, "CPP")
        assert "unsupplied" in mock_stdout.getvalue()


class TestSubstituteCXXForMissing:
    def test_substitutes_cpp_and_cppflags(self):
        args = SimpleNamespace(
            verbose=0,
            CXX="g++",
            CXXFLAGS="-Wall",
            CPP="unsupplied_implies_use_CXX",
            CPPFLAGS="unsupplied_implies_use_CXXFLAGS",
        )
        _substitute_CXX_for_missing(args)
        assert args.CPP == "g++"
        assert args.CPPFLAGS == "-Wall"

    def test_substitutes_ld_when_present(self):
        args = SimpleNamespace(
            verbose=0,
            CXX="g++",
            CXXFLAGS="-Wall",
            CPP="g++",
            CPPFLAGS="-Wall",
            LD="unsupplied_implies_use_CXX",
            LDFLAGS="unsupplied_implies_use_CXXFLAGS",
        )
        _substitute_CXX_for_missing(args)
        assert args.LD == "g++"
        assert args.LDFLAGS == "-Wall"

    def test_no_ld_attribute_ok(self):
        args = SimpleNamespace(
            verbose=0,
            CXX="g++",
            CXXFLAGS="-Wall",
            CPP="g++",
            CPPFLAGS="-Wall",
        )
        _substitute_CXX_for_missing(args)  # Should not raise


class TestAddIncludePathsToFlags:
    def test_adds_include_paths(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc",
            CPPFLAGS="-Wall",
            CFLAGS="-Wall",
            CXXFLAGS="-Wall",
            verbose=0,
        )
        _add_include_paths_to_flags(args)
        assert "-I /tmp/inc" in args.CPPFLAGS
        assert "-I /tmp/inc" in args.CFLAGS
        assert "-I /tmp/inc" in args.CXXFLAGS

    def test_no_duplicate_include_paths(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc",
            CPPFLAGS="-Wall -I /tmp/inc",
            CFLAGS="-Wall",
            CXXFLAGS="-Wall",
            verbose=0,
        )
        _add_include_paths_to_flags(args)
        # /tmp/inc already a proper -I entry in CPPFLAGS; do not add again.
        assert args.CPPFLAGS.count("/tmp/inc") == 1

    def test_verbose_include_print(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc",
            CPPFLAGS="-Wall",
            CFLAGS="-Wall",
            CXXFLAGS="-Wall",
            verbose=6,
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            _add_include_paths_to_flags(args)
        assert "include" in mock_stdout.getvalue().lower()


class TestExtractSystemIncludePaths:
    def test_dash_I_attached(self):
        args = SimpleNamespace(CPPFLAGS="-I/foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_dash_I_detached(self):
        args = SimpleNamespace(CPPFLAGS="-I /foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_isystem_detached(self):
        args = SimpleNamespace(CPPFLAGS="-isystem /foo/bar", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert "/foo/bar" in result

    def test_no_flags(self):
        args = SimpleNamespace(CPPFLAGS="", CXXFLAGS="")
        result = extract_system_include_paths(args)
        assert result == []

    def test_deduplicates(self):
        args = SimpleNamespace(CPPFLAGS="-I/foo", CXXFLAGS="-I/foo")
        result = extract_system_include_paths(args)
        assert result.count("/foo") == 1

    def test_custom_flag_sources(self):
        args = SimpleNamespace(CFLAGS="-I/bar")
        result = extract_system_include_paths(args, flag_sources=["CFLAGS"])
        assert "/bar" in result

    def test_missing_attribute(self):
        args = SimpleNamespace()
        result = extract_system_include_paths(args, flag_sources=["CPPFLAGS"])
        assert result == []


class TestExtractCommandLineMacros:
    def test_basic_define(self):
        args = SimpleNamespace(CPPFLAGS="-DFOO=bar", CFLAGS="", CXXFLAGS="", verbose=0, CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["FOO"] == "bar"

    def test_define_no_value(self):
        args = SimpleNamespace(CPPFLAGS="-DFOO", CFLAGS="", CXXFLAGS="", verbose=0, CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["FOO"] == "1"

    def test_list_flags(self):
        args = SimpleNamespace(CPPFLAGS=["-DA=1", "-DB=2"], CFLAGS="", CXXFLAGS="", verbose=0, CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result["A"] == "1"
        assert result["B"] == "2"

    def test_empty_flags(self):
        args = SimpleNamespace(CPPFLAGS="", CFLAGS="", CXXFLAGS="", verbose=0, CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result == {}

    def test_non_define_ignored(self):
        args = SimpleNamespace(CPPFLAGS="-Wall -O2", CFLAGS="", CXXFLAGS="", verbose=0, CXX="g++")
        result = extract_command_line_macros(args, include_compiler_macros=False)
        assert result == {}

    def test_extract_command_line_macros_handles_detached_d_form(self):
        """Detached -D form (separate -D and value tokens) was previously
        silently dropped by extract_command_line_macros. Must now be
        recognized so it's consistent with cmdline_d_macro_names."""
        args = SimpleNamespace(CPPFLAGS="-D FOO=1 -D BAR", CFLAGS="", CXXFLAGS="", verbose=0, CXX=None)
        macros = extract_command_line_macros(args, include_compiler_macros=False)
        assert macros == {"FOO": "1", "BAR": "1"}  # bare -D BAR defaults value to "1"


class TestDoXxpend:
    def test_prepend(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall",
            prepend_cppflags=["-O2"],
            append_cppflags=[],
            verbose=0,
        )
        _do_xxpend(args, "CPPFLAGS")
        assert args.CPPFLAGS.startswith("-O2")

    def test_append(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall",
            prepend_cppflags=[],
            append_cppflags=["-O2"],
            verbose=0,
        )
        _do_xxpend(args, "CPPFLAGS")
        assert args.CPPFLAGS.endswith("-O2")

    def test_no_duplicate_xxpend(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall -O2",
            prepend_cppflags=["-O2"],
            append_cppflags=[],
            verbose=0,
        )
        _do_xxpend(args, "CPPFLAGS")
        # -O2 already in CPPFLAGS, should not be prepended again
        assert args.CPPFLAGS.count("-O2") == 1

    def test_no_xxpend_attrs(self):
        args = SimpleNamespace(CPPFLAGS="-Wall", verbose=0)
        _do_xxpend(args, "CPPFLAGS")  # Should not raise
        assert args.CPPFLAGS == "-Wall"


class TestUnifyCppCxxFlags:
    def test_unifies_flags(self):
        args = SimpleNamespace(
            CPPFLAGS="-DFOO",
            CXXFLAGS="-Wall",
            separate_flags_CPP_CXX=False,
        )
        _unify_cpp_cxx_flags(args)
        assert args.CPPFLAGS == args.CXXFLAGS
        assert "-DFOO" in args.CPPFLAGS
        assert "-Wall" in args.CPPFLAGS

    def test_separate_flags_skips(self):
        args = SimpleNamespace(
            CPPFLAGS="-DFOO",
            CXXFLAGS="-Wall",
            separate_flags_CPP_CXX=True,
        )
        _unify_cpp_cxx_flags(args)
        assert args.CPPFLAGS == "-DFOO"
        assert args.CXXFLAGS == "-Wall"


class TestDeduplicateAllFlags:
    def test_deduplicates(self):
        args = SimpleNamespace(CPPFLAGS="-Wall -Wall -O2", CFLAGS="-g -g", CXXFLAGS="-Wall", LDFLAGS="-lm -lm")
        _deduplicate_all_flags(args)
        assert args.CPPFLAGS.count("-Wall") == 1
        assert args.CFLAGS.count("-g") == 1
        assert args.LDFLAGS.count("-lm") == 1

    def test_missing_flag_ok(self):
        args = SimpleNamespace(CPPFLAGS="-Wall", CFLAGS="-g", CXXFLAGS="-Wall")
        _deduplicate_all_flags(args)  # No LDFLAGS, should not raise


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

        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                cached_pkg_config("nonexistent_test_pkg_clear_cache", "--cflags")

        assert cached_pkg_config.cache_info().currsize > 0, "Cache should be populated"

        clear_cache()

        assert cached_pkg_config.cache_info().currsize == 0, "Cache should be cleared"


class TestCallbackSystem:
    def test_register_and_reset(self):
        called = []
        registercallback(lambda args: called.append(True))
        resetcallbacks()
        # After reset, only _commonsubstitutions should remain. Read via the
        # module attribute since resetcallbacks() rebinds it; a module-level
        # `from ... import` snapshot would still see the pre-reset list.
        assert len(apptools._substitutioncallbacks) == 1

    def test_substitutions_calls_callbacks(self):
        resetcallbacks()
        called = []
        registercallback(lambda args: called.append(True))
        args = SimpleNamespace(verbose=0)
        # _commonsubstitutions will fail without full args, so just test
        # with our own callback only

        saved = apptools._substitutioncallbacks[:]
        try:
            apptools._substitutioncallbacks = [lambda args: called.append("main")]
            substitutions(args)
        finally:
            apptools._substitutioncallbacks = saved
            resetcallbacks()
        assert "main" in called


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
        assert "obj" in args.cas_objdir

    def test_add_output_directory_arguments_registers_use_mtime(self):
        """M2: --use-mtime must be registered for every backend that
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


class TestProjectVersionAndNameOptIn:
    """--project-{version,name}{,-cmd} are opt-in: no flag specified -> no
    cmdline -D injection. This keeps the cmdline -D macro set clean for
    TUs that don't need these macros, so the byte-level scope filter has
    no needle to scan for in unrelated headers."""

    def _make_args(self, **kwargs):
        cap = configargparse.ArgParser(default_config_files=[])
        add_target_arguments_ex(cap)
        argv = []
        for key, value in kwargs.items():
            argv.append(f"--{key}={value}")
        args = cap.parse_args(argv)
        args.CPPFLAGS = ""
        args.CFLAGS = ""
        args.CXXFLAGS = ""
        args.verbose = 0
        return args

    def test_no_injection_when_neither_flag_set_version(self):
        args = self._make_args()
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION" not in args.CPPFLAGS
        assert "-DCT_PROJECT_VERSION" not in args.CFLAGS
        assert "-DCT_PROJECT_VERSION" not in args.CXXFLAGS

    def test_no_injection_when_neither_flag_set_name(self):
        args = self._make_args()
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME" not in args.CPPFLAGS
        assert "-DCT_PROJECT_NAME" not in args.CFLAGS
        assert "-DCT_PROJECT_NAME" not in args.CXXFLAGS

    def test_explicit_version_injects(self):
        args = self._make_args(**{"project-version": "1.2.3"})
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CPPFLAGS
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CFLAGS
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CXXFLAGS

    def test_explicit_name_injects(self):
        args = self._make_args(**{"project-name": "myapp"})
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CPPFLAGS
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CFLAGS
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CXXFLAGS

    def test_version_cmd_alone_injects(self):
        args = self._make_args(**{"project-version-cmd": "echo from-cmd-1.0"})
        _set_project_version(args)
        # First whitespace token of stdout is taken
        assert "-DCT_PROJECT_VERSION='\"from-cmd-1.0\"'" in args.CPPFLAGS

    def test_name_cmd_alone_injects(self):
        args = self._make_args(**{"project-name-cmd": "echo cmd-named-app"})
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME='\"cmd-named-app\"'" in args.CPPFLAGS

    def test_explicit_version_takes_precedence_over_cmd(self):
        args = self._make_args(**{"project-version": "explicit-1.0", "project-version-cmd": "echo from-cmd"})
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION='\"explicit-1.0\"'" in args.CPPFLAGS
        assert "from-cmd" not in args.CPPFLAGS

    def test_idempotent_when_macro_already_present(self):
        args = self._make_args(**{"project-name": "newvalue"})
        args.CPPFLAGS = '-DCT_PROJECT_NAME="oldvalue"'
        _set_project_name(args)
        assert args.CPPFLAGS.count("-DCT_PROJECT_NAME") == 1
        assert "oldvalue" in args.CPPFLAGS
        assert "newvalue" not in args.CPPFLAGS

    def test_project_version_token_is_argv_safe(self):
        """The -DCT_PROJECT_VERSION token must survive the full
        _set_project_version → _unify_cpp_cxx_flags pipeline.

        _unify_cpp_cxx_flags deduplicates by calling shlex.split on the raw
        CPPFLAGS string and then joining the result. If that join uses
        ' '.join (a plain space-join) instead of shlex.join, any token
        containing double-quote characters (like -DCT_PROJECT_VERSION="1.2.3")
        is written back to the raw string with unquoted double-quotes.  A
        subsequent shlex.split then strips those double-quotes so args.flags.cxx
        ends up with '-DCT_PROJECT_VERSION=1.2.3' — a bare numeric token — which
        causes the compiler to reject '1.2.3' as "too many decimal points in
        number".

        The assertion: after the full injection+unification round-trip, the token
        in the tokenized flag list must have literal double-quote characters (the C
        string-literal delimiters).
        """

        args = self._make_args(**{"project-version": "1.2.3"})
        args.separate_flags_CPP_CXX = False  # default; unify will run
        _set_project_version(args)

        # Simulate _unify_cpp_cxx_flags (runs immediately after _set_project_version
        # in _commonsubstitutions) followed by _finalize_flag_state (shlex.split):
        _unify_cpp_cxx_flags(args)
        final_tokens = split_command_cached(args.CPPFLAGS)

        version_tokens = [t for t in final_tokens if t.startswith("-DCT_PROJECT_VERSION")]
        assert version_tokens, (
            f"no -DCT_PROJECT_VERSION token survived the injection+unification "
            f"round-trip; final CPPFLAGS tokens: {final_tokens!r}"
        )
        assert len(version_tokens) == 1, f"duplicate version tokens: {version_tokens!r}"
        token = version_tokens[0]

        macro, _, value = token.partition("=")
        assert macro == "-DCT_PROJECT_VERSION"
        assert value == '"1.2.3"', (
            f'macro value must be the C string literal "1.2.3" '
            f"(with literal double-quote chars) after the unification round-trip, "
            f"got {value!r}.  Likely cause: _unify_cpp_cxx_flags uses ' '.join "
            f"instead of shlex.join to reconstruct args.CPPFLAGS, dropping the "
            f"shell-quoting that protects the double-quote characters."
        )

    def test_project_name_token_is_argv_safe(self):
        """Mirror of test_project_version_token_is_argv_safe for CT_PROJECT_NAME."""

        args = self._make_args(**{"project-name": "myapp"})
        args.separate_flags_CPP_CXX = False
        _set_project_name(args)
        _unify_cpp_cxx_flags(args)
        final_tokens = split_command_cached(args.CPPFLAGS)

        name_tokens = [t for t in final_tokens if t.startswith("-DCT_PROJECT_NAME")]
        assert name_tokens, (
            f"no -DCT_PROJECT_NAME token survived the injection+unification "
            f"round-trip; final CPPFLAGS tokens: {final_tokens!r}"
        )
        token = name_tokens[0]

        macro, _, value = token.partition("=")
        assert macro == "-DCT_PROJECT_NAME"
        assert value == '"myapp"', (
            f'macro value must be the C string literal "myapp" '
            f"(with literal double-quote chars) after the unification round-trip, "
            f"got {value!r}"
        )


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


class TestCachedPkgConfig:
    def test_missing_package(self):
        clear_cache()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = cached_pkg_config("nonexistent_pkg_12345", "--cflags")
        assert result == ""
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

    def test_prepends_when_override_dir_exists(self, monkeypatch, tmp_path, pkgconfig_dir):
        """When ct.conf.d/pkgconfig/ exists at gitroot, it is prepended to PKG_CONFIG_PATH."""

        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/existing/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        pkg_config_path = os.environ["PKG_CONFIG_PATH"]
        assert pkg_config_path.startswith(str(pkgconfig_dir))
        assert "/existing/path" in pkg_config_path

    def test_noop_when_no_override_dir(self, monkeypatch, tmp_path):
        """When ct.conf.d/pkgconfig/ does not exist, PKG_CONFIG_PATH is unchanged."""
        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == "/original/path"

    def test_works_when_pkg_config_path_unset(self, monkeypatch, tmp_path, pkgconfig_dir):
        """When PKG_CONFIG_PATH is not set, sets it to just the override dir."""

        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == str(pkgconfig_dir)

    def test_idempotency(self, monkeypatch, tmp_path, pkgconfig_dir):
        """Calling twice does not duplicate the path in PKG_CONFIG_PATH."""

        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/existing/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        first_value = os.environ["PKG_CONFIG_PATH"]

        _setup_pkg_config_overrides(ctx)
        second_value = os.environ["PKG_CONFIG_PATH"]

        assert first_value == second_value

    def test_verbose_output(self, monkeypatch, tmp_path, capsys, pkgconfig_dir):
        """Verbose >= 4 prints the override path with an auto-discovered label."""

        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, verbose=4)

        captured = capsys.readouterr()
        assert "Prepended pkg-config path:" in captured.out
        assert str(pkgconfig_dir) in captured.out
        # cwd == gitroot here, so the cwd branch fires first and dedup
        # suppresses the gitroot duplicate. Label must say "cwd".
        assert "(auto-discovered: cwd)" in captured.out
        assert "(auto-discovered: gitroot)" not in captured.out

    def test_verbose_output_labels_gitroot_when_cwd_differs(self, monkeypatch, tmp_path, capsys):
        """When cwd != gitroot and only the gitroot has a pkgconfig dir,
        the auto-discovered label says 'gitroot'."""
        repo_root = tmp_path / "repo"
        project_dir = tmp_path / "repo" / "subproject"
        repo_pkgconfig = repo_root / "ct.conf.d" / "pkgconfig"
        repo_pkgconfig.mkdir(parents=True)
        project_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(repo_root))
        monkeypatch.chdir(project_dir)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, verbose=4)

        captured = capsys.readouterr()
        assert str(repo_pkgconfig) in captured.out
        assert "(auto-discovered: gitroot)" in captured.out
        assert "(auto-discovered: cwd)" not in captured.out

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

    def test_cwd_pkgconfig_takes_priority_over_gitroot(self, monkeypatch, tmp_path):
        """cwd/ct.conf.d/pkgconfig/ is prepended before gitroot/ct.conf.d/pkgconfig/."""
        repo_root = tmp_path / "repo"
        project_dir = tmp_path / "repo" / "subproject"
        repo_pkgconfig = repo_root / "ct.conf.d" / "pkgconfig"
        cwd_pkgconfig = project_dir / "ct.conf.d" / "pkgconfig"
        repo_pkgconfig.mkdir(parents=True)
        cwd_pkgconfig.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(repo_root))
        monkeypatch.chdir(project_dir)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/system/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs[0] == str(cwd_pkgconfig)
        assert dirs[1] == str(repo_pkgconfig)
        assert dirs[2] == "/system/path"

    def test_cwd_only_no_gitroot(self, monkeypatch, tmp_path):
        """When outside a git repo, cwd/ct.conf.d/pkgconfig/ is still discovered."""
        cwd_pkgconfig = tmp_path / "ct.conf.d" / "pkgconfig"
        cwd_pkgconfig.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == str(cwd_pkgconfig)

    def test_dedup_when_cwd_equals_gitroot(self, monkeypatch, tmp_path, pkgconfig_dir):
        """When cwd is the git root, only one entry is prepended."""

        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        pkg_config_path = os.environ["PKG_CONFIG_PATH"]
        assert pkg_config_path == str(pkgconfig_dir)
        assert pkg_config_path.count(str(pkgconfig_dir)) == 1

    def test_prepend_promotes_existing_entry_to_front(self, monkeypatch, tmp_path):
        """Regression: --prepend-PKG-CONFIG-PATH=/X with PKG_CONFIG_PATH=/Y:/X
        must produce /X:/Y (X promoted), not /Y:/X (unchanged)."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/system:/local")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, prepend_paths=["/local"])

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/local", "/system"]

    def test_append_demotes_existing_entry_to_end(self, monkeypatch, tmp_path):
        """Symmetric: --append-PKG-CONFIG-PATH should move an existing
        entry to the end."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/local:/system")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, append_paths=["/local"])

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/system", "/local"]

    def test_prepend_higher_priority_conf_wins_over_lower(self, monkeypatch, tmp_path):
        """Regression: ``prepend-PKG-CONFIG-PATH`` set in two layered conf
        files must place the higher-priority conf's entry leftmost in
        ``PKG_CONFIG_PATH``, mirroring the codebase's prepend/append
        idiom (highest-priority source wins).

        ``args.prepend_pkg_config_path`` arrives ordered
        ``[low_priority_conf, ..., high_priority_conf, cli_in_parse_order]``
        — the same order ``_AccumulatingConfigFileParser`` produces for
        every ``prepend-*`` / ``append-*`` key. For compiler-flag slots
        the rightmost token wins, so that order yields CLI > high-conf >
        low-conf naturally. ``PKG_CONFIG_PATH`` resolves leftmost-first,
        so the accumulator list must be *reversed* on emission to
        preserve the same priority ordering.

        Before the fix, the base ct.conf entry sat leftmost and silently
        shadowed every axis-level override that targeted the same .pc
        file, causing the wrong ABI flavor of a pinned library to be
        selected by downstream consumers.
        """
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            prepend_paths=["/base/pkgconfig", "/axisX/pkgconfig"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/axisX/pkgconfig", "/base/pkgconfig"]

    def test_append_higher_priority_conf_wins_over_lower(self, monkeypatch, tmp_path):
        """Symmetric to ``test_prepend_higher_priority_conf_wins_over_lower``
        for ``append-PKG-CONFIG-PATH``. Within the appended group, the
        higher-priority conf entry still has to land leftmost — appends
        are fallback paths searched after prepends + existing env, and
        within that fallback group leftmost still wins."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            append_paths=["/base/pkgconfig", "/axisX/pkgconfig"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/axisX/pkgconfig", "/base/pkgconfig"]

    def test_prepend_cli_wins_over_conf(self, monkeypatch, tmp_path):
        """The CLI portion of ``--prepend-PKG-CONFIG-PATH`` is appended to
        the accumulator list after every conf-file contribution (matches
        the ``_ComposingArgumentParser`` CLI re-append). The reversal
        therefore puts CLI entries leftmost, ahead of any conf-file
        prepend — so CLI overrides every conf the same way it does for
        compiler-flag slots."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            prepend_paths=["/conf/pkgconfig", "/cli/pkgconfig"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs[0] == "/cli/pkgconfig"
        assert dirs[1] == "/conf/pkgconfig"

    def test_append_cli_wins_over_conf(self, monkeypatch, tmp_path):
        """Symmetric: within the appended fallback group, CLI lands
        leftmost (most preferred fallback)."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            append_paths=["/conf/pkgconfig", "/cli/pkgconfig"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs[-2] == "/cli/pkgconfig"
        assert dirs[-1] == "/conf/pkgconfig"

    def test_prepend_within_cli_last_wins(self, monkeypatch, tmp_path):
        """Multiple ``--prepend-PKG-CONFIG-PATH`` flags on the same CLI:
        the rightmost-typed flag ends up leftmost in PKG_CONFIG_PATH,
        matching the "last-occurrence wins" convention every other
        ``prepend-*`` / ``append-*`` key follows in this codebase."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            prepend_paths=["/cli/first", "/cli/second"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/cli/second", "/cli/first"]

    def test_append_within_cli_last_wins(self, monkeypatch, tmp_path):
        """Symmetric for ``--append-PKG-CONFIG-PATH``: within the
        appended fallback group, the rightmost-typed CLI flag lands
        leftmost (most preferred fallback)."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(
            ctx,
            append_paths=["/cli/first", "/cli/second"],
        )

        dirs = os.environ["PKG_CONFIG_PATH"].split(os.pathsep)
        assert dirs == ["/cli/second", "/cli/first"]

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

    def test_flag_set_only_after_env_mutation_succeeds(self, monkeypatch, tmp_path):
        """Regression: pkg_config_overrides_applied must NOT be set
        if the function raises before mutating PKG_CONFIG_PATH — otherwise
        a retry within the same context is silently suppressed."""

        def boom(filename=None):
            raise RuntimeError("simulated find_git_root failure")

        monkeypatch.setattr("compiletools.git_utils.find_git_root", boom)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/system")

        ctx = BuildContext()
        try:
            _setup_pkg_config_overrides(ctx)
        except RuntimeError:
            pass
        assert ctx.pkg_config_overrides_applied is False, (
            "Flag must remain False if the function failed; otherwise the caller has no way to retry."
        )

    def test_restore_pkg_config_path_undoes_mutation(self, monkeypatch, tmp_path, pkgconfig_dir):
        """Regression: BuildContext must expose a way to undo the
        global env mutation, so long-lived processes / tests using
        multiple sequential contexts don't leak PKG_CONFIG_PATH state."""
        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        # Mutated
        assert os.environ["PKG_CONFIG_PATH"] != "/original/path"

        ctx.restore_pkg_config_path()

        assert os.environ.get("PKG_CONFIG_PATH") == "/original/path"
        assert ctx.pkg_config_overrides_applied is False, (
            "After restore, the flag should be reset so a future apply works."
        )

    def test_restore_when_pkg_config_path_was_unset(self, monkeypatch, tmp_path, pkgconfig_dir):
        """Restore must remove PKG_CONFIG_PATH if it was originally unset."""
        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        assert "PKG_CONFIG_PATH" in os.environ

        ctx.restore_pkg_config_path()

        assert "PKG_CONFIG_PATH" not in os.environ

    def test_restore_runs_after_subprocess_exception(self, monkeypatch, tmp_path, pkgconfig_dir):
        """Regression: restore_pkg_config_path() must cleanly undo the
        env mutation even when a downstream pkg-config subprocess raises
        between apply and restore. Long-lived processes / tests rely on
        this for cleanup-by-context-manager / try/finally patterns."""
        _stub_gitroot_and_chdir(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        mutated_value = os.environ["PKG_CONFIG_PATH"]
        assert mutated_value != "/original/path"

        # Mock subprocess to raise — simulate batch pkg-config failure
        def boom(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "pkg-config", "boom")

        monkeypatch.setattr(subprocess, "run", boom)

        # Caller's try/finally pattern: regardless of pkg-config exception,
        # restore must succeed and leave the environment clean.
        try:
            subprocess.run(["pkg-config", "--exists", "fake"], check=True)
        except subprocess.CalledProcessError:
            pass
        finally:
            ctx.restore_pkg_config_path()

        assert os.environ.get("PKG_CONFIG_PATH") == "/original/path", (
            "Restore must succeed even when a pkg-config subprocess raised"
        )
        assert ctx.pkg_config_overrides_applied is False


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
        ``args.append_cxxflags`` (and therefore the final ``args.CXXFLAGS``).
        """

        with uth.TempDirContextNoChange() as repo_root:
            self._setup_three_axis_conf_tree(repo_root)
            argv = ["--variant=gcc,release,extras", "--no-git-root"]
            args = _parseargs_for_variant(repo_root, argv)

            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in args.CXXFLAGS, (
                    f"{marker} missing from args.CXXFLAGS={args.CXXFLAGS!r}. "
                    f"Multi-conf append-CXXFLAGS composition is broken — only "
                    f"the highest-priority conf's value survived. "
                    f"args.append_cxxflags={args.append_cxxflags!r}"
                )
                assert marker in args.CFLAGS, (
                    f"{marker} missing from args.CFLAGS={args.CFLAGS!r}; "
                    f"append-CFLAGS suffers the same bug as append-CXXFLAGS."
                )

    def test_cli_append_combines_with_conf_append(self):
        """A ``--append-CXXFLAGS`` token on the CLI accumulates with the conf
        file's ``append-CXXFLAGS`` rather than replacing it.

        With three conf files contributing append values and one CLI value,
        all four should reach the final ``args.CXXFLAGS``. (Stock
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
            assert "-DFROM_CLI" in args.CXXFLAGS, args.CXXFLAGS
            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in args.CXXFLAGS, (
                    f"CLI --append-CXXFLAGS swallowed {marker} from the conf "
                    f"hierarchy. CXXFLAGS={args.CXXFLAGS!r}, "
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
                assert marker in args.LDFLAGS, (
                    f"{marker} missing from LDFLAGS={args.LDFLAGS!r}; "
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
                assert marker in args.CXXFLAGS, (
                    f"{marker} missing from CXXFLAGS={args.CXXFLAGS!r}; "
                    f"list-form syntax may have broken the accumulating "
                    f"parser. args.append_cxxflags={args.append_cxxflags!r}"
                )

    def test_append_order_lower_priority_before_higher(self):
        """Lower-priority axis values must appear BEFORE higher-priority axis
        values in the merged flag string, so that compilers' "last occurrence
        wins" rule resolves conflicting flags (e.g. ``-O0`` vs ``-O3``) in
        favor of the higher-priority axis. With the conf-file hierarchy
        ``gcc < release < extras``, an ``-O0`` in gcc.conf must end up to
        the LEFT of an ``-O3`` in release.conf in args.CXXFLAGS.
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

            cxx = args.CXXFLAGS
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
        ``args.append_include`` reaches the final ``args.INCLUDE`` via
        ``_do_xxpend('INCLUDE')`` in ``_tier_one_modifications``.
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

            for inc_dir in (inc_a, inc_b, inc_c):
                assert inc_dir in args.INCLUDE, (
                    f"{inc_dir} missing from args.INCLUDE={args.INCLUDE!r}; "
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

            assert "-DFROM_CLI_SPACE" in args.CXXFLAGS, args.CXXFLAGS
            for marker in ("-DFROM_GCC_AXIS", "-DFROM_RELEASE_AXIS", "-DFROM_EXTRAS_AXIS"):
                assert marker in args.CXXFLAGS, (
                    f"Space-form CLI --append-CXXFLAGS swallowed {marker}. "
                    f"args.CXXFLAGS={args.CXXFLAGS!r}, "
                    f"args.append_cxxflags={args.append_cxxflags!r}"
                )

    def test_three_axis_prepend_cxxflags_all_present(self):
        """``prepend-CXXFLAGS`` follows the same accumulation rule as
        ``append-CXXFLAGS``: when three axis confs each ``prepend-CXXFLAGS``,
        all three values reach ``args.prepend_cxxflags`` and the final
        ``args.CXXFLAGS``. ``prepend-*`` uses ``action='append'`` under the
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
                assert marker in args.CXXFLAGS, (
                    f"{marker} missing from args.CXXFLAGS={args.CXXFLAGS!r}. "
                    f"prepend-CXXFLAGS values are not accumulating across the "
                    f"conf hierarchy. args.prepend_cxxflags={args.prepend_cxxflags!r}"
                )


@pytest.mark.usefixtures("parsers_reset")
class TestVariantResolutionRespectsArgv:
    """Regression tests for the substitutions() variant-from-sys.argv bug.

    Historically _commonsubstitutions called extract_variant() with no argv,
    so it read sys.argv even when parseargs had been given a custom argv.
    This caused embedded callers and test harnesses to see args.variant
    reset to whatever sys.argv implied. Both tests below exercise the
    parseargs pipeline with argv that does NOT match sys.argv.
    """

    def test_argv_variant_preserved_when_not_aliased(self):
        """A --variant=<canonical-name> in argv survives substitutions even
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
                    f"Expected --variant=gcc.debug from argv to survive substitutions, "
                    f"got {args.variant!r}. The pre-fix code re-read sys.argv (which "
                    f"lacks --variant in pytest) and clobbered the parsed value."
                )

    def test_argv_composite_variant_is_canonicalized(self):
        """A composite --variant on the CLI (comma/space separated) is
        canonicalized to its dotted form by _commonsubstitutions, so
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


def _std_check_args(*, variant="x", cc="g++", cxx="g++", cflags="-O0", cxxflags=""):
    """SimpleNamespace for _check_compiler_supports_requested_standard.
    Defaults match the most common shape (gcc-style driver, -O0 cflags)."""
    return SimpleNamespace(variant=variant, CC=cc, CXX=cxx, CFLAGS=cflags, CXXFLAGS=cxxflags)


class TestCompilerSupportsRequestedStandard:
    """Static (compiler, version) -> max-std table is the cheap way to
    catch "user picked cxx26 on gcc 11" before the compile error surfaces
    with no pointer at the variant chain."""

    def test_too_old_for_requested_std_raises(self, monkeypatch):
        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 11))
        args = _std_check_args(variant="gcc.cxx26.debug", cxxflags="-std=c++26 -O0")
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_compiler_supports_requested_standard(args)
        msg = str(excinfo.value)
        assert "does not support -std=c++26" in msg
        assert "gcc >= 14" in msg

    def test_recent_compiler_passes(self, monkeypatch):
        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 14))
        args = _std_check_args(variant="gcc.cxx26.debug", cxxflags="-std=c++26 -O0")
        # 14 >= 14 — passes.
        apptools._check_compiler_supports_requested_standard(args)

    def test_unknown_driver_skips_silently(self, monkeypatch):
        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: None)
        args = _std_check_args(cc="some-cross-compiler", cxx="some-cross-compiler", cflags="", cxxflags="-std=c++26")
        # Unknown driver → skip silently rather than false-positive.
        apptools._check_compiler_supports_requested_standard(args)

    def test_no_std_flag_skips_silently(self, monkeypatch):
        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 4))
        # No -std= in flags → nothing to check.
        args = _std_check_args(variant="blank.debug", cc="gcc", cxxflags="-O0")
        apptools._check_compiler_supports_requested_standard(args)

    def test_alt_spelling_cxx2c_normalised_to_cxx26(self, monkeypatch):
        # gcc <14 / clang <18 spelled C++26 as -std=c++2c. The check should
        # normalise that to c++26 for the version lookup.
        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 11))
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


# ---------------------------------------------------------------------------
# Round 3: --ffile-prefix-map-target CLI flag (cross-user CAS sharing)
# ---------------------------------------------------------------------------


class TestFfilePrefixMapTargetArg:
    """The --ffile-prefix-map-target CLI argument controls the RHS of the
    auto-injected ``-ffile-prefix-map=<gitroot>=<target>`` flag added to
    CXXFLAGS / CFLAGS in :func:`apptools._inject_ffile_prefix_map`.

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


class TestHasPrefixMapFlag:
    """``_has_prefix_map_flag`` detects user-specified prefix-map flags so
    :func:`apptools._inject_ffile_prefix_map` can skip auto-injection on
    a per-slot basis (user choice wins).
    """

    def test_detects_all_four_aliases(self):
        for prefix in (
            "-ffile-prefix-map",
            "-fdebug-prefix-map",
            "-fmacro-prefix-map",
            "-fcanon-prefix-map",
        ):
            assert _has_prefix_map_flag(f"-O2 {prefix}=/foo=. -g")
            assert _has_prefix_map_flag(f"{prefix}=/foo=.")

    def test_negative_cases(self):
        assert not _has_prefix_map_flag("-O2 -g -Wall")
        assert not _has_prefix_map_flag("")
        # Lookalike: -fno-omit-frame-pointer shares the -f prefix but
        # is not a prefix-map flag. Must not false-positive.
        assert not _has_prefix_map_flag("-fno-omit-frame-pointer")
        # Bare prefix without trailing '=': not a recognized prefix-map
        # flag (the flag syntax is OLD=NEW after the equals).
        assert not _has_prefix_map_flag("-ffile-prefix-map")

    def test_quoted_d_macro_does_not_false_positive(self):
        """Regression: a ``-D`` macro whose VALUE happens to contain the
        literal ``-ffile-prefix-map=`` substring (e.g. a build-reason
        string baked into the binary) must NOT be mistaken for a
        user-supplied prefix-map flag — silently skipping auto-injection
        for that slot would cause per-user-divergent ``.o`` bytes for a
        project that thought it had cross-user CAS sharing. Substring
        detection on the raw string returned True here; tokenized
        detection correctly returns False.
        """

        assert not _has_prefix_map_flag("-DREASON='-ffile-prefix-map=oops='")
        # And a real prefix-map sitting next to the masquerading -D=
        # is still detected.
        assert _has_prefix_map_flag("-DFOO=bar -ffile-prefix-map=/a=/b")

    def test_unbalanced_quote_fallback(self):
        """An unparseable flag string (shlex raises ValueError on
        unbalanced quotes) is treated as user-supplied prefix-map —
        the conservative call: an opaque string is unsafe to interpret
        either way, so decline auto-injection rather than risk appending
        a flag the user might already have inside their unparseable
        text. Pinned explicitly so future changes don't silently flip.
        """

        assert _has_prefix_map_flag("'unbalanced quote")


class TestInjectFfilePrefixMap:
    """``_inject_ffile_prefix_map`` appends
    ``-ffile-prefix-map=<gitroot>=<target>`` to args.CXXFLAGS / args.CFLAGS
    when the user has not already specified any prefix-map flag (per
    slot independently). The injection is a no-op when no git root is
    resolvable (find_git_root returns the cwd as a fallback root, but
    the helper treats that case the same — see below).
    """

    def _make_args(self, **overrides):
        defaults = dict(
            ffile_prefix_map_target=".",
            CXXFLAGS="-O2 -g",
            CFLAGS="-O2",
            LDFLAGS="",
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_appends_when_absent(self, monkeypatch):
        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args()
        apptools._inject_ffile_prefix_map(args)
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CXXFLAGS
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CFLAGS

    def test_respects_user_override_per_slot(self, monkeypatch):
        """User-set ``-fdebug-prefix-map`` in CXXFLAGS suppresses injection
        for CXXFLAGS only; CFLAGS still gets the default."""

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args(CXXFLAGS="-O2 -fdebug-prefix-map=/user/set=foo")
        apptools._inject_ffile_prefix_map(args)
        # CXXFLAGS unchanged: user already specified a prefix-map flag
        assert args.CXXFLAGS == "-O2 -fdebug-prefix-map=/user/set=foo"
        # CFLAGS gets the default injection (independent slot)
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CFLAGS

    def test_no_op_when_git_root_falsy(self, monkeypatch):
        """An empty / falsy gitroot is the identity -- no anchor to
        canonicalize against, so injection is silently skipped."""

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "")
        args = self._make_args(CXXFLAGS="-O2", CFLAGS="-O2")
        apptools._inject_ffile_prefix_map(args)
        assert args.CXXFLAGS == "-O2"
        assert args.CFLAGS == "-O2"

    def test_honors_custom_target(self, monkeypatch):
        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args(ffile_prefix_map_target="/__ct__/", CFLAGS="")
        apptools._inject_ffile_prefix_map(args)
        assert "-ffile-prefix-map=/home/alice/proj=/__ct__/" in args.CXXFLAGS

    def test_handles_empty_initial_flag_string(self, monkeypatch):
        """No leading whitespace when the slot starts empty."""

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/repo")
        args = self._make_args(CXXFLAGS="", CFLAGS="")
        apptools._inject_ffile_prefix_map(args)
        assert args.CXXFLAGS == "-ffile-prefix-map=/repo=."
        assert args.CFLAGS == "-ffile-prefix-map=/repo=."

    def test_idempotent(self, monkeypatch):
        """Second call detects its own injection and skips."""

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/repo")
        args = self._make_args()
        apptools._inject_ffile_prefix_map(args)
        first_cxx = args.CXXFLAGS
        first_c = args.CFLAGS
        apptools._inject_ffile_prefix_map(args)
        assert first_cxx == args.CXXFLAGS
        assert first_c == args.CFLAGS

    def test_quoted_d_macro_does_not_block_injection(self, monkeypatch):
        """End-to-end regression: a ``-D`` whose VALUE contains the
        literal ``-ffile-prefix-map=`` substring previously caused
        ``_has_prefix_map_flag`` to return True (substring match),
        so ``_inject_ffile_prefix_map`` skipped auto-injection for that
        slot. The user thought they had cross-user CAS sharing; they
        actually got per-user-divergent ``.o`` bytes. After the
        tokenization fix, auto-injection happens as expected.
        """

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args(
            CXXFLAGS="-O2 -DREASON='-ffile-prefix-map=oops='",
            CFLAGS="-O2 -DREASON='-ffile-prefix-map=oops='",
        )
        apptools._inject_ffile_prefix_map(args)
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CXXFLAGS
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CFLAGS
        # The masquerading -D should still be present, untouched.
        assert "-DREASON='-ffile-prefix-map=oops='" in args.CXXFLAGS
        assert "-DREASON='-ffile-prefix-map=oops='" in args.CFLAGS


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


def _wild_args(cxx, ldflags, variant="gcc.wild.release"):
    """Minimal namespace for unit-testing the wild normalization helpers."""
    return SimpleNamespace(CXX=cxx, LDFLAGS=ldflags, variant=variant)


def test_normalize_wild_clang_rewrites_to_ld_path():
    args = _wild_args("clang++", "-fuse-ld=wild", "clang.wild.release")
    apptools._normalize_wild_linker(args)
    assert "--ld-path=wild" in args.LDFLAGS
    assert "-fuse-ld=wild" not in args.LDFLAGS


def test_normalize_wild_gcc_passthrough():
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    apptools._normalize_wild_linker(args)
    assert args.LDFLAGS == "-fuse-ld=wild"


def test_normalize_wild_unknown_compiler_passthrough():
    args = _wild_args("weirdcc", "-fuse-ld=wild", "weird.wild.release")
    apptools._normalize_wild_linker(args)
    assert args.LDFLAGS == "-fuse-ld=wild"


def test_normalize_wild_noop_when_not_selected():
    args = _wild_args("clang++", "-O2 -lm", "clang.release")
    apptools._normalize_wild_linker(args)
    assert args.LDFLAGS == "-O2 -lm"


def test_normalize_wild_explicit_ld_overrides_cxx_for_clang():
    # _effective_link_driver prefers args.LD: a clang LD with a gcc CXX
    # still triggers the clang rewrite (LDFLAGS is consumed by the link
    # driver, which is LD here).
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    args.LD = "clang++"
    apptools._normalize_wild_linker(args)
    assert "--ld-path=wild" in args.LDFLAGS
    assert "-fuse-ld=wild" not in args.LDFLAGS


def test_normalize_wild_b_injects_dash_B_and_makes_symlink(tmp_path, monkeypatch):
    fake_wild = tmp_path / "wild"
    fake_wild.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(fake_wild) if name == "wild" else None)
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda *a, **k: str(tmp_path))
    args = _wild_args("g++", "", "gcc.wild-B.release")
    apptools._normalize_wild_linker(args)

    search_dir = tmp_path / ".ct-wild-ld"
    assert f"-B{search_dir}" in args.LDFLAGS
    ld_link = search_dir / "ld"
    assert ld_link.is_symlink()
    assert os.readlink(ld_link) == str(fake_wild)


def test_materialize_wild_b_idempotent(tmp_path, monkeypatch):
    fake_wild = tmp_path / "wild"
    fake_wild.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(fake_wild) if name == "wild" else None)
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda *a, **k: str(tmp_path))
    d1 = apptools._materialize_wild_b_searchdir()
    d2 = apptools._materialize_wild_b_searchdir()
    assert d1 == d2 == str(tmp_path / ".ct-wild-ld")
    ld_link = tmp_path / ".ct-wild-ld" / "ld"
    assert ld_link.is_symlink()
    assert os.readlink(ld_link) == str(fake_wild)


def test_materialize_wild_b_falls_back_to_tempdir_without_gitroot(tmp_path, monkeypatch):
    fake_wild = tmp_path / "wild"
    fake_wild.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(fake_wild) if name == "wild" else None)
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda *a, **k: None)
    faketmp = tmp_path / "faketmp"
    faketmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(faketmp))

    result = apptools._materialize_wild_b_searchdir()
    assert result == str(faketmp / "ct-wild-ld")
    assert (faketmp / "ct-wild-ld" / "ld").is_symlink()


def test_check_wild_usable_missing_wild_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    with pytest.raises(RuntimeError, match="wild-linker"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_old_gcc_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools, "_compiler_major_version", lambda c: ("gcc", 15))
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    with pytest.raises(RuntimeError, match="gcc >= 16"):
        apptools._check_wild_linker_usable(args)


def test_check_wild_usable_gcc16_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools, "_compiler_major_version", lambda c: ("gcc", 16))
    args = _wild_args("g++", "-fuse-ld=wild", "gcc.wild.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_usable_clang_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools, "_compiler_major_version", lambda c: ("clang", 22))
    # post-rewrite form on clang
    args = _wild_args("clang++", "--ld-path=wild", "clang.wild.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_b_old_gcc_ok(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/wild")
    monkeypatch.setattr(apptools, "_compiler_major_version", lambda c: ("gcc", 11))
    # wild-B has no version gate — that's its whole purpose.
    args = _wild_args("g++", "", "gcc.wild-B.release")
    apptools._check_wild_linker_usable(args)  # no raise


def test_check_wild_usable_not_selected_noop(monkeypatch):
    def _boom(name):
        raise AssertionError("should not probe when wild is not selected")

    monkeypatch.setattr(shutil, "which", _boom)
    args = _wild_args("g++", "-O2 -lm", "gcc.release")
    apptools._check_wild_linker_usable(args)  # returns before any probe
