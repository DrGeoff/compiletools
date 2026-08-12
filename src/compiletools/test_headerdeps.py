import os
import sys

import configargparse
import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.test_base as tb
import compiletools.testhelper as uth
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_context import BuildContext


def _make_cppflags_path(relative_path):
    """Helper to construct path within cppflags_macros sample directory."""
    return uth.example_file(f"cppflags_macros/{relative_path}")


def _make_cppflags_paths(relative_paths):
    """Helper to construct multiple paths within cppflags_macros sample directory."""
    return {_make_cppflags_path(path) for path in relative_paths}


def _assert_headers_present(result_set, expected_headers):
    """Assert that all expected headers are present in result set."""
    expected_paths = _make_cppflags_paths(expected_headers)
    assert expected_paths <= result_set, f"Missing headers: {expected_paths - result_set}"


def _assert_headers_absent(result_set, forbidden_headers):
    """Assert that none of the forbidden headers are present in result set."""
    forbidden_paths = _make_cppflags_paths(forbidden_headers)
    intersection = forbidden_paths & result_set
    assert not intersection, f"Unexpected headers found: {intersection}"


def _clean_cppflags(cppflags):
    """Remove -I{e2e_dir()} from cppflags since it's handled by --include parameter."""
    if not cppflags:
        return None
    # Remove the include path part since --include handles it
    include_pattern = f"-I{uth.e2e_dir()}"
    cleaned = cppflags.replace(include_pattern, "").strip()
    return cleaned or None


def _run_scenario_test(filename, scenarios):
    """Helper to run multiple cppflags scenarios and compare headerdeps kinds."""
    for name, cppflags in scenarios:
        cleaned_cppflags = _clean_cppflags(cppflags)
        uth.compare_headerdeps_kinds(filename, cppflags=cleaned_cppflags, scenario_name=name)


def _clear_include_env(monkeypatch):
    """Remove all CPATH-family vars so tests control exactly which are set."""
    for var in compiletools.headerdeps.INCLUDE_PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _callprocess(headerobj, filenames):
    result = []
    for filename in filenames:
        realpath = compiletools.wrappedos.realpath(filename)
        result.extend(headerobj.process(realpath, frozenset()))
    return compiletools.utils.ordered_unique(result)


def _generatecache(tempdir, name, realpaths, extraargs=None):
    if extraargs is None:
        extraargs = []

    with uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
        argv = [
            "--headerdeps",
            name,
            "--include",
            uth.ctdir(),
            "-c",
            temp_config_name,
        ] + extraargs
        cachename = os.path.join(tempdir, name)
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        ctx = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        headerdeps = compiletools.headerdeps.create(args, context=ctx)
        return cachename, _callprocess(headerdeps, realpaths)


