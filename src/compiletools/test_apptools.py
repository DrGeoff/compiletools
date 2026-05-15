"""Unit tests for apptools.py utility functions."""

import io
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import configargparse
import pytest
import stringzilla as sz

from compiletools.apptools import (
    _add_include_paths_to_flags,
    _add_xxpend_argument,
    _add_xxpend_arguments,
    _deduplicate_all_flags,
    _do_xxpend,
    _flatten_variables,
    _safely_unquote_string,
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

    def test_header_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            header_path = os.path.join(tmpdir, "myheader.h")
            with open(header_path, "w") as f:
                f.write("// header\n")

            args = SimpleNamespace(CPPFLAGS=f"-I{tmpdir}", CFLAGS="", CXXFLAGS="", INCLUDE="")
            result = find_system_header("myheader.h", args)
            assert result is not None
            assert result.endswith("myheader.h")

    def test_header_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = SimpleNamespace(CPPFLAGS=f"-I{tmpdir}", CFLAGS="", CXXFLAGS="", INCLUDE="")
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
        import shlex

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
        import warnings

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
        # After reset, only _commonsubstitutions should remain
        from compiletools.apptools import _substitutioncallbacks

        assert len(_substitutioncallbacks) == 1

    def test_substitutions_calls_callbacks(self):
        resetcallbacks()
        called = []
        registercallback(lambda args: called.append(True))
        args = SimpleNamespace(verbose=0)
        # _commonsubstitutions will fail without full args, so just test
        # with our own callback only
        from compiletools import apptools

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
        from compiletools.apptools import _set_project_version

        args = self._make_args()
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION" not in args.CPPFLAGS
        assert "-DCT_PROJECT_VERSION" not in args.CFLAGS
        assert "-DCT_PROJECT_VERSION" not in args.CXXFLAGS

    def test_no_injection_when_neither_flag_set_name(self):
        from compiletools.apptools import _set_project_name

        args = self._make_args()
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME" not in args.CPPFLAGS
        assert "-DCT_PROJECT_NAME" not in args.CFLAGS
        assert "-DCT_PROJECT_NAME" not in args.CXXFLAGS

    def test_explicit_version_injects(self):
        from compiletools.apptools import _set_project_version

        args = self._make_args(**{"project-version": "1.2.3"})
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CPPFLAGS
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CFLAGS
        assert "-DCT_PROJECT_VERSION='\"1.2.3\"'" in args.CXXFLAGS

    def test_explicit_name_injects(self):
        from compiletools.apptools import _set_project_name

        args = self._make_args(**{"project-name": "myapp"})
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CPPFLAGS
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CFLAGS
        assert "-DCT_PROJECT_NAME='\"myapp\"'" in args.CXXFLAGS

    def test_version_cmd_alone_injects(self):
        from compiletools.apptools import _set_project_version

        args = self._make_args(**{"project-version-cmd": "echo from-cmd-1.0"})
        _set_project_version(args)
        # First whitespace token of stdout is taken
        assert "-DCT_PROJECT_VERSION='\"from-cmd-1.0\"'" in args.CPPFLAGS

    def test_name_cmd_alone_injects(self):
        from compiletools.apptools import _set_project_name

        args = self._make_args(**{"project-name-cmd": "echo cmd-named-app"})
        _set_project_name(args)
        assert "-DCT_PROJECT_NAME='\"cmd-named-app\"'" in args.CPPFLAGS

    def test_explicit_version_takes_precedence_over_cmd(self):
        from compiletools.apptools import _set_project_version

        args = self._make_args(**{"project-version": "explicit-1.0", "project-version-cmd": "echo from-cmd"})
        _set_project_version(args)
        assert "-DCT_PROJECT_VERSION='\"explicit-1.0\"'" in args.CPPFLAGS
        assert "from-cmd" not in args.CPPFLAGS

    def test_idempotent_when_macro_already_present(self):
        from compiletools.apptools import _set_project_name

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
        from compiletools.apptools import _set_project_version, _unify_cpp_cxx_flags
        from compiletools.utils import split_command_cached

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
        from compiletools.apptools import _set_project_name, _unify_cpp_cxx_flags
        from compiletools.utils import split_command_cached

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
            import warnings

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
        from compiletools.apptools import compiler_default_cxx_std

        assert compiler_default_cxx_std(None) is None
        assert compiler_default_cxx_std("") is None

    def test_returns_none_for_nonexistent_compiler(self):
        from compiletools.apptools import compiler_default_cxx_std

        assert compiler_default_cxx_std("nonexistent_compiler_xyz_999") is None

    def test_returns_none_when_compiler_exits_nonzero(self):
        from compiletools.apptools import clear_cache, compiler_default_cxx_std

        clear_cache()
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert compiler_default_cxx_std("fake_cxx") is None
        clear_cache()

    def test_returns_none_when_macro_missing(self):
        from compiletools.apptools import clear_cache, compiler_default_cxx_std

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
        from compiletools.apptools import clear_cache, compiler_default_cxx_std

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
        from compiletools.apptools import clear_cache, compiler_default_cxx_std

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

    def test_prepends_when_override_dir_exists(self, monkeypatch, tmp_path):
        """When ct.conf.d/pkgconfig/ exists at gitroot, it is prepended to PKG_CONFIG_PATH."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/existing/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        pkg_config_path = os.environ["PKG_CONFIG_PATH"]
        assert pkg_config_path.startswith(str(pkgconfig_dir))
        assert "/existing/path" in pkg_config_path

    def test_noop_when_no_override_dir(self, monkeypatch, tmp_path):
        """When ct.conf.d/pkgconfig/ does not exist, PKG_CONFIG_PATH is unchanged."""
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == "/original/path"

    def test_works_when_pkg_config_path_unset(self, monkeypatch, tmp_path):
        """When PKG_CONFIG_PATH is not set, sets it to just the override dir."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == str(pkgconfig_dir)

    def test_idempotency(self, monkeypatch, tmp_path):
        """Calling twice does not duplicate the path in PKG_CONFIG_PATH."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/existing/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        first_value = os.environ["PKG_CONFIG_PATH"]

        _setup_pkg_config_overrides(ctx)
        second_value = os.environ["PKG_CONFIG_PATH"]

        assert first_value == second_value

    def test_verbose_output(self, monkeypatch, tmp_path, capsys):
        """Verbose >= 4 prints the override path."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, verbose=4)

        captured = capsys.readouterr()
        assert "Prepended pkg-config path:" in captured.out
        assert str(pkgconfig_dir) in captured.out

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

    def test_dedup_when_cwd_equals_gitroot(self, monkeypatch, tmp_path):
        """When cwd is the git root, only one entry is prepended."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
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

    def test_restore_pkg_config_path_undoes_mutation(self, monkeypatch, tmp_path):
        """Regression: BuildContext must expose a way to undo the
        global env mutation, so long-lived processes / tests using
        multiple sequential contexts don't leak PKG_CONFIG_PATH state."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
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

    def test_restore_when_pkg_config_path_was_unset(self, monkeypatch, tmp_path):
        """Restore must remove PKG_CONFIG_PATH if it was originally unset."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)
        assert "PKG_CONFIG_PATH" in os.environ

        ctx.restore_pkg_config_path()

        assert "PKG_CONFIG_PATH" not in os.environ

    def test_restore_runs_after_subprocess_exception(self, monkeypatch, tmp_path):
        """Regression: restore_pkg_config_path() must cleanly undo the
        env mutation even when a downstream pkg-config subprocess raises
        between apply and restore. Long-lived processes / tests rely on
        this for cleanup-by-context-manager / try/finally patterns."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)
        monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
        monkeypatch.chdir(tmp_path)
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


class TestVariantResolutionRespectsArgv:
    """Regression tests for the substitutions() variant-from-sys.argv bug.

    Historically _commonsubstitutions called extract_variant() with no argv,
    so it read sys.argv even when parseargs had been given a custom argv.
    This caused embedded callers and test harnesses to see args.variant
    reset to whatever sys.argv implied. Both tests below exercise the
    parseargs pipeline with argv that does NOT match sys.argv.
    """

    def setup_method(self):
        import compiletools.testhelper as uth

        uth.reset()

    def test_argv_variant_preserved_when_not_aliased(self):
        """A --variant=<canonical-name> in argv survives substitutions even
        when sys.argv does not contain that flag."""
        import compiletools.apptools as apptools
        import compiletools.compilation_database as cdb
        import compiletools.hunter
        import compiletools.testhelper as uth

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
        import compiletools.apptools as apptools
        import compiletools.compilation_database as cdb
        import compiletools.hunter
        import compiletools.testhelper as uth

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


class TestResolvedCompilerAvailable:
    """The functional-compiler auto-detect kicks in only when args.CXX is
    None. A toolchain axis (e.g. gcc.conf) sets CXX=g++ explicitly, so on
    a system without gcc the build fails late and opaquely. The check
    catches it at parseargs end with a clear pointer at the variant chain.
    """

    def test_missing_binary_raises_with_variant_hint(self):
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        args = SimpleNamespace(
            variant="gcc.debug",
            CC="this-compiler-does-not-exist-7f3a",
            CXX="this-compiler-does-not-exist-7f3a",
            LD="this-compiler-does-not-exist-7f3a",
        )
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_resolved_compiler_available(args)
        msg = str(excinfo.value)
        assert "not on PATH" in msg
        assert "gcc.debug" in msg  # variant must appear in the diagnostic

    def test_existing_binary_passes_silently(self):
        import shutil
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        real_cxx = shutil.which("g++") or shutil.which("clang++") or shutil.which("sh")
        assert real_cxx, "test environment lacks any usable executable"
        args = SimpleNamespace(variant="gcc.debug", CC=real_cxx, CXX=real_cxx, LD=real_cxx)
        # Must not raise.
        apptools._check_resolved_compiler_available(args)

    def test_unsupplied_sentinel_is_skipped(self):
        # The "unsupplied_implies_use_CXX" sentinel means a downstream
        # substitution replaces this with a real CXX value — the check
        # must not flag it as a missing binary.
        import shutil
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        real_cxx = shutil.which("g++") or shutil.which("clang++") or shutil.which("sh")
        args = SimpleNamespace(
            variant="x",
            CC="unsupplied_implies_use_CXX",
            CXX=real_cxx,
            LD="unsupplied_implies_use_CXX",
        )
        apptools._check_resolved_compiler_available(args)


class TestCompilerSupportsRequestedStandard:
    """Static (compiler, version) -> max-std table is the cheap way to
    catch "user picked cxx26 on gcc 11" before the compile error surfaces
    with no pointer at the variant chain."""

    def test_too_old_for_requested_std_raises(self, monkeypatch):
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 11))
        args = SimpleNamespace(
            variant="gcc.cxx26.debug",
            CC="g++",
            CXX="g++",
            CFLAGS="-O0",
            CXXFLAGS="-std=c++26 -O0",
        )
        with pytest.raises(RuntimeError) as excinfo:
            apptools._check_compiler_supports_requested_standard(args)
        msg = str(excinfo.value)
        assert "does not support -std=c++26" in msg
        assert "gcc >= 14" in msg

    def test_recent_compiler_passes(self, monkeypatch):
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 14))
        args = SimpleNamespace(
            variant="gcc.cxx26.debug",
            CC="g++",
            CXX="g++",
            CFLAGS="-O0",
            CXXFLAGS="-std=c++26 -O0",
        )
        # 14 >= 14 — passes.
        apptools._check_compiler_supports_requested_standard(args)

    def test_unknown_driver_skips_silently(self, monkeypatch):
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: None)
        args = SimpleNamespace(
            variant="x",
            CC="some-cross-compiler",
            CXX="some-cross-compiler",
            CFLAGS="",
            CXXFLAGS="-std=c++26",
        )
        # Unknown driver → skip silently rather than false-positive.
        apptools._check_compiler_supports_requested_standard(args)

    def test_no_std_flag_skips_silently(self, monkeypatch):
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 4))
        args = SimpleNamespace(
            variant="blank.debug",
            CC="gcc",
            CXX="g++",
            CFLAGS="-O0",
            CXXFLAGS="-O0",  # No -std=
        )
        # No -std= in flags → nothing to check.
        apptools._check_compiler_supports_requested_standard(args)

    def test_alt_spelling_cxx2c_normalised_to_cxx26(self, monkeypatch):
        # gcc <14 / clang <18 spelled C++26 as -std=c++2c. The check should
        # normalise that to c++26 for the version lookup.
        from types import SimpleNamespace

        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools, "_compiler_major_version", lambda path: ("gcc", 11))
        args = SimpleNamespace(
            variant="x",
            CC="g++",
            CXX="g++",
            CFLAGS="",
            CXXFLAGS="-std=c++2c -O0",
        )
        with pytest.raises(RuntimeError, match=r"does not support -std=c\+\+2c"):
            apptools._check_compiler_supports_requested_standard(args)


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
        import compiletools.apptools as apptools

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
        from compiletools.apptools import _has_prefix_map_flag

        for prefix in (
            "-ffile-prefix-map",
            "-fdebug-prefix-map",
            "-fmacro-prefix-map",
            "-fcanon-prefix-map",
        ):
            assert _has_prefix_map_flag(f"-O2 {prefix}=/foo=. -g")
            assert _has_prefix_map_flag(f"{prefix}=/foo=.")

    def test_negative_cases(self):
        from compiletools.apptools import _has_prefix_map_flag

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
        from compiletools.apptools import _has_prefix_map_flag

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
        from compiletools.apptools import _has_prefix_map_flag

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
        from types import SimpleNamespace

        defaults = dict(
            ffile_prefix_map_target=".",
            CXXFLAGS="-O2 -g",
            CFLAGS="-O2",
            LDFLAGS="",
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_appends_when_absent(self, monkeypatch):
        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args()
        apptools._inject_ffile_prefix_map(args)
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CXXFLAGS
        assert "-ffile-prefix-map=/home/alice/proj=." in args.CFLAGS

    def test_respects_user_override_per_slot(self, monkeypatch):
        """User-set ``-fdebug-prefix-map`` in CXXFLAGS suppresses injection
        for CXXFLAGS only; CFLAGS still gets the default."""
        import compiletools.apptools as apptools

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
        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "")
        args = self._make_args(CXXFLAGS="-O2", CFLAGS="-O2")
        apptools._inject_ffile_prefix_map(args)
        assert args.CXXFLAGS == "-O2"
        assert args.CFLAGS == "-O2"

    def test_honors_custom_target(self, monkeypatch):
        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/home/alice/proj")
        args = self._make_args(ffile_prefix_map_target="/__ct__/", CFLAGS="")
        apptools._inject_ffile_prefix_map(args)
        assert "-ffile-prefix-map=/home/alice/proj=/__ct__/" in args.CXXFLAGS

    def test_handles_empty_initial_flag_string(self, monkeypatch):
        """No leading whitespace when the slot starts empty."""
        import compiletools.apptools as apptools

        monkeypatch.setattr(apptools.compiletools.git_utils, "find_git_root", lambda: "/repo")
        args = self._make_args(CXXFLAGS="", CFLAGS="")
        apptools._inject_ffile_prefix_map(args)
        assert args.CXXFLAGS == "-ffile-prefix-map=/repo=."
        assert args.CFLAGS == "-ffile-prefix-map=/repo=."

    def test_idempotent(self, monkeypatch):
        """Second call detects its own injection and skips."""
        import compiletools.apptools as apptools

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
        import compiletools.apptools as apptools

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
