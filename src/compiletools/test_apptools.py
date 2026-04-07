"""Unit tests for apptools.py utility functions."""

import io
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import configargparse
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
            verbose=0, CXX="g++", CXXFLAGS="-Wall",
            CPP="unsupplied_implies_use_CXX",
            CPPFLAGS="unsupplied_implies_use_CXXFLAGS",
        )
        _substitute_CXX_for_missing(args)
        assert args.CPP == "g++"
        assert args.CPPFLAGS == "-Wall"

    def test_substitutes_ld_when_present(self):
        args = SimpleNamespace(
            verbose=0, CXX="g++", CXXFLAGS="-Wall",
            CPP="g++", CPPFLAGS="-Wall",
            LD="unsupplied_implies_use_CXX",
            LDFLAGS="unsupplied_implies_use_CXXFLAGS",
        )
        _substitute_CXX_for_missing(args)
        assert args.LD == "g++"
        assert args.LDFLAGS == "-Wall"

    def test_no_ld_attribute_ok(self):
        args = SimpleNamespace(
            verbose=0, CXX="g++", CXXFLAGS="-Wall",
            CPP="g++", CPPFLAGS="-Wall",
        )
        _substitute_CXX_for_missing(args)  # Should not raise


class TestAddIncludePathsToFlags:
    def test_adds_include_paths(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc", CPPFLAGS="-Wall", CFLAGS="-Wall", CXXFLAGS="-Wall", verbose=0,
        )
        _add_include_paths_to_flags(args)
        assert "-I /tmp/inc" in args.CPPFLAGS
        assert "-I /tmp/inc" in args.CFLAGS
        assert "-I /tmp/inc" in args.CXXFLAGS

    def test_no_duplicate_include_paths(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc", CPPFLAGS="-Wall /tmp/inc", CFLAGS="-Wall", CXXFLAGS="-Wall", verbose=0,
        )
        _add_include_paths_to_flags(args)
        # /tmp/inc already in CPPFLAGS, should not add again
        assert args.CPPFLAGS.count("/tmp/inc") == 1

    def test_verbose_include_print(self):
        args = SimpleNamespace(
            INCLUDE="/tmp/inc", CPPFLAGS="-Wall", CFLAGS="-Wall", CXXFLAGS="-Wall", verbose=6,
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


class TestDoXxpend:
    def test_prepend(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall", prepend_cppflags=["-O2"], append_cppflags=[], verbose=0,
        )
        _do_xxpend(args, "CPPFLAGS")
        assert args.CPPFLAGS.startswith("-O2")

    def test_append(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall", prepend_cppflags=[], append_cppflags=["-O2"], verbose=0,
        )
        _do_xxpend(args, "CPPFLAGS")
        assert args.CPPFLAGS.endswith("-O2")

    def test_no_duplicate_xxpend(self):
        args = SimpleNamespace(
            CPPFLAGS="-Wall -O2", prepend_cppflags=["-O2"], append_cppflags=[], verbose=0,
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
            CPPFLAGS="-DFOO", CXXFLAGS="-Wall", separate_flags_CPP_CXX=False,
        )
        _unify_cpp_cxx_flags(args)
        assert args.CPPFLAGS == args.CXXFLAGS
        assert "-DFOO" in args.CPPFLAGS
        assert "-Wall" in args.CPPFLAGS

    def test_separate_flags_skips(self):
        args = SimpleNamespace(
            CPPFLAGS="-DFOO", CXXFLAGS="-Wall", separate_flags_CPP_CXX=True,
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
        assert "obj" in args.objdir

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
            "Name: TestPkg\nDescription: Base\nVersion: 1.0\n"
            "Cflags: -I/base/include -DBASE\n"
            "Libs: -L/base/lib -lbase\n"
        )

        (override_dir / "testoverridepkg.pc").write_text(pc_content_override)
        (base_dir / "testoverridepkg.pc").write_text(pc_content_base)

        monkeypatch.setenv(
            "PKG_CONFIG_PATH", f"{override_dir}{os.pathsep}{base_dir}"
        )

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
        """When ct.conf.d/pkgconfig/ exists, it is prepended to PKG_CONFIG_PATH."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path)
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", "/existing/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        pkg_config_path = os.environ["PKG_CONFIG_PATH"]
        assert pkg_config_path.startswith(str(pkgconfig_dir))
        assert "/existing/path" in pkg_config_path

    def test_noop_when_no_override_dir(self, monkeypatch, tmp_path):
        """When ct.conf.d/pkgconfig/ does not exist, PKG_CONFIG_PATH is unchanged."""
        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path)
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == "/original/path"

    def test_works_when_pkg_config_path_unset(self, monkeypatch, tmp_path):
        """When PKG_CONFIG_PATH is not set, sets it to just the override dir."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path)
        )
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx)

        assert os.environ["PKG_CONFIG_PATH"] == str(pkgconfig_dir)

    def test_idempotency(self, monkeypatch, tmp_path):
        """Calling twice does not duplicate the path in PKG_CONFIG_PATH."""
        pkgconfig_dir = tmp_path / "ct.conf.d" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path)
        )
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

        monkeypatch.setattr(
            "compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path)
        )
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        _setup_pkg_config_overrides(ctx, verbose=4)

        captured = capsys.readouterr()
        assert "Prepended project pkg-config overrides" in captured.out
        assert str(pkgconfig_dir) in captured.out