class TestHeaderDepsModule(tb.BaseCompileToolsTestCase):
    def setup_method(self):
        super().setup_method()
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)

    @staticmethod
    def _parse_args(argv):
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)
        return compiletools.apptools.parseargs(cap, argv, context=BuildContext())

    @uth.requires_functional_compiler
    def test_direct_and_cpp_generate_same_results(self):
        filenames = [
            "factory/test_factory.cpp",
            "numbers/test_direct_include.cpp",
            "dottypaths/dottypaths.cpp",
            "calculator/main.cpp",
        ]
        for filename in filenames:
            tb.compare_direct_cpp_headers(self, self._get_sample_path(filename))

    def _direct_and_cpp_generate_same_results_ex(self, extraargs=None):
        """Test that HeaderTree and HeaderDependencies give the same results.
        Rather than polluting the real ct cache, use temporary cache
        directories.
        """
        if extraargs is None:
            extraargs = []

        # Use context manager for temp dir lifecycle
        with uth.TempDirContextNoChange() as tempdir:
            relativepaths = [
                "factory/test_factory.cpp",
                "numbers/test_direct_include.cpp",
                "simple/helloworld_c.c",
                "simple/helloworld_cpp.cpp",
                "simple/test_cflags.c",
                "calculator/main.cpp",
            ]
            realpaths = [uth.example_file(filename) for filename in relativepaths]

            _directcache, directresults = _generatecache(tempdir, "direct", realpaths, extraargs)
            _cppcache, cppresults = _generatecache(tempdir, "cpp", realpaths, extraargs)

            # The key test: both HeaderDeps implementations should produce the same results
            assert set(directresults) == set(cppresults)

    @uth.requires_functional_compiler
    def test_direct_and_cpp_generate_same_results_ex(self):
        self._direct_and_cpp_generate_same_results_ex()

    @uth.requires_functional_compiler
    def test_conditional_includes(self):
        """Test that DirectHeaderDeps correctly handles conditional includes"""
        filename = self._get_sample_path("conditional_includes/main.cpp")
        tb.compare_direct_cpp_headers(self, filename)

    @uth.requires_functional_compiler
    def test_has_include(self):
        """Test that DirectHeaderDeps evaluates __has_include via the compiler.

        The has_include sample uses __has_include to conditionally include:
        - optional_feature.h (exists locally, should be found)
        - nonexistent_feature.h (does not exist, should be skipped)
        - stdheader_extras.h (guarded by __has_include(<cstddef>), should be found)
        """
        filename = self._get_sample_path("has_include/main.cpp")
        result_set = uth.headerdeps_result(filename, "direct")
        expected = {
            self._get_sample_path("has_include/optional_feature.h"),
            self._get_sample_path("has_include/stdheader_extras.h"),
        }
        unexpected = {
            self._get_sample_path("has_include/nonexistent_feature.h"),
        }
        assert expected <= result_set, f"Missing expected headers: {expected - result_set}"
        assert not (unexpected & result_set), f"Found unexpected headers: {unexpected & result_set}"

    @uth.requires_functional_compiler
    def test_platform_has_include_source_tree(self):
        """Test that __has_include selects only one platform's headers and implied sources.

        platform_main.cpp uses __has_include(<windows.h>) / __has_include(<unistd.h>)
        to pick between windows_func.hpp and linux_func.hpp.  On Linux only the
        linux files should appear; the windows files must be absent.  The implied
        source (linux_func.cpp) should exist, confirming the compilation source tree
        contains exactly one platform.
        """
        filename = self._get_sample_path("platform_has_include/platform_main.cpp")
        result_set = uth.headerdeps_result(filename, "direct")

        linux_hpp = self._get_sample_path("platform_has_include/linux_func.hpp")
        windows_hpp = self._get_sample_path("platform_has_include/windows_func.hpp")

        # Any POSIX platform (Linux, macOS, Android/Termux, *BSD) provides
        # <unistd.h>; only Windows provides <windows.h>. The sample's
        # __has_include chain checks <windows.h> first, then <unistd.h>.
        if sys.platform != "win32":
            assert linux_hpp in result_set, f"Expected linux_func.hpp in deps: {result_set}"
            assert windows_hpp not in result_set, f"windows_func.hpp should NOT be in deps: {result_set}"

            # Verify the implied source exists (hunter would pick this up)
            linux_cpp = self._get_sample_path("platform_has_include/linux_func.cpp")
            implied = compiletools.utils.implied_source(linux_hpp)
            assert implied == linux_cpp, f"Implied source should be linux_func.cpp, got {implied}"

            # Verify no implied source would be found for the excluded platform
            windows_cpp = self._get_sample_path("platform_has_include/windows_func.cpp")
            assert windows_cpp not in result_set
        else:
            assert windows_hpp in result_set
            assert linux_hpp not in result_set

    @uth.requires_functional_compiler
    def test_user_defined_feature_headers(self):
        """Test that DirectHeaderDeps correctly handles user-defined feature macros"""
        filename = self._get_sample_path("feature_headers/main.cpp")
        tb.compare_direct_cpp_headers(self, filename)
        result_set = uth.headerdeps_result(filename, "direct")
        expected = {
            self._get_sample_path("feature_headers/feature_config.h"),
            self._get_sample_path("feature_headers/database.h"),
            self._get_sample_path("feature_headers/logging.h"),
        }
        unexpected = {
            self._get_sample_path("feature_headers/graphics.h"),
            self._get_sample_path("feature_headers/networking.h"),
        }
        assert expected <= result_set
        assert not (unexpected & result_set)

    def test_cppflags_macro_extraction(self):
        filename = self._get_sample_path("cppflags_macros/main.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags=f"-I{uth.e2e_dir()} -DENABLE_ADVANCED_FEATURES",
        )
        assert _make_cppflags_path("advanced_feature.hpp") in result_set

    def test_macro_extraction_from_all_flag_sources(self):
        filename = self._get_sample_path("cppflags_macros/multi_flag_test.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags=f"-I{uth.e2e_dir()} -DFROM_CPPFLAGS -DFROM_CFLAGS -DFROM_CXXFLAGS",
        )
        expected_headers = ["cppflags_feature.hpp", "cflags_feature.hpp", "cxxflags_feature.hpp"]
        _assert_headers_present(result_set, expected_headers)

    @uth.requires_functional_compiler
    def test_compiler_builtin_macro_recognition(self):
        filename = self._get_sample_path("cppflags_macros/compiler_builtin_test.cpp")
        result_set = uth.headerdeps_result(filename, "direct")
        import platform

        arch = platform.machine().lower()
        expected_headers = ["gcc_feature.hpp"]
        if sys.platform.startswith("linux"):
            expected_headers.append("linux_feature.hpp")
        if arch in ["x86_64", "amd64"]:
            expected_headers.append("x86_64_feature.hpp")
        elif arch.startswith("arm") and not ("64" in arch or arch.startswith("aarch")):
            expected_headers.append("arm_feature.hpp")
        elif arch.startswith("aarch") or (arch.startswith("arm") and "64" in arch):
            expected_headers.append("aarch64_feature.hpp")
        elif "riscv" in arch:
            expected_headers.append("riscv_feature.hpp")
        _assert_headers_present(result_set, expected_headers)

    def test_riscv_architecture_macro_recognition(self):
        filename = self._get_sample_path("cppflags_macros/compiler_builtin_test.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags="-D__riscv -D__riscv64__",
        )
        assert _make_cppflags_path("riscv_feature.hpp") in result_set

    def test_additional_compiler_macro_recognition(self):
        filename = self._get_sample_path("cppflags_macros/compiler_builtin_test.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags="-D_MSC_VER -D__INTEL_COMPILER -D__EMSCRIPTEN__ -D__ARMCC_VERSION",
        )
        expected_headers = ["msvc_feature.hpp", "intel_feature.hpp", "emscripten_feature.hpp", "armcc_feature.hpp"]
        _assert_headers_present(result_set, expected_headers)

    def test_elif_conditional_compilation_support(self):
        filename = self._get_sample_path("cppflags_macros/elif_test.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags="-DVERSION_2",
        )
        _assert_headers_present(result_set, ["version2_feature.hpp"])
        _assert_headers_absent(result_set, ["version1_feature.hpp", "version3_feature.hpp", "default_feature.hpp"])

    @uth.requires_functional_compiler
    def test_elif_matches_cpp_preprocessor(self):
        filename = self._get_sample_path("cppflags_macros/elif_test.cpp")
        scenarios = [
            ("VERSION_1_defined", f"-I{uth.e2e_dir()} -DVERSION_1"),
            ("VERSION_2_defined", f"-I{uth.e2e_dir()} -DVERSION_2"),
            ("VERSION_3_defined", f"-I{uth.e2e_dir()} -DVERSION_3"),
            ("no_version_defined", f"-I{uth.e2e_dir()}"),
        ]
        _run_scenario_test(filename, scenarios)

    def test_advanced_preprocessor_features(self):
        filename = self._get_sample_path("cppflags_macros/advanced_preprocessor_test.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags="-DFEATURE_A -DALT_FORM_TEST",
        )
        expected_headers = [
            "version_ge_2_feature.hpp",
            "partial_features.hpp",
            "temp_defined.hpp",
            "alt_form_feature.hpp",
            "version_205_plus.hpp",
        ]
        forbidden_headers = ["temp_still_defined.hpp", "combined_features.hpp"]
        _assert_headers_present(result_set, expected_headers)
        _assert_headers_absent(result_set, forbidden_headers)

    @uth.requires_functional_compiler
    def test_advanced_preprocessor_matches_cpp_preprocessor(self):
        filename = self._get_sample_path("cppflags_macros/advanced_preprocessor_test.cpp")
        scenarios = [
            ("FEATURE_A_and_ALT_FORM_TEST", f"-I{uth.e2e_dir()} -DFEATURE_A -DALT_FORM_TEST"),
            ("FEATURE_A_and_FEATURE_B", f"-I{uth.e2e_dir()} -DFEATURE_A -DFEATURE_B"),
            ("FEATURE_C_only", f"-I{uth.e2e_dir()} -DFEATURE_C"),
            ("no_feature_macros", f"-I{uth.e2e_dir()}"),
        ]
        _run_scenario_test(filename, scenarios)

    @uth.requires_functional_compiler
    def test_multiply_nested_macros_with_complex_logic(self):
        filename = self._get_sample_path("cppflags_macros/nested_macros_test.cpp")
        scenarios = [
            (
                "level2_linux_threading_numa",
                f"-I{uth.e2e_dir()} -DBUILD_CONFIG=2 -D__linux__ -DUSE_EPOLL=1 -DENABLE_THREADING -DTHREAD_COUNT=4 -DNUMA_SUPPORT=1",
            ),
            (
                "level3_expert_mode_with_profiling",
                f"-I{uth.e2e_dir()} -DBUILD_CONFIG=3 -DENABLE_EXPERT_MODE=1 -DCUSTOM_ALLOCATOR -DALLOCATOR_TYPE=2 -DMEMORY_TRACKING=1 -DLEAK_DETECTION=1 -DSTACK_TRACE=1 -DENABLE_PROFILING=1 -DPROFILING_LEVEL=3 -DMEMORY_PROFILING=1 -DCPU_PROFILING=1 -DCACHE_PROFILING=1",
            ),
            ("level1_basic_only", f"-I{uth.e2e_dir()} -DBUILD_CONFIG=1"),
        ]

        # Expected headers for each scenario
        scenario_expectations = {
            "level2_linux_threading_numa": {
                "expected": [
                    "basic_feature.hpp",
                    "advanced_feature.hpp",
                    "linux_advanced.hpp",
                    "linux_epoll_threading.hpp",
                    "numa_threading.hpp",
                ],
                "forbidden": [],
            },
            "level3_expert_mode_with_profiling": {
                "expected": ["basic_feature.hpp", "advanced_feature.hpp", "expert_feature.hpp"],
                "forbidden": [],
            },
            "level1_basic_only": {
                "expected": ["basic_feature.hpp"],
                "forbidden": ["advanced_feature.hpp", "expert_feature.hpp"],
            },
        }

        for name, cppflags in scenarios:
            direct = uth.compare_headerdeps_kinds(filename, cppflags=cppflags, scenario_name=name)["direct"]
            expectations = scenario_expectations[name]
            _assert_headers_present(direct, expectations["expected"])
            _assert_headers_absent(direct, expectations["forbidden"])

    def test_include_flag_parsing(self, monkeypatch):
        """Test that -I flags are parsed correctly with and without spaces"""
        # CPATH-family vars now append to deps.includes; clear them so the
        # exact-equality assertions below don't depend on the dev's shell.
        _clear_include_env(monkeypatch)
        test_cases = [
            ("-I /usr/include -I/opt/local/include", ["/usr/include", "/opt/local/include"]),
            ("-Isrc -I build/include", ["src", "build/include"]),
            ("-I src", ["src"]),
            ("-Isrc", ["src"]),
        ]

        for cppflags, expected_includes in test_cases:
            args = self._parse_args([f"--CPPFLAGS={cppflags}", "-q"])
            deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
            assert deps.includes == expected_includes, (
                f"CPPFLAGS: {cppflags}, Expected: {expected_includes}, Got: {deps.includes}"
            )

    def test_quoted_include_paths_shell_parsing_bug(self, monkeypatch):
        """Test that exposes the shell parsing bug in HeaderDeps with quoted include paths.

        This test demonstrates that the current regex-based approach fails to handle
        quoted paths with spaces correctly, just like the MagicFlags bug that was fixed.

        The regex pattern r"-(?:I)(?:\\s+|)([^\\s]+)" stops at the first whitespace,
        breaking quoted paths that should be treated as single arguments.
        """

        _clear_include_env(monkeypatch)
        # This is the critical test case that exposes the bug
        expected_includes = ["/path with spaces/include"]

        # Bypass command line parsing issues by setting the flag string
        # directly. The extraction reads the stashed BuildState's cpp
        # string, so the override goes there (a bare args.CPPFLAGS
        # mutation is deliberately inert).
        import dataclasses

        args = self._parse_args(["-q"])
        args._build_state = dataclasses.replace(args._build_state, cppflags='-I "/path with spaces/include"')

        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
        actual_includes = deps.includes

        # This assertion should now PASS after the shlex.split() fix
        assert actual_includes == expected_includes, (
            f"SHELL PARSING BUG STILL EXISTS in HeaderDeps!\n"
            f"CPPFLAGS: {args.CPPFLAGS}\n"
            f"Expected: {expected_includes} (quoted path treated as single argument)\n"
            f"Got:      {actual_includes} (shlex parsing should handle this correctly)\n"
            f"The shlex.split() fix should handle quoted paths with spaces correctly!"
        )

    def test_isystem_flag_parsing(self):
        """Test that -isystem flags are parsed correctly with and without spaces"""
        test_cases = [
            "-isystem /usr/include -isystem/opt/local/include",
            "-isystemsrc -isystem build/include",
            "-isystem src",
            "-isystemsrc",
        ]

        for cppflags in test_cases:
            args = self._parse_args([f"--CPPFLAGS={cppflags}", "-q"])
            # This should not raise an exception - the isystem parsing should work
            compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())

    def test_computed_include_no_macro(self):
        """Without COMPILETIME_INCLUDE_FILE, the #else branch includes default_extra.h."""
        filename = self._get_sample_path("computed_include/main.cpp")
        result_set = uth.headerdeps_result(filename, "direct")
        expected = self._get_sample_path("computed_include/default_extra.h")
        assert expected in result_set, f"Expected {expected} in {result_set}"

    def test_computed_include_with_commandline_macro(self):
        """With -DCOMPILETME_INCLUDE_FILE=linux_extra.h, direct should find linux_extra.h."""
        filename = self._get_sample_path("computed_include/main.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "direct",
            cppflags="-DCOMPILETIME_INCLUDE_FILE=linux_extra.h",
        )
        expected = self._get_sample_path("computed_include/linux_extra.h")
        assert expected in result_set, f"Expected {expected} in {result_set}"

    @uth.requires_functional_compiler
    def test_computed_include_with_commandline_macro_cpp(self):
        """With -DCOMPILETME_INCLUDE_FILE=linux_extra.h, cpp preprocessor should resolve it."""
        filename = self._get_sample_path("computed_include/main.cpp")
        result_set = uth.headerdeps_result(
            filename,
            "cpp",
            cppflags="-DCOMPILETIME_INCLUDE_FILE=linux_extra.h",
        )
        expected = self._get_sample_path("computed_include/linux_extra.h")
        assert expected in result_set, f"Expected {expected} in {result_set}"


