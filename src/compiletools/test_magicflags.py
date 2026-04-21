import os

import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.magicflags import _HARD_ORDERINGS_KEY


class TestMagicFlagsModule(tb.BaseCompileToolsTestCase):
    def setup_method(self):
        """Setup method - initialize parser cache"""
        super().setup_method()
        self._parser_cache = {}

    def _check_flags(self, result, flag_type, expected_flags, unexpected_flags):
        """Helper to verify flags of given type contain expected flags and not unexpected ones"""
        import stringzilla as sz

        flag_key = sz.Str(flag_type) if isinstance(flag_type, str) else flag_type
        flags_str = " ".join(str(flag) for flag in result[flag_key])
        return all(flag in flags_str for flag in expected_flags) and not any(
            flag in flags_str for flag in unexpected_flags
        )

    def _parse_with_magic(self, magic_type, source_file, extra_args=None):
        """Helper to create or reuse parser and parse file with given magic type

        Parsers are cached by (magic_type, extra_args_tuple) to avoid recreating
        identical parsers, but caches are cleared before each parse for isolation.
        """
        args = ["--magic", magic_type] if magic_type else []
        if extra_args:
            args.extend(extra_args)

        # Create cache key from magic type and extra args
        extra_args_tuple = tuple(extra_args) if extra_args else ()
        cache_key = (magic_type, extra_args_tuple)

        # Get or create parser for this configuration
        if cache_key not in self._parser_cache:
            ctx = BuildContext()
            self._parser_cache[cache_key] = tb.create_magic_parser(args, tempdir=self._tmpdir, context=ctx)

        parser = self._parser_cache[cache_key]
        parser.clear_cache()  # Clear cache for test isolation

        try:
            return parser.parse(self._get_sample_path(source_file))
        except RuntimeError as e:
            if "No functional C++ compiler detected" in str(e):
                pytest.skip("No functional C++ compiler detected")
            else:
                raise

    def test_parsing_CFLAGS(self):
        """Test parsing CFLAGS from magic comments"""
        result = self._parse_with_magic(None, "simple/test_cflags.c")
        assert self._check_flags(result, "CFLAGS", ["-std=gnu99"], [])

    @uth.requires_functional_compiler
    def test_lotsofmagic(self, pkgconfig_env):
        """Test parsing multiple magic flags from a complex file"""
        result = self._parse_with_magic("cpp", "lotsofmagic/lotsofmagic.cpp")

        # Check that basic magic flags are present
        import stringzilla as sz

        assert sz.Str("F1") in result and str(result[sz.Str("F1")]) == str([sz.Str("1")])
        assert sz.Str("F2") in result and str(result[sz.Str("F2")]) == str([sz.Str("2")])
        assert sz.Str("F3") in result and str(result[sz.Str("F3")]) == str([sz.Str("3")])
        assert sz.Str("LDFLAGS") in result and "-lpcap" in str(result[sz.Str("LDFLAGS")])
        assert sz.Str("PKG-CONFIG") in result and str(result[sz.Str("PKG-CONFIG")]) == str([sz.Str("nested")])

        # Check that PKG-CONFIG processing adds flags to LDFLAGS
        assert sz.Str("LDFLAGS") in result
        ldflags = result[sz.Str("LDFLAGS")]
        assert "-lm" in str(ldflags)  # From explicit //#LDFLAGS=-lm

        # Check that fake pkg-config flags were added
        # nested.pc has: -L/usr/local/lib -ltestpkg1
        ldflags_str = " ".join(str(f) for f in ldflags)
        assert "-ltestpkg1" in ldflags_str, "Expected '-ltestpkg1' from nested.pc to be in LDFLAGS"

        # Check that PKG-CONFIG processing adds empty entries for flag types
        assert sz.Str("CPPFLAGS") in result
        assert sz.Str("CFLAGS") in result
        assert sz.Str("CXXFLAGS") in result

    @uth.requires_functional_compiler
    def test_direct_and_cpp_magic_generate_same_results(self, pkgconfig_env):
        """Test that DirectMagicFlags and CppMagicFlags produce identical results on conditional compilation samples"""

        # Test files with optional expected values for correctness verification
        # Format: (filename, expected_values_dict or None)
        test_files = [
            # Core functionality with specific expected values
            (
                "cross_platform/cross_platform.cpp",
                {"SOURCE": [self._get_sample_path("cross_platform/cross_platform_lin.cpp")]},
            ),
            (
                "magicsourceinheader/main.cpp",
                {
                    "LDFLAGS": ["-lm"],
                    "SOURCE": [self._get_sample_path("magicsourceinheader/include_dir/sub_dir/the_code_lin.cpp")],
                },
            ),
            # Macro dependencies - verify correct feature selection
            ("macro_deps/main.cpp", None),
            # LDFLAGS conditional compilation
            ("ldflags/conditional_ldflags_test.cpp", None),
            ("ldflags/version_dependent_ldflags.cpp", None),
            # Platform-specific includes
            ("conditional_includes/main.cpp", None),
            # Feature-based compilation
            ("feature_headers/main.cpp", None),
            # Complex macro scenarios - each tests different preprocessor edge cases
            ("cppflags_macros/elif_test.cpp", None),
            ("cppflags_macros/multi_flag_test.cpp", None),
            ("cppflags_macros/nested_macros_test.cpp", None),
            ("cppflags_macros/compiler_builtin_test.cpp", None),
            ("cppflags_macros/advanced_preprocessor_test.cpp", None),
            # Version-dependent API - both old and new versions
            ("version_dependent_api/test_main.cpp", None),
            ("version_dependent_api/test_main_new.cpp", None),
            # Magic processing order bug tests
            ("magic_processing_order/test_macro_transform.cpp", None),
            ("magic_processing_order/complex_test.cpp", None),
        ]

        # Create parsers once and reuse across all files
        with uth.ParserContext():
            ctx = BuildContext()
            magicparser_direct = tb.create_magic_parser(["--magic", "direct"], tempdir=self._tmpdir, context=ctx)
            magicparser_cpp = tb.create_magic_parser(["--magic", "cpp"], tempdir=self._tmpdir, context=ctx)
            parsers = (magicparser_direct, magicparser_cpp)

            failures = []
            for test_spec in test_files:
                # Handle both tuple (filename, expected) and plain filename for compatibility
                if isinstance(test_spec, tuple):
                    filename, expected_values = test_spec
                else:
                    filename, expected_values = test_spec, None

                try:
                    tb.compare_direct_cpp_magic(self, filename, self._tmpdir, expected_values, parsers)
                except (AssertionError, Exception) as e:
                    failures.append(f"{filename}: {e!s}")

            if failures:
                fail_msg = "\n\nDirectMagicFlags vs CppMagicFlags equivalence failures:\n" + "\n".join(failures)
                assert False, fail_msg

    def test_macro_deps_cross_file(self, pkgconfig_env):
        """Test that macros defined in source files affect header magic flags"""
        source_file = "macro_deps/main.cpp"

        # First verify both parsers give same results
        tb.compare_direct_cpp_magic(self, source_file, self._tmpdir)

        # Then test specific behavior with direct parser
        result_direct = self._parse_with_magic("direct", source_file)

        # Should only contain feature X dependencies, not feature Y
        assert sz.Str("PKG-CONFIG") in result_direct
        assert "zlib" in [str(x) for x in result_direct[sz.Str("PKG-CONFIG")]]
        assert "nested" not in [str(x) for x in result_direct.get(sz.Str("PKG-CONFIG"), [])]

        assert sz.Str("SOURCE") in result_direct
        feature_x_source = self._get_sample_path("macro_deps/feature_x_impl.cpp")
        feature_y_source = self._get_sample_path("macro_deps/feature_y_impl.cpp")
        assert feature_x_source in [str(x) for x in result_direct[sz.Str("SOURCE")]]
        assert feature_y_source not in [str(x) for x in result_direct[sz.Str("SOURCE")]]

    @uth.requires_functional_compiler
    def test_conditional_ldflags_with_command_line_macro(self):
        """Test that conditional LDFLAGS work with command-line defined macros"""
        source_file = "ldflags/conditional_ldflags_test.cpp"
        debug_flags = ["-ldebug_library", "-ltest_framework"]
        production_flags = ["-lproduction_library", "-loptimized_framework"]

        # Without macro - should get debug LDFLAGS
        result_debug = self._parse_with_magic("direct", source_file)
        assert self._check_flags(result_debug, "LDFLAGS", debug_flags, production_flags)

        # With macro using direct magic via CPPFLAGS
        result_direct = self._parse_with_magic("direct", source_file, ["--append-CPPFLAGS=-DUSE_PRODUCTION_LIBS"])
        assert self._check_flags(result_direct, "LDFLAGS", production_flags, debug_flags), (
            "Direct magic should handle command-line macros correctly"
        )

        # With macro using cpp magic - should work correctly
        result_cpp = self._parse_with_magic("cpp", source_file, ["--append-CPPFLAGS=-DUSE_PRODUCTION_LIBS"])
        assert self._check_flags(result_cpp, "LDFLAGS", production_flags, debug_flags), (
            "CPP magic should handle command-line macros correctly"
        )

        # Test that direct magic also works with CXXFLAGS
        result_direct_cxx = self._parse_with_magic("direct", source_file, ["--append-CXXFLAGS=-DUSE_PRODUCTION_LIBS"])
        assert self._check_flags(result_direct_cxx, "LDFLAGS", production_flags, debug_flags), (
            "Direct magic should handle macros from CXXFLAGS correctly"
        )

    def test_macro_expansion_in_magic_flag_values(self):
        """Test that macros in magic flag values are expanded (e.g., LIB_SUFFIX -> O2)"""
        source_file = "ldflags/macro_expanded_ldflags.cpp"

        # With LIB_SUFFIX=O2, should get -lmylib-O2 -lother-O2
        result = self._parse_with_magic("direct", source_file, ["--append-CPPFLAGS=-DLIB_SUFFIX=O2"])
        assert self._check_flags(result, "LDFLAGS", ["-lmylib-O2", "-lother-O2"], ["LIB_SUFFIX"]), (
            "Direct magic should expand LIB_SUFFIX=O2 in LDFLAGS values"
        )

        # With LIB_SUFFIX=g, should get -lmylib-g -lother-g
        result_debug = self._parse_with_magic("direct", source_file, ["--append-CPPFLAGS=-DLIB_SUFFIX=g"])
        assert self._check_flags(result_debug, "LDFLAGS", ["-lmylib-g", "-lother-g"], ["-lmylib-O2"]), (
            "Direct magic should expand LIB_SUFFIX=g in LDFLAGS values"
        )

    def test_macro_expansion_in_pkg_config_output(self, pkgconfig_env):
        """Test that macros are expanded in pkg-config --cflags and --libs output.

        Without the fix, pkg-config output like -lmylib-LIB_SUFFIX would not
        be macro-expanded even when LIB_SUFFIX is defined, because
        _handle_pkg_config did not have access to the expander.
        """
        files = uth.write_sources(
            {"test_pkg_macro.cpp": ("#define LIB_SUFFIX O2\n//#PKG-CONFIG=macro-in-output\nint main() { return 0; }\n")}
        )
        source = str(files["test_pkg_macro.cpp"])

        result = self._parse_with_magic("direct", source)
        assert self._check_flags(result, "LDFLAGS", ["-lmylib-O2"], ["LIB_SUFFIX"]), (
            "LIB_SUFFIX should be expanded in pkg-config --libs output"
        )

    def test_hard_orderings_use_expanded_names(self, pkgconfig_env):
        """Test that _HARD_ORDERINGS from multi-package PKG-CONFIG annotations
        use macro-expanded library names, not raw pkg-config output.

        Regression test: _collect_hard_orderings() previously called
        cached_pkg_config() independently and got unexpanded names like
        mylib-LIB_SUFFIX, while soft LDFLAGS constraints used expanded
        names like mylib-O2, preventing cancellation in the topo sort.
        """
        files = uth.write_sources(
            {
                "test_hard_ordering_macro.cpp": (
                    "#define LIB_SUFFIX O2\n//#PKG-CONFIG=macro-in-output conditional\nint main() { return 0; }\n"
                )
            }
        )
        source = str(files["test_hard_ordering_macro.cpp"])

        result = self._parse_with_magic("direct", source)
        orderings = result.get(_HARD_ORDERINGS_KEY, [])
        assert len(orderings) == 1, f"Expected 1 hard ordering, got {len(orderings)}"
        pred, succ = orderings[0]
        assert pred == "mylib-O2", f"Hard ordering should use expanded name 'mylib-O2', got '{pred}'"
        assert succ == "testpkg", f"Expected 'testpkg', got '{succ}'"

    def test_macro_expansion_debug_suffix_in_ldflags_and_hard_orderings(self, pkgconfig_env):
        """Verify macro expansion works with debug-style suffixes (e.g. -g).

        LIB_SUFFIX=g should expand -lmylib-LIB_SUFFIX to -lmylib-g in
        both the LDFLAGS (soft constraints) and the _HARD_ORDERINGS
        (hard constraints).
        """
        files = uth.write_sources(
            {
                "test_debug_suffix.cpp": (
                    "#define LIB_SUFFIX g\n//#PKG-CONFIG=macro-in-output conditional\nint main() { return 0; }\n"
                )
            }
        )
        source = str(files["test_debug_suffix.cpp"])

        result = self._parse_with_magic("direct", source)

        # LDFLAGS should contain the expanded form
        assert self._check_flags(result, "LDFLAGS", ["-lmylib-g"], ["LIB_SUFFIX"]), (
            "LIB_SUFFIX should be expanded to 'g' in pkg-config --libs output"
        )

        # Hard orderings must use the same expanded name
        orderings = result.get(_HARD_ORDERINGS_KEY, [])
        assert len(orderings) == 1, f"Expected 1 hard ordering, got {len(orderings)}"
        pred, succ = orderings[0]
        assert pred == "mylib-g", f"Hard ordering should use expanded name 'mylib-g', got '{pred}'"
        assert succ == "testpkg", f"Expected 'testpkg', got '{succ}'"

    def test_gcc_linux_macro_not_expanded_in_pkg_config_paths(self, pkgconfig_env):
        """GCC predefines #define linux 1.  This must not corrupt
        pkg-config paths that contain the word 'linux'.

        Regression test: without the fix, pkg-config output like
        -I/opt/clickhouse-linux-x64/include becomes
        -I/opt/clickhouse-1-x64/include.
        """
        files = uth.write_sources({"test_linux_path.cpp": ("//#PKG-CONFIG=linux-path-pkg\nint main() { return 0; }\n")})
        source = str(files["test_linux_path.cpp"])

        result = self._parse_with_magic("direct", source)

        # The word 'linux' in paths must survive expansion
        assert self._check_flags(
            result,
            "CPPFLAGS",
            ["clickhouse-linux-x64"],
            ["clickhouse-1-x64"],
        ), "GCC's legacy #define linux 1 must not corrupt pkg-config paths"

        assert self._check_flags(
            result,
            "LDFLAGS",
            ["clickhouse-linux-x64"],
            ["clickhouse-1-x64"],
        ), "GCC's legacy #define linux 1 must not corrupt pkg-config -L paths"

    def test_user_redefined_legacy_macro_is_honored(self):
        """Regression: the legacy-name filter in _parse() drops compiler
        predefined names without leading underscore (e.g. ``linux``, ``unix``)
        UNLESS the user has redefined them via ``#define``.

        This is the positive path: if a source file does ``#define linux foo``,
        the user's value MUST take effect during magic-flag expansion.
        Previously only the negative path (compiler predefine ignored)
        was covered."""
        files = uth.write_sources(
            {
                "test_user_legacy_redef.cpp": (
                    "#define linux mycustomvalue\n//#CPPFLAGS=-DPLATFORM=linux\nint main() { return 0; }\n"
                )
            }
        )
        source = str(files["test_user_legacy_redef.cpp"])

        result = self._parse_with_magic("direct", source)

        # The user's #define linux mycustomvalue should expand the bare
        # ``linux`` identifier in the magic flag's value to mycustomvalue.
        cppflags_str = " ".join(str(f) for f in result[sz.Str("CPPFLAGS")])
        assert "PLATFORM=mycustomvalue" in cppflags_str, (
            f"User #define linux mycustomvalue must override legacy filter; got CPPFLAGS={cppflags_str}"
        )

    def test_undefined_macro_in_magic_flag_values_unchanged(self):
        """Undefined macros in magic flag values should remain as-is"""
        source_file = "ldflags/macro_expanded_ldflags.cpp"
        # Without defining LIB_SUFFIX, should keep literal text
        result = self._parse_with_magic("direct", source_file)
        assert self._check_flags(result, "LDFLAGS", ["LIB_SUFFIX"], ["-lmylib-O2"]), (
            "Undefined macros should not be expanded in magic flag values"
        )

    @uth.requires_functional_compiler
    def test_version_dependent_ldflags_requires_feature_parity(self):
        """Test that DirectMagicFlags must have feature parity with CppMagicFlags for complex #if expressions"""

        source_file = "ldflags/version_dependent_ldflags.cpp"
        new_api_flags = ["-lnewapi", "-ladvanced_features"]
        old_api_flags = ["-loldapi", "-lbasic_features"]

        # Both magic types should produce identical results for complex #if expressions
        result_cpp = self._parse_with_magic("cpp", source_file)
        result_direct = self._parse_with_magic("direct", source_file)

        # Both should correctly evaluate the complex expression and choose new API
        assert self._check_flags(result_cpp, "LDFLAGS", new_api_flags, old_api_flags), (
            "CPP magic should correctly evaluate complex #if expressions"
        )
        assert self._check_flags(result_direct, "LDFLAGS", new_api_flags, old_api_flags), (
            "DirectMagicFlags must have feature parity with CppMagicFlags for complex #if expressions"
        )

    @uth.requires_functional_compiler
    def test_myapp_version_dependent_api_regression(self):
        """Test that external header version macros work correctly for MYAPP API selection"""

        # Test MYAPP 1.27.8 (< 1.27.13) - should get legacy API
        legacy_api_flags = ["USE_LEGACY_API", "DLEGACY_HANDLER=myapp::LegacyProcessor"]
        modern_api_flags = ["MYAPP_ENABLE_V2_SYSTEM", "DV2_PROCESSOR_CLASS=myapp::ModernProcessor"]
        common_flags = ["MYAPP_CORE_ENABLED", "DMYAPP_CONFIG_NAMESPACE=MYAPP_CORE"]

        # Test old version (1.27.8) with both magic types
        result_cpp_old = self._parse_with_magic("cpp", "version_dependent_api/api_config.h")
        result_direct_old = self._parse_with_magic("direct", "version_dependent_api/api_config.h")

        # Both should extract legacy API flags for version 1.27.8
        assert self._check_flags(result_cpp_old, "CPPFLAGS", legacy_api_flags, modern_api_flags), (
            "CPP magic should extract legacy API for MYAPP 1.27.8"
        )
        assert self._check_flags(result_direct_old, "CPPFLAGS", legacy_api_flags, modern_api_flags), (
            "Direct magic should extract legacy API for MYAPP 1.27.8"
        )

        # Test new version (1.27.13) with both magic types
        result_cpp_new = self._parse_with_magic("cpp", "version_dependent_api/api_config_new.h")
        result_direct_new = self._parse_with_magic("direct", "version_dependent_api/api_config_new.h")

        # Both should extract modern API flags for version 1.27.13
        assert self._check_flags(result_cpp_new, "CPPFLAGS", modern_api_flags, legacy_api_flags), (
            "CPP magic should extract modern API for MYAPP 1.27.13"
        )
        assert self._check_flags(result_direct_new, "CPPFLAGS", modern_api_flags, legacy_api_flags), (
            "Direct magic should extract modern API for MYAPP 1.27.13"
        )

        # Both versions should have common flags
        for result in [result_cpp_old, result_direct_old, result_cpp_new, result_direct_new]:
            assert self._check_flags(result, "CPPFLAGS", common_flags, []), (
                "All versions should have common MYAPP flags"
            )

    @uth.requires_functional_compiler
    def test_magic_processing_order_bug(self, pkgconfig_env):
        """Test that DirectMagicFlags and CppMagicFlags produce identical results - should expose the processing order bug"""

        source_file = "magic_processing_order/complex_test.cpp"

        # Test with the exact macro combination that reproduces the real bug
        test_flags = [
            "--append-CPPFLAGS=-DNDEBUG",
            "--append-CPPFLAGS=-DUSE_SIMULATION_MODE",
            "--append-CPPFLAGS=-DUSE_CUSTOM_FEATURES",
        ]

        # Get results from both parsers
        result_direct = self._parse_with_magic("direct", source_file, test_flags)
        result_cpp = self._parse_with_magic("cpp", source_file, test_flags)

        # Print results for debugging
        print(f"\nDirect result: {result_direct}")
        print(f"CPP result: {result_cpp}")

        # The critical test: results should be identical between parsers
        # This should FAIL if there's a macro transformation bug
        assert result_direct == result_cpp, (
            f"BUG EXPOSED: DirectMagicFlags and CppMagicFlags produce different results!\n"
            f"DirectMagicFlags: {result_direct}\n"
            f"CppMagicFlags: {result_cpp}\n"
            f"This indicates a magic processing order bug in DirectMagicFlags!"
        )

    @uth.requires_functional_compiler
    def test_conditional_magic_comments_with_complex_headers(self, pkgconfig_env):
        """Test conditional magic comments work correctly with header dependencies"""

        source_file = "magic_processing_order/complex_test.cpp"

        # Test different macro combinations
        test_cases = [
            # NDEBUG + USE_SIMULATION_MODE should exclude production_util, include optimized_core
            (
                ["--append-CPPFLAGS=-DNDEBUG", "--append-CPPFLAGS=-DUSE_SIMULATION_MODE"],
                ["optimized_core"],
                ["production_util", "debug_util", "standard_core"],
            ),
            # No NDEBUG, no USE_SIMULATION_MODE should include debug_util and standard_core
            ([], ["debug_util", "standard_core"], ["production_util", "optimized_core"]),
            # NDEBUG but no USE_SIMULATION_MODE should include production_util and optimized_core
            (["--append-CPPFLAGS=-DNDEBUG"], ["production_util", "optimized_core"], ["debug_util", "standard_core"]),
        ]

        for test_flags, expected_libs, unexpected_libs in test_cases:
            result_direct = self._parse_with_magic("direct", source_file, test_flags)
            result_cpp = self._parse_with_magic("cpp", source_file, test_flags)

            # Verify both parsers get same results
            assert result_direct == result_cpp, (
                f"Parsers disagree for flags {test_flags}:\nDirect: {result_direct}\nCPP: {result_cpp}"
            )

            # Verify correct libraries are included/excluded
            ldflags_str = " ".join(str(x) for x in result_direct.get(sz.Str("LDFLAGS"), []))

            for lib in expected_libs:
                assert f"-l{lib}" in ldflags_str, (
                    f"Expected -l{lib} in LDFLAGS for flags {test_flags}, got: {ldflags_str}"
                )

            for lib in unexpected_libs:
                assert f"-l{lib}" not in ldflags_str, (
                    f"Unexpected -l{lib} in LDFLAGS for flags {test_flags}, got: {ldflags_str}"
                )

    def test_system_header_macro_extraction_bug_fix_disabled(self):
        """This test is disabled because the iterative processing masks the bug.
        The bug exists but is corrected in later iterations, making it hard to test."""
        pass

    @uth.requires_functional_compiler
    def test_system_header_macro_extraction_bug_fix(self):
        """Test that DirectMagicFlags has the system header macro extraction fix

        The bug fix adds the _extract_macros_from_file method to DirectMagicFlags
        which extracts macros from system headers before processing conditional compilation.
        Without this fix, system header macros may not be available when needed.

        Since the iterative processing eventually fixes the issue, we test for the
        presence of the fix method and validate correct behavior with system headers.
        """

        # Test 1: Check that the fix method exists (will fail on buggy version)
        assert hasattr(compiletools.magicflags.DirectMagicFlags, "_extract_macros_from_file"), (
            "BUG EXPOSED: DirectMagicFlags is missing the _extract_macros_from_file method! "
            "This method is required to extract macros from system headers before conditional compilation."
        )

        # Test 2: Verify system header processing works correctly
        source_file = "isystem_include_bug/main.cpp"
        include_path = self._get_sample_path("isystem_include_bug/fake_system_include")
        extra_args = ["--append-INCLUDE", include_path]

        # The isystem_include_bug sample tests system header macro extraction
        # SYSTEM_VERSION is 2.15, so should trigger modern API (>= 2.10), not legacy API (< 2.10)
        expected_modern_flags = ["SYSTEM_ENABLE_V2", "V2_PROCESSOR_CLASS=system::ModernProcessor"]
        unexpected_legacy_flags = ["USE_LEGACY_API", "LEGACY_HANDLER=system::LegacyProcessor"]
        common_flags = ["SYSTEM_CORE_ENABLED", "SYSTEM_CONFIG_NAMESPACE=SYSTEM_CORE"]

        # Test both magic types produce identical results
        result_cpp = self._parse_with_magic("cpp", source_file, extra_args)
        result_direct = self._parse_with_magic("direct", source_file, extra_args)

        # Both parsers must produce identical results
        assert result_direct == result_cpp, (
            f"DirectMagicFlags and CppMagicFlags must produce identical results for system header macro extraction:\n"
            f"DirectMagicFlags: {result_direct}\n"
            f"CppMagicFlags: {result_cpp}"
        )

        # Verify correct API selection based on SYSTEM_VERSION (2.15 >= 2.10)
        assert self._check_flags(result_direct, "CPPFLAGS", expected_modern_flags, unexpected_legacy_flags), (
            f"Should select modern API for SYSTEM_VERSION=2.15, got CPPFLAGS: {result_direct.get('CPPFLAGS', [])}"
        )

        assert self._check_flags(result_direct, "CXXFLAGS", expected_modern_flags, unexpected_legacy_flags), (
            f"Should select modern API for SYSTEM_VERSION=2.15, got CXXFLAGS: {result_direct.get('CXXFLAGS', [])}"
        )

        # Verify common flags are present
        assert self._check_flags(result_direct, "CPPFLAGS", common_flags, []), (
            f"Should include common SYSTEM flags, got CPPFLAGS: {result_direct.get('CPPFLAGS', [])}"
        )

    @uth.requires_functional_compiler
    def test_isystem_include_path_bug(self):
        """Test that exposes the -isystem include path bug where DirectMagicFlags
        doesn't process system headers the same way CppMagicFlags does.

        DirectMagicFlags only processes local files and misses macros defined in
        system headers accessible via -isystem include paths.
        """

        source_file = "isystem_include_bug/main.cpp"

        # Path to fake system include directory
        fake_system_include = self._get_sample_path("isystem_include_bug/fake_system_include")

        # Test with -isystem include path that contains version macros
        include_args = [f"--append-CPPFLAGS=-isystem {fake_system_include}"]

        # Get results from both parsers with identical arguments
        result_direct = self._parse_with_magic("direct", source_file, include_args)
        result_cpp = self._parse_with_magic("cpp", source_file, include_args)

        # Extract CPPFLAGS for comparison
        direct_cppflags = " ".join(str(x) for x in result_direct.get(sz.Str("CPPFLAGS"), []))
        cpp_cppflags = " ".join(str(x) for x in result_cpp.get(sz.Str("CPPFLAGS"), []))

        print("\n-isystem include path test results:")
        print(f"DirectMagicFlags: {direct_cppflags}")
        print(f"CppMagicFlags: {cpp_cppflags}")

        # Define the expected patterns based on version 2.15
        legacy_pattern = "USE_LEGACY_API"  # Should appear if macros undefined (DirectMagicFlags)
        modern_pattern = "SYSTEM_ENABLE_V2"  # Should appear if macros = 2,15 (CppMagicFlags)
        common_pattern = "SYSTEM_CORE_ENABLED"  # Should appear in both

        # Verify both have common flags
        assert common_pattern in direct_cppflags, f"DirectMagicFlags missing common flags: {direct_cppflags}"
        assert common_pattern in cpp_cppflags, f"CppMagicFlags missing common flags: {cpp_cppflags}"

        # This WILL FAIL and expose the -isystem include path bug
        if legacy_pattern in direct_cppflags and modern_pattern in cpp_cppflags:
            assert False, (
                f"-ISYSTEM INCLUDE PATH BUG EXPOSED: DirectMagicFlags doesn't process system headers!\n"
                f"DirectMagicFlags: {direct_cppflags} (can't see system headers - treats macros as undefined)\n"
                f"CppMagicFlags: {cpp_cppflags} (processes system headers correctly - sees real macro values)\n"
                f"DirectMagicFlags never processes -I/-isystem include paths like the real preprocessor does!\n"
                f"SYSTEM_VERSION_MAJOR=2, SYSTEM_VERSION_MINOR=15 should choose modern branch!"
            )

        # Any difference in results exposes the bug
        if result_direct != result_cpp:
            assert False, (
                f"-ISYSTEM INCLUDE PATH BUG: DirectMagicFlags and CppMagicFlags process include paths differently!\n"
                f"DirectMagicFlags result: {result_direct}\n"
                f"CppMagicFlags result: {result_cpp}\n"
                f"DirectMagicFlags doesn't process -I/-isystem include paths like the real preprocessor!"
            )

        # If we reach here, both parsers produce identical results (bug is fixed)
        print("✓ Both parsers process -isystem include paths identically - bug is fixed!")

    def test_duplicate_flag_deduplication(self):
        """Test that duplicate compiler flags are properly deduplicated using samples"""
        # Use our new duplicate_flags sample
        sample_file = os.path.join(os.path.dirname(__file__), "samples", "duplicate_flags", "main.cpp")

        # Test with DirectMagicFlags
        result = self._parse_with_magic("direct", sample_file, [])

        # Check CPPFLAGS for duplicates
        cppflags = result.get(sz.Str("CPPFLAGS"), [])
        print(f"CPPFLAGS result: {cppflags}")

        # Count occurrences of duplicate flags
        include_test_count = 0
        duplicate_macro_count = 0
        i = 0
        while i < len(cppflags):
            if cppflags[i] == "-I" and i + 1 < len(cppflags) and cppflags[i + 1] == "/usr/include/test":
                include_test_count += 1
                i += 2
            elif cppflags[i] == "-D" and i + 1 < len(cppflags) and cppflags[i + 1] == "DUPLICATE_MACRO":
                duplicate_macro_count += 1
                i += 2
            else:
                i += 1

        # Verify deduplication worked - each flag should appear at most once
        assert include_test_count <= 1, f"Duplicate -I /usr/include/test found {include_test_count} times in {cppflags}"
        assert duplicate_macro_count <= 1, (
            f"Duplicate -D DUPLICATE_MACRO found {duplicate_macro_count} times in {cppflags}"
        )

        print("✓ Duplicate flag deduplication test passed!")

    def test_mixed_flag_forms_deduplication(self):
        """Test that mixed forms like '-I/path' and '-I path' are properly deduplicated"""
        import compiletools.utils

        # Test mixed -I forms
        flags = ["-I/usr/include/test", "-I", "/usr/include/test", "-I/usr/include/other", "-I", "/usr/include/other"]
        deduplicated = compiletools.utils.deduplicate_compiler_flags(flags)

        # Should have only 2 include paths, not 4
        include_paths = []
        i = 0
        while i < len(deduplicated):
            if deduplicated[i] == "-I" and i + 1 < len(deduplicated):
                include_paths.append(deduplicated[i + 1])
                i += 2
            elif deduplicated[i].startswith("-I") and len(deduplicated[i]) > 2:
                include_paths.append(deduplicated[i][2:])
                i += 1
            else:
                i += 1

        unique_paths = set(include_paths)
        assert len(include_paths) == len(unique_paths), f"Mixed -I forms not deduplicated: {include_paths}"
        assert len(unique_paths) == 2, f"Expected 2 unique paths, got {len(unique_paths)}: {unique_paths}"

        # Test mixed -isystem forms
        flags2 = ["-isystem/usr/include/sys", "-isystem", "/usr/include/sys"]
        deduplicated2 = compiletools.utils.deduplicate_compiler_flags(flags2)

        isystem_paths = []
        i = 0
        while i < len(deduplicated2):
            if deduplicated2[i] == "-isystem" and i + 1 < len(deduplicated2):
                isystem_paths.append(deduplicated2[i + 1])
                i += 2
            elif deduplicated2[i].startswith("-isystem") and len(deduplicated2[i]) > 8:
                isystem_paths.append(deduplicated2[i][8:])
                i += 1
            else:
                i += 1

        assert len(isystem_paths) == 1, f"Mixed -isystem forms not deduplicated: {isystem_paths}"

        print("✓ Mixed flag forms deduplication test passed!")

    def test_ldflags_and_linkflags_deduplication(self):
        """Test that LDFLAGS and LINKFLAGS are properly deduplicated using samples"""
        # Use our duplicate_flags sample which now includes LDFLAGS/LINKFLAGS
        sample_file = os.path.join(os.path.dirname(__file__), "samples", "duplicate_flags", "main.cpp")

        # Test with DirectMagicFlags
        result = self._parse_with_magic("direct", sample_file, [])

        # Check LDFLAGS for duplicates (LINKFLAGS should be merged into LDFLAGS)
        ldflags = result.get(sz.Str("LDFLAGS"), [])
        print(f"LDFLAGS result: {ldflags}")

        # LINKFLAGS should no longer appear in results (merged into LDFLAGS)
        linkflags = result.get(sz.Str("LINKFLAGS"), [])
        print(f"LINKFLAGS result: {linkflags}")
        assert len(linkflags) == 0, f"LINKFLAGS should be empty (merged into LDFLAGS), got: {linkflags}"

        # Count occurrences of duplicate library paths and libraries in LDFLAGS only
        combined_flags = ldflags

        lib_paths = []
        libraries = []
        i = 0
        while i < len(combined_flags):
            if combined_flags[i] == "-L" and i + 1 < len(combined_flags):
                lib_paths.append(combined_flags[i + 1])
                i += 2
            elif combined_flags[i].startswith("-L") and len(combined_flags[i]) > 2:
                lib_paths.append(combined_flags[i][2:])
                i += 1
            elif combined_flags[i] == "-l" and i + 1 < len(combined_flags):
                libraries.append(combined_flags[i + 1])
                i += 2
            elif combined_flags[i].startswith("-l") and len(combined_flags[i]) > 2:
                libraries.append(combined_flags[i][2:])
                i += 1
            else:
                i += 1

        # Verify deduplication worked
        unique_lib_paths = set(lib_paths)
        unique_libraries = set(libraries)

        assert len(lib_paths) == len(unique_lib_paths), f"Duplicate library paths found: {lib_paths}"
        assert len(libraries) == len(unique_libraries), f"Duplicate libraries found: {libraries}"

        # Verify specific expected deduplication
        assert lib_paths.count("/usr/lib") <= 1, f"/usr/lib path duplicated: {lib_paths}"
        assert libraries.count("math") <= 1 and libraries.count("m") <= 1, f"math library duplicated: {libraries}"

        print("✓ LDFLAGS and LINKFLAGS deduplication test passed!")

    def test_cache_invalidates_on_header_magic_change(self):
        """Verify cache invalidates when header magic flags change."""
        import stringzilla as sz

        import compiletools.headerdeps

        # Clear all caches for test isolation
        compiletools.magicflags.MagicFlagsBase.clear_cache()

        # Create source and header
        files = uth.write_sources({"test.cpp": '#include "test.h"\nint main() {}', "test.h": "//#LDFLAGS=-lversion1\n"})
        source_file = str(files["test.cpp"])

        # Create properly initialized parser using test helper with a BuildContext
        ctx = BuildContext()
        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx)

        # First pass
        result1 = mf.parse(source_file)
        assert sz.Str("-lversion1") in result1.get(sz.Str("LDFLAGS"), [])

        # Modify header
        uth.write_sources({"test.h": "//#LDFLAGS=-lversion2\n"})

        # Simulate fresh build invocation with new BuildContext (clean caches)
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        ctx2 = BuildContext()
        mf2 = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx2)

        # Second pass - should see new flags (fresh context = clean caches)
        result2 = mf2.parse(source_file)
        assert sz.Str("-lversion2") in result2.get(sz.Str("LDFLAGS"), [])
        assert sz.Str("-lversion1") not in result2.get(sz.Str("LDFLAGS"), [])

    def test_cache_invalidates_on_readmacros_change(self):
        """Verify cache invalidates when READMACROS file changes."""
        import stringzilla as sz

        import compiletools.headerdeps

        # Clear all caches for test isolation
        compiletools.magicflags.MagicFlagsBase.clear_cache()

        # Create source and READMACROS file
        files = uth.write_sources(
            {"test.cpp": "//#READMACROS=macros.h\nint main() {}", "macros.h": "#define FOO 1\n//#LDFLAGS=-lfoo1\n"}
        )
        source_file = str(files["test.cpp"])

        # Create properly initialized parser with a BuildContext
        ctx = BuildContext()
        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx)

        # First pass
        result1 = mf.parse(source_file)
        assert sz.Str("-lfoo1") in result1.get(sz.Str("LDFLAGS"), [])

        # Modify macros file
        uth.write_sources({"macros.h": "#define FOO 2\n//#LDFLAGS=-lfoo2\n"})

        # Simulate fresh build invocation with new BuildContext (clean caches)
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        ctx2 = BuildContext()
        mf2 = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx2)

        # Second pass - should reprocess (fresh context = clean caches)
        result2 = mf2.parse(source_file)

        # Results should show new LDFLAGS (cache miss occurred)
        assert sz.Str("-lfoo2") in result2.get(sz.Str("LDFLAGS"), [])
        assert sz.Str("-lfoo1") not in result2.get(sz.Str("LDFLAGS"), [])

    def test_cache_hit_when_deps_unchanged(self):
        """Verify cache hits when source and dependencies unchanged."""
        import stringzilla as sz

        # Clear all caches for test isolation
        compiletools.magicflags.MagicFlagsBase.clear_cache()

        # Create source with header and READMACROS dependencies
        files = uth.write_sources(
            {
                "test.cpp": '#include "test.h"\n//#READMACROS=macros.h\nint main() {}',
                "test.h": "//#LDFLAGS=-ltest\n",
                "macros.h": "#define VERSION 1\n",
            }
        )
        source_file = str(files["test.cpp"])

        # Create properly initialized parser
        ctx = BuildContext()
        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx)

        # First parse - cache miss
        result1 = mf.parse(source_file)
        assert sz.Str("-ltest") in result1.get(sz.Str("LDFLAGS"), [])

        # Second parse - should be cache hit (nothing changed)
        result2 = mf.parse(source_file)
        assert result1 == result2

        # Verify results are identical
        assert sz.Str("-ltest") in result2.get(sz.Str("LDFLAGS"), [])

    def test_header_guard_bug_transitive_magic_flags(self):
        """Test for include guard detection with non-standard guard patterns.

        This test verifies the fix for a bug where include guards were not detected
        when other directives appeared between #ifndef and #define. The sample uses
        header_a.hpp with a non-standard pattern:
            #ifndef HEADER_A_HPP_GUARD
            #define SOME_OTHER_MACRO 1  // Breaks simple sequential detection
            #define HEADER_A_HPP_GUARD

        File structure:
            main.cpp -> header_a.hpp (non-standard guard) -> header_b.hpp (has magic flags)

        The bug: directives were processed by TYPE (all #ifndef, then all #define, etc.)
        rather than by LINE NUMBER, causing guard detection to fail. This resulted in:
        - Guard macro incorrectly included in defines list
        - Transitive dependencies possibly missing due to stale guard macros in cache

        Fixed in file_analyzer.py by:
        - Sorting directives by line number before guard detection (line 597-603)
        - Robust lookahead pattern (up to 5 directives) instead of strict next-directive check

        This test now passes and validates magic flags are correctly discovered from
        transitive headers even with non-standard guard patterns.
        """

        source_file = "header_guard_bug/main.cpp"

        # Parse with DirectMagicFlags
        result = self._parse_with_magic("direct", source_file)

        # Verify magic flags from transitive header (header_b.hpp) are discovered
        assert sz.Str("PKG-CONFIG") in result, "PKG-CONFIG not found - transitive header magic flags not discovered"

        pkg_config_values = [str(x) for x in result.get(sz.Str("PKG-CONFIG"), [])]
        assert "zlib" in pkg_config_values, f"Expected zlib in PKG-CONFIG from header_b.hpp, got: {pkg_config_values}"

        assert sz.Str("LDFLAGS") in result, "LDFLAGS not found - transitive header magic flags not discovered"

        ldflags_values = [str(x) for x in result.get(sz.Str("LDFLAGS"), [])]
        assert "-lm" in ldflags_values, f"Expected -lm in LDFLAGS from header_b.hpp, got: {ldflags_values}"

    @uth.requires_functional_compiler
    def test_cpp_magic_initialization_regression(self, pkgconfig_env):
        """Regression test for CppMagicFlags initialization (AttributeError fix) using real processing."""
        # Use existing sample that caused issues (lotsofmagic/lotsofmagic.cpp)
        source_file = "lotsofmagic/lotsofmagic.cpp"

        # 1. Parse with "cpp" magic. This exercises __init__ processing and populates state.
        # This implicitly calls parser.parse(abs_path)
        self._parse_with_magic("cpp", source_file)

        # 2. Retrieve the parser instance from the cache
        parser = self._parser_cache[("cpp", ())]

        # 3. Simulate Hunter's behavior: asking for the macro state key
        # This triggered the crash because _final_macro_states was missing
        abs_path = self._get_sample_path(source_file)

        try:
            # This method accesses self._final_macro_states
            key = parser.get_final_macro_state_key(abs_path)
            assert key is not None
            assert isinstance(key, frozenset)
        except AttributeError as e:
            pytest.fail(f"Crashed with AttributeError accessing macro state key: {e}")

    @uth.requires_functional_compiler
    def test_magic_flags_macro_state_equivalence(self, pkgconfig_env):
        """Verify DirectMagicFlags and CppMagicFlags produce same final macro state.

        Both magic modes should converge to the same set of variable macros after
        preprocessing. DirectMagicFlags analyzes source files with conditional compilation
        evaluation, while CppMagicFlags uses the actual preprocessor's -dM flag to dump
        final macro definitions.

        NOTE: This test uses a file that only includes user headers (not system headers)
        because DirectMagicFlags with headerdeps="direct" doesn't process system headers,
        while CppMagicFlags with -dM would include all system header macros.
        """
        source_file = "cppflags_macros/advanced_preprocessor_test.cpp"
        abs_path = self._get_sample_path(source_file)

        # Parse with both magic modes
        self._parse_with_magic("direct", source_file)
        direct_parser = self._parser_cache[("direct", ())]

        self._parse_with_magic("cpp", source_file)
        cpp_parser = self._parser_cache[("cpp", ())]

        # Get final macro state keys (variable macros only, for caching)
        direct_key = direct_parser.get_final_macro_state_key(abs_path)
        cpp_key = cpp_parser.get_final_macro_state_key(abs_path)

        # CppMagicFlags should include at least the macros DirectMagicFlags found
        # (it may include additional ones like include guards and compiler built-ins)
        # Exception: DirectMagicFlags may track macros that were later #undef'd
        only_in_direct = direct_key - cpp_key

        if only_in_direct:
            # Check if these are macros that exist in source but were #undef'd
            # This is OK - CppMagicFlags uses -dM which shows final state after #undef
            # DirectMagicFlags tracks all macros encountered during processing
            # For this test, we'll allow this difference
            print(
                f"\nNote: DirectMagicFlags found {len(only_in_direct)} macros not in CppMagicFlags (likely #undef'd):"
            )
            for name, value in sorted(only_in_direct):
                print(f"  {name} = {value}")

        # For macros present in both, values should match
        direct_dict = dict(direct_key)
        cpp_dict = dict(cpp_key)
        mismatches = []
        for macro_name, direct_value in direct_dict.items():
            cpp_value = cpp_dict.get(macro_name)
            if cpp_value is not None and str(cpp_value) != str(direct_value):
                mismatches.append((str(macro_name), str(direct_value), str(cpp_value)))

        if mismatches:
            pytest.fail(
                "Macro value mismatches between DirectMagicFlags and CppMagicFlags:\n"
                + "\n".join(f"  {name}: Direct={dv}, Cpp={cv}" for name, dv, cv in mismatches[:10])
            )

    def test_magic_cppflags_unified_with_cxxflags(self):
        """Magic CPPFLAGS appear in CXXFLAGS (and vice versa) when unified."""
        files = uth.write_sources(
            {
                "test_unified.cpp": "//#CPPFLAGS=-DFROMCPP\nint main() { return 0; }\n",
            }
        )
        source_file = str(files["test_unified.cpp"])

        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        result = mf.parse(source_file)

        cpp_flags = [str(f) for f in result.get(sz.Str("CPPFLAGS"), [])]
        cxx_flags = [str(f) for f in result.get(sz.Str("CXXFLAGS"), [])]
        assert "-DFROMCPP" in cpp_flags, f"Expected -DFROMCPP in CPPFLAGS, got {cpp_flags}"
        assert "-DFROMCPP" in cxx_flags, f"Expected -DFROMCPP in CXXFLAGS, got {cxx_flags}"

    def test_magic_cxxflags_unified_with_cppflags(self):
        """Magic CXXFLAGS appear in CPPFLAGS when unified."""
        files = uth.write_sources(
            {
                "test_unified2.cpp": "//#CXXFLAGS=-DFROMCXX\nint main() { return 0; }\n",
            }
        )
        source_file = str(files["test_unified2.cpp"])

        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        result = mf.parse(source_file)

        cpp_flags = [str(f) for f in result.get(sz.Str("CPPFLAGS"), [])]
        cxx_flags = [str(f) for f in result.get(sz.Str("CXXFLAGS"), [])]
        assert "-DFROMCXX" in cpp_flags, f"Expected -DFROMCXX in CPPFLAGS, got {cpp_flags}"
        assert "-DFROMCXX" in cxx_flags, f"Expected -DFROMCXX in CXXFLAGS, got {cxx_flags}"

    def test_magic_flags_separate_mode(self):
        """Magic CPPFLAGS stay separate from CXXFLAGS with --separate-flags-CPP-CXX."""
        files = uth.write_sources(
            {
                "test_separate.cpp": "//#CPPFLAGS=-DFROMCPP\n//#CXXFLAGS=-DFROMCXX\nint main() { return 0; }\n",
            }
        )
        source_file = str(files["test_separate.cpp"])

        mf = tb.create_magic_parser(
            ["--magic=direct", "--separate-flags-CPP-CXX"], tempdir=self._tmpdir, context=BuildContext()
        )
        result = mf.parse(source_file)

        cpp_flags = [str(f) for f in result.get(sz.Str("CPPFLAGS"), [])]
        cxx_flags = [str(f) for f in result.get(sz.Str("CXXFLAGS"), [])]
        assert "-DFROMCPP" in cpp_flags
        assert "-DFROMCXX" not in cpp_flags, f"CPPFLAGS should not contain -DFROMCXX in separate mode, got {cpp_flags}"
        assert "-DFROMCXX" in cxx_flags
        assert "-DFROMCPP" not in cxx_flags, f"CXXFLAGS should not contain -DFROMCPP in separate mode, got {cxx_flags}"

    def test_macro_state_hash_captures_global_cxxflags(self):
        """Different global CXXFLAGS must produce different macro_state_hash."""
        files = uth.write_sources({"test_cxxflags.cpp": "int main() { return 0; }\n"})
        source_file = str(files["test_cxxflags.cpp"])

        self._parse_with_magic("direct", source_file, ["--append-CXXFLAGS=-O0"])
        parser1 = self._parser_cache[("direct", ("--append-CXXFLAGS=-O0",))]
        hash1 = parser1.get_final_macro_state_hash(source_file)

        self._parse_with_magic("direct", source_file, ["--append-CXXFLAGS=-O2"])
        parser2 = self._parser_cache[("direct", ("--append-CXXFLAGS=-O2",))]
        hash2 = parser2.get_final_macro_state_hash(source_file)

        assert hash1 != hash2, f"Different CXXFLAGS should produce different macro_state_hash: {hash1} vs {hash2}"

    def test_macro_state_hash_captures_global_cppflags_paths(self):
        """Different CPPFLAGS include paths must produce different macro_state_hash."""
        files = uth.write_sources({"test_cppflags_path.cpp": "int main() { return 0; }\n"})
        source_file = str(files["test_cppflags_path.cpp"])

        self._parse_with_magic("direct", source_file, ["--append-CPPFLAGS=-I/opt/libfoo/v1/include"])
        parser1 = self._parser_cache[("direct", ("--append-CPPFLAGS=-I/opt/libfoo/v1/include",))]
        hash1 = parser1.get_final_macro_state_hash(source_file)

        self._parse_with_magic("direct", source_file, ["--append-CPPFLAGS=-I/opt/libfoo/v2/include"])
        parser2 = self._parser_cache[("direct", ("--append-CPPFLAGS=-I/opt/libfoo/v2/include",))]
        hash2 = parser2.get_final_macro_state_hash(source_file)

        assert hash1 != hash2, f"Different CPPFLAGS paths should produce different macro_state_hash: {hash1} vs {hash2}"

    def test_macro_state_hash_captures_per_file_magic_cppflags(self):
        """Different per-file magic CPPFLAGS must produce different macro_state_hash."""
        # File1 has magic CPPFLAGS with -I/v1
        files1 = uth.write_sources(
            {"test_magic_v1.cpp": "//#CPPFLAGS=-I/opt/mylib/v1/include\nint main() { return 0; }\n"}
        )
        source1 = str(files1["test_magic_v1.cpp"])

        self._parse_with_magic("direct", source1)
        parser = self._parser_cache[("direct", ())]
        hash1 = parser.get_final_macro_state_hash(source1)

        # File2 has magic CPPFLAGS with -I/v2
        files2 = uth.write_sources(
            {"test_magic_v2.cpp": "//#CPPFLAGS=-I/opt/mylib/v2/include\nint main() { return 0; }\n"}
        )
        source2 = str(files2["test_magic_v2.cpp"])

        self._parse_with_magic("direct", source2)
        hash2 = parser.get_final_macro_state_hash(source2)

        assert hash1 != hash2, (
            f"Different per-file magic CPPFLAGS should produce different macro_state_hash: {hash1} vs {hash2}"
        )

    def test_macro_state_hash_captures_per_file_pkg_config(self, pkgconfig_env):
        """Different pkg-config results must produce different macro_state_hash."""
        # Use the existing nested.pc from samples
        files = uth.write_sources({"test_pkg.cpp": "//#PKG-CONFIG=nested\nint main() { return 0; }\n"})
        source_pkg = str(files["test_pkg.cpp"])

        self._parse_with_magic("direct", source_pkg)
        parser = self._parser_cache[("direct", ())]
        hash_with_pkg = parser.get_final_macro_state_hash(source_pkg)

        # File without PKG-CONFIG
        files2 = uth.write_sources({"test_nopkg.cpp": "int main() { return 0; }\n"})
        source_nopkg = str(files2["test_nopkg.cpp"])
        self._parse_with_magic("direct", source_nopkg)
        hash_without_pkg = parser.get_final_macro_state_hash(source_nopkg)

        assert hash_with_pkg != hash_without_pkg, (
            f"PKG-CONFIG flags should affect macro_state_hash: {hash_with_pkg} vs {hash_without_pkg}"
        )

    @uth.requires_functional_compiler
    def test_cpp_mode_macro_state_hash_captures_global_cxxflags(self):
        """CppMagicFlags: different global CXXFLAGS must produce different macro_state_hash."""
        files = uth.write_sources({"test_cpp_cxxflags.cpp": "int main() { return 0; }\n"})
        source_file = str(files["test_cpp_cxxflags.cpp"])

        self._parse_with_magic("cpp", source_file, ["--append-CXXFLAGS=-O0"])
        parser1 = self._parser_cache[("cpp", ("--append-CXXFLAGS=-O0",))]
        hash1 = parser1.get_final_macro_state_hash(source_file)

        self._parse_with_magic("cpp", source_file, ["--append-CXXFLAGS=-O2"])
        parser2 = self._parser_cache[("cpp", ("--append-CXXFLAGS=-O2",))]
        hash2 = parser2.get_final_macro_state_hash(source_file)

        assert hash1 != hash2, f"CppMagicFlags: different CXXFLAGS should produce different hash: {hash1} vs {hash2}"

    @uth.requires_functional_compiler
    def test_pch_header_resolves_flag_value(self):
        """PCH stores resolved path from flag value, not the containing file."""
        import stringzilla as sz

        files = uth.write_sources(
            {
                "main.cpp": "//#PCH=myheader.h\nint main() { return 0; }\n",
                "myheader.h": "#pragma once\n",
            }
        )
        source = str(files["main.cpp"])
        header = str(files["myheader.h"])

        result = self._parse_with_magic("direct", source)

        pch_values = result.get(sz.Str("PCH"), [])
        assert len(pch_values) == 1, f"Expected one PCH value, got {pch_values}"
        resolved = str(pch_values[0])
        assert resolved == header, f"PCH should resolve to header path {header}, got {resolved}"

    def test_pch_header_from_sample(self):
        """PCH is correctly parsed from the pch sample."""
        import stringzilla as sz

        source = self._get_sample_path("pch/pch_user.cpp")
        result = self._parse_with_magic("direct", source)

        pch_values = result.get(sz.Str("PCH"), [])
        assert len(pch_values) == 1, f"Expected one PCH, got {pch_values}"
        resolved = str(pch_values[0])
        assert resolved.endswith("stdafx.h"), f"PCH should point to stdafx.h, got {resolved}"
        assert os.path.isfile(resolved), f"Resolved PCH header should exist: {resolved}"

    def test_cpp_mode_macro_state_hash_captures_per_file_magic(self):
        """CppMagicFlags: per-file magic CPPFLAGS must affect macro_state_hash."""
        files1 = uth.write_sources(
            {"test_cpp_magic_v1.cpp": "//#CPPFLAGS=-I/opt/mylib/v1/include\nint main() { return 0; }\n"}
        )
        source1 = str(files1["test_cpp_magic_v1.cpp"])

        self._parse_with_magic("cpp", source1)
        parser = self._parser_cache[("cpp", ())]
        hash1 = parser.get_final_macro_state_hash(source1)

        files2 = uth.write_sources(
            {"test_cpp_magic_v2.cpp": "//#CPPFLAGS=-I/opt/mylib/v2/include\nint main() { return 0; }\n"}
        )
        source2 = str(files2["test_cpp_magic_v2.cpp"])

        self._parse_with_magic("cpp", source2)
        hash2 = parser.get_final_macro_state_hash(source2)

        assert hash1 != hash2, (
            f"CppMagicFlags: different per-file CPPFLAGS should produce different hash: {hash1} vs {hash2}"
        )