class TestHeaderDepsReadsBuildState(tb.BaseCompileToolsTestCase):
    """DirectHeaderDeps' include-path extraction must read the stashed
    BuildState's cpp string, not the raw args.CPPFLAGS attr (pre-gather
    values only). Pinned by mutating the raw attr after parseargs: the include
    list must reflect the state, not the mutation."""

    def test_includes_come_from_build_state_not_raw_attr(self, monkeypatch):
        _clear_include_env(monkeypatch)
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)
        args = compiletools.apptools.parseargs(cap, ["-q", "--CPPFLAGS=-I /state/include"], context=BuildContext())
        args.CPPFLAGS = "-I /raw/mutated"
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
        assert "/state/include" in deps.includes
        assert "/raw/mutated" not in deps.includes


class TestHeaderDepsUnitTests(tb.BaseCompileToolsTestCase):
    """Unit tests for headerdeps module coverage of uncovered lines."""

    def _make_args(self, cppflags="", verbose=0):
        """Helper to create args with a fresh parser."""
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="unit test parser",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        argv = ["-q"]
        if cppflags:
            argv.append(f"--CPPFLAGS={cppflags}")
        args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())
        if verbose:
            args.verbose = verbose
        return args

    def _make_base(self, cppflags="", verbose=0):
        return compiletools.headerdeps.HeaderDepsBase(
            self._make_args(cppflags=cppflags, verbose=verbose),
            context=BuildContext(),
        )

    def test_clear_caches(self):
        """Test clear_caches resets both caches on BuildContext."""
        ctx = BuildContext()
        # The caches have strict typed keys/values in production code, but
        # clear_caches() doesn't inspect either — it just empties the dicts —
        # so a stub sentinel suffices for this test. Cast away the typing so
        # the test reads naturally; if clear_caches ever started typechecking
        # entries the assignments above would correctly fail at runtime.
        ctx.include_list_cache["dummy"] = "value"  # type: ignore[index,assignment]
        ctx.invariant_include_cache["dummy"] = "value"  # type: ignore[assignment]
        compiletools.headerdeps.clear_caches(ctx)
        assert ctx.include_list_cache == {}
        assert ctx.invariant_include_cache == {}

    def test_create_verbose(self):
        """Test create() with verbose >= 4 prints classname."""
        args = self._make_args(verbose=4)
        deps = compiletools.headerdeps.create(args, context=BuildContext())
        assert isinstance(deps, compiletools.headerdeps.DirectHeaderDeps)

    def test_base_process_impl_raises(self):
        """Test HeaderDepsBase._process_impl raises NotImplementedError."""
        base = self._make_base()
        try:
            base._process_impl("somepath", frozenset())
            assert False, "Should have raised NotImplementedError"
        except NotImplementedError:
            pass

    def test_extract_isystem_empty(self):
        """Test _extract_isystem_paths_from_flags with empty input."""
        base = self._make_base()
        assert base._extract_isystem_paths_from_flags("") == []
        assert base._extract_isystem_paths_from_flags(None) == []

    def test_extract_isystem_shlex_fallback(self):
        """_extract_isystem_paths_from_flags raises FlagTokenizeError on an
        unbalanced quote instead of silently degrading to a plain split
        (Task 9: DRY unbalanced-quote handling across every user-entered
        flag string, including this one -- consistent with every other
        flag-slot site even though production input, the state's
        shlex-joined cppflags, cannot itself be malformed)."""
        base = self._make_base()
        # Unclosed quote causes shlex ValueError
        try:
            base._extract_isystem_paths_from_flags("-isystem /usr/include 'unclosed")
            assert False, "Should have raised FlagTokenizeError"
        except compiletools.utils.FlagTokenizeError as exc:
            assert "CPPFLAGS" in str(exc)

    def test_extract_isystem_dangling(self):
        """Test -isystem at end of string with no following path."""
        base = self._make_base()
        result = base._extract_isystem_paths_from_flags("-isystem")
        assert result == []

    def test_extract_isystem_joined_format(self):
        """Test -isystem/path format (joined without space)."""
        base = self._make_base()
        result = base._extract_isystem_paths_from_flags("-isystem/usr/local/include")
        assert result == ["/usr/local/include"]

    def test_extract_include_empty(self):
        """Test _extract_include_paths_from_flags with empty input."""
        base = self._make_base()
        assert base._extract_include_paths_from_flags("") == []
        assert base._extract_include_paths_from_flags(None) == []

    def test_extract_include_list_input(self):
        """Test _extract_include_paths_from_flags with list input."""
        base = self._make_base()
        result = base._extract_include_paths_from_flags(["-I", "/usr/include", "-Ilocal"])
        assert "/usr/include" in result
        assert "local" in result

    def test_extract_include_shlex_fallback(self):
        """_extract_include_paths_from_flags raises FlagTokenizeError on an
        unbalanced quote instead of silently degrading to a plain split
        (Task 9 DRY conversion; see test_extract_isystem_shlex_fallback)."""
        base = self._make_base()
        try:
            base._extract_include_paths_from_flags("-I /usr/include 'unclosed")
            assert False, "Should have raised FlagTokenizeError"
        except compiletools.utils.FlagTokenizeError as exc:
            assert "CPPFLAGS" in str(exc)

    def test_extract_include_dangling_I(self):
        """Test -I at end of string with no following path."""
        base = self._make_base()
        result = base._extract_include_paths_from_flags("-I")
        assert result == []

    def test_cpp_header_deps_with_macro_cache_key_raises(self):
        """Test CppHeaderDeps.process raises NotImplementedError with non-empty macro_cache_key."""
        args = self._make_args()
        cpp = compiletools.headerdeps.CppHeaderDeps(args, context=BuildContext())
        try:
            cpp.process("/tmp/test.cpp", frozenset({("FOO", "1")}))
            assert False, "Should have raised NotImplementedError"
        except NotImplementedError:
            pass

    @uth.requires_functional_compiler
    def test_cpp_headerdeps_resolves_header_in_space_containing_extra_dir(self, tmp_path):
        """extra_include_dirs entries with spaces must survive to the cpp -MM
        argv as single -I tokens — the old ' '.join/re-split round-trip
        shredded '/has space/inc' into '-I/has' + 'space/inc'."""
        spaced = tmp_path / "has space" / "inc"
        spaced.mkdir(parents=True)
        (spaced / "spaced_dep.hpp").write_text("#pragma once\n")
        src = tmp_path / "consumer.cpp"
        src.write_text('#include "spaced_dep.hpp"\nint main() { return 0; }\n')

        args = self._make_args()
        deps_object = compiletools.headerdeps.CppHeaderDeps(
            args, context=BuildContext(), extra_include_dirs=[str(spaced)]
        )
        deps = deps_object.process(str(src), frozenset())
        assert any(d.endswith("spaced_dep.hpp") for d in deps), (
            f"header in space-containing extra include dir not resolved; got {deps}"
        )

    def test_direct_clear_instance_cache(self):
        """Test DirectHeaderDeps.clear_instance_cache."""
        args = self._make_args()
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
        # Just call it - should not raise
        deps.clear_instance_cache()

    def test_generatetree_with_macro_cache_key(self):
        """Test generatetree with a macro_cache_key."""
        filename = uth.example_file("simple/helloworld_cpp.cpp")
        args = self._make_args()
        ctx = BuildContext()
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=ctx)
        result = deps.generatetree(filename, macro_cache_key=frozenset())
        assert result is not None

    def test_generatetree_with_nonempty_macro_key(self):
        """Test generatetree with non-empty macro_cache_key initializes macros."""
        filename = uth.example_file("simple/helloworld_cpp.cpp")
        args = self._make_args()
        ctx = BuildContext()
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=ctx)
        result = deps.generatetree(filename, macro_cache_key=frozenset({("TEST_MACRO", "1")}))
        assert result is not None


class TestIncludePathEnvVars(tb.BaseCompileToolsTestCase):
    """DirectHeaderDeps must walk headers reachable only via the CPATH-family
    env vars so their content enters dep_hash (stale-object regression)."""

    def _make_args(self, cppflags=""):
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        argv = ["-q"]
        if cppflags:
            argv.append(f"--CPPFLAGS={cppflags}")
        return compiletools.apptools.parseargs(cap, argv, context=BuildContext())

    def _make_env_only_header_tree(self, tempdir):
        """Create main.cpp in tempdir including a header that lives only in
        tempdir/env_inc (not next to the source, not in any -I dir)."""
        incdir = os.path.join(tempdir, "env_inc")
        os.makedirs(incdir)
        header = os.path.join(incdir, "via_env.hpp")
        with open(header, "w") as f:
            f.write("#define VIA_ENV 1\n")
        src = os.path.join(tempdir, "main.cpp")
        with open(src, "w") as f:
            f.write('#include "via_env.hpp"\nint main() { return 0; }\n')
        return incdir, header, src

    def _assert_env_var_resolves_header(self, monkeypatch, varname):
        _clear_include_env(monkeypatch)
        with uth.TempDirContextNoChange() as tempdir:
            incdir, header, src = self._make_env_only_header_tree(tempdir)
            monkeypatch.setenv(varname, incdir)
            args = self._make_args()
            deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
            result = deps.process(src, frozenset())
            expected = compiletools.wrappedos.realpath(header)
            assert expected in result, f"{varname}-only header not walked: {result}"

    def test_cpath_only_header_is_walked(self, monkeypatch):
        self._assert_env_var_resolves_header(monkeypatch, "CPATH")

    def test_cplus_include_path_only_header_is_walked(self, monkeypatch):
        self._assert_env_var_resolves_header(monkeypatch, "CPLUS_INCLUDE_PATH")

    def test_env_dirs_ordered_after_cppflags_and_empty_entries_skipped(self, monkeypatch):
        """CPPFLAGS -I dirs come first (they win a name collision), CPATH next,
        the language vars after; empty os.pathsep entries are skipped."""
        _clear_include_env(monkeypatch)
        monkeypatch.setenv("CPATH", f"{os.pathsep}/env/cpath_a{os.pathsep}{os.pathsep}/env/cpath_b{os.pathsep}")
        monkeypatch.setenv("CPLUS_INCLUDE_PATH", "/env/cplus")
        args = self._make_args(cppflags="-I/flags/one -I /flags/two")
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
        assert deps.includes == [
            "/flags/one",
            "/flags/two",
            "/env/cpath_a",
            "/env/cpath_b",
            "/env/cplus",
        ]

    def test_unset_env_leaves_includes_untouched(self, monkeypatch):
        _clear_include_env(monkeypatch)
        args = self._make_args(cppflags="-I/flags/one")
        deps = compiletools.headerdeps.DirectHeaderDeps(args, context=BuildContext())
        assert deps.includes == ["/flags/one"]


class TestConditionalBlindWalk(tb.BaseCompileToolsTestCase):
    """DirectHeaderDeps.process_conditional_blind: every branch, no evaluation.

    The declaration-harvesting walk fetch's ``//#GIT=`` scan uses. Its whole
    point is to disagree with ``process`` on conditionals, so every test here
    contrasts the two.
    """

    def _make_args(self, tmpdir, verbose=0):
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="conditional-blind walk parser",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        argv = ["-q", f"--CPPFLAGS=-I{tmpdir}"]
        args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())
        if verbose:
            args.verbose = verbose
        return args

    def _make_deps(self, tmpdir):
        return compiletools.headerdeps.DirectHeaderDeps(self._make_args(tmpdir), context=BuildContext())

    @staticmethod
    def _write(tmpdir, name, text):
        path = os.path.join(tmpdir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return compiletools.wrappedos.realpath(path)

    def test_both_branches_of_an_if_else_are_walked(self):
        """The discriminating shape: process takes one branch, the blind walk
        takes both. An `in` assertion on either alone would pass on the
        evaluating walk, so this asserts on the taken AND the untaken side."""
        with uth.TempDirContextNoChange() as tmpdir:
            taken = self._write(tmpdir, "taken.h", "#pragma once\n")
            untaken = self._write(tmpdir, "untaken.h", "#pragma once\n")
            main = self._write(
                tmpdir,
                "main.cpp",
                '#if 1\n#include "taken.h"\n#else\n#include "untaken.h"\n#endif\nint main() { return 0; }\n',
            )

            deps = self._make_deps(tmpdir)
            evaluated = set(deps.process(main, frozenset()))
            blind = set(deps.process_conditional_blind(main))

            assert taken in evaluated and untaken not in evaluated, "precondition: process picks one branch"
            assert taken in blind and untaken in blind, "the blind walk must take every branch"

    def test_a_header_reachable_only_through_a_dead_branch_is_walked(self):
        """`#if 0` is the extreme case: process reaches nothing, the blind walk
        reaches the header and everything it transitively includes."""
        with uth.TempDirContextNoChange() as tmpdir:
            deep = self._write(tmpdir, "deep.h", "#pragma once\n")
            dead = self._write(tmpdir, "dead.h", '#pragma once\n#include "deep.h"\n')
            main = self._write(tmpdir, "main.cpp", '#if 0\n#include "dead.h"\n#endif\nint main() { return 0; }\n')

            deps = self._make_deps(tmpdir)
            assert deps.process(main, frozenset()) == [], "precondition: the dead branch is not followed"

            blind = set(deps.process_conditional_blind(main))
            assert dead in blind
            assert deep in blind, "the walk must recurse through the dead branch, not just enter it"

    def test_an_include_cycle_terminates(self):
        """Include guards are conditionals, and this walk ignores conditionals —
        so the visited set is the only thing preventing infinite recursion."""
        with uth.TempDirContextNoChange() as tmpdir:
            a = self._write(tmpdir, "a.h", '#ifndef A_H\n#define A_H\n#include "b.h"\n#endif\n')
            b = self._write(tmpdir, "b.h", '#ifndef B_H\n#define B_H\n#include "a.h"\n#endif\n')
            main = self._write(tmpdir, "main.cpp", '#include "a.h"\nint main() { return 0; }\n')

            blind = self._make_deps(tmpdir).process_conditional_blind(main)

            assert set(blind) == {a, b}
            assert len(blind) == 2, f"each file must appear once, got {blind}"

    def test_the_walk_excludes_the_starting_file_and_unresolvable_includes(self):
        """Contract parity with process: the target itself is not in its own
        closure, and an include that resolves nowhere (a system header, or one
        inside a not-yet-fetched external) contributes nothing instead of
        raising."""
        with uth.TempDirContextNoChange() as tmpdir:
            real = self._write(tmpdir, "real.h", "#pragma once\n")
            main = self._write(
                tmpdir,
                "main.cpp",
                '#include "real.h"\n#include "not_fetched_yet.h"\n#include <cstdio>\nint main() { return 0; }\n',
            )

            blind = self._make_deps(tmpdir).process_conditional_blind(main)

            assert blind == [real]
            assert main not in blind

    def test_the_blind_walk_leaves_macro_state_untouched(self):
        """It runs no preprocessing, so a process() call after a blind walk must
        answer exactly as it would have without one. If the blind walk leaked
        #defines into defined_macros, the guarded branch selection would move."""
        with uth.TempDirContextNoChange() as tmpdir:
            self._write(tmpdir, "on.h", "#pragma once\n")
            self._write(tmpdir, "off.h", "#pragma once\n")
            # switch.h only #defines FEATURE inside a branch the blind walk also
            # enters; a leak would flip main.cpp's selection below.
            self._write(tmpdir, "switch.h", "#pragma once\n#if 0\n#define FEATURE 1\n#endif\n")
            main = self._write(
                tmpdir,
                "main.cpp",
                '#include "switch.h"\n#ifdef FEATURE\n#include "on.h"\n#else\n#include "off.h"\n#endif\n',
            )

            baseline = self._make_deps(tmpdir).process(main, frozenset())

            deps = self._make_deps(tmpdir)
            deps.process_conditional_blind(main)
            after_blind = deps.process(main, frozenset())

            assert after_blind == baseline


class TestIncludeListCacheIsImmutable(tb.BaseCompileToolsTestCase):
    """``_create_include_list`` returns its cache entry by identity.

    Both halves of the entry are shared with every later caller for the same
    key — the ``FileEffects`` half is deeply immutable, and the include list
    must be too, or one consumer's edit rewrites the include graph every
    other traversal sees.
    """

    def _make_deps(self, tmpdir, context):
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="include-list immutability parser",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.headerdeps.add_arguments(cap)
        args = compiletools.apptools.parseargs(cap, ["-q", f"--CPPFLAGS=-I{tmpdir}"], context=context)
        return compiletools.headerdeps.DirectHeaderDeps(args, context=context)

    @staticmethod
    def _write(tmpdir, name, text):
        path = os.path.join(tmpdir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return compiletools.wrappedos.realpath(path)

    def test_a_warm_include_list_is_shared_and_rejects_mutation(self):
        """The identity assertion is the precondition: without the sharing
        there would be nothing for the mutation to corrupt."""
        with uth.TempDirContextNoChange() as tmpdir:
            self._write(tmpdir, "header.h", "#pragma once\n")
            main = self._write(tmpdir, "main.cpp", '#include "header.h"\nint main() { return 0; }\n')

            context = BuildContext()
            deps = self._make_deps(tmpdir, context)
            cold = deps._create_include_list(main)
            warm = deps._create_include_list(main)

            assert warm is cold, "precondition: the cache entry is served by identity"
            with pytest.raises(AttributeError):
                cold.append("hijacked.h")  # type: ignore[attr-defined]

            assert [str(inc) for inc in warm] == ["header.h"]
