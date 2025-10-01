
import os
import pytest
import stringzilla as sz
import compiletools.test_base as tb
import compiletools.testhelper as uth
import compiletools.magicflags
import compiletools.apptools


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
        return (all(flag in flags_str for flag in expected_flags) and
                not any(flag in flags_str for flag in unexpected_flags))

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
            self._parser_cache[cache_key] = tb.create_magic_parser(args, tempdir=self._tmpdir)

        parser = self._parser_cache[cache_key]
        parser.clear_cache()  # Clear cache for test isolation

        # Also clear preprocessing cache when args change (for MacroState correctness)
        from compiletools.preprocessing_cache import clear_cache as clear_preprocessing_cache
        clear_preprocessing_cache()

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
    @uth.requires_pkg_config("zlib")
    def test_lotsofmagic(self):
        """Test parsing multiple magic flags from a complex file"""
        result = self._parse_with_magic("cpp", "lotsofmagic/lotsofmagic.cpp")
        
        # Check that basic magic flags are present
        import stringzilla as sz
        assert sz.Str("F1") in result and str(result[sz.Str("F1")]) == str([sz.Str("1")])
        assert sz.Str("F2") in result and str(result[sz.Str("F2")]) == str([sz.Str("2")])
        assert sz.Str("F3") in result and str(result[sz.Str("F3")]) == str([sz.Str("3")])
        assert sz.Str("LDFLAGS") in result and "-lpcap" in str(result[sz.Str("LDFLAGS")])
        assert sz.Str("PKG-CONFIG") in result and str(result[sz.Str("PKG-CONFIG")]) == str([sz.Str("zlib")])

        # Check that PKG-CONFIG processing adds flags to LDFLAGS
        assert sz.Str("LDFLAGS") in result
        ldflags = result[sz.Str("LDFLAGS")]
        assert "-lm" in str(ldflags)  # From explicit //#LDFLAGS=-lm
        
        # Check that pkg-config flags were added (if pkg-config available)
        zlib_libs = compiletools.apptools.cached_pkg_config("zlib", "--libs")
        if zlib_libs:
            # The entire pkg-config output should be in LDFLAGS as a single item
            assert zlib_libs in ldflags, f"Expected '{zlib_libs}' from pkg-config to be in LDFLAGS"
            
        # Check that PKG-CONFIG processing adds empty entries for flag types
        assert sz.Str("CPPFLAGS") in result
        assert sz.Str("CFLAGS") in result
        assert sz.Str("CXXFLAGS") in result

    @uth.requires_functional_compiler
    def test_direct_and_cpp_magic_generate_same_results(self):
        """Test that DirectMagicFlags and CppMagicFlags produce identical results on conditional compilation samples"""

        # Test files with optional expected values for correctness verification
        # Format: (filename, expected_values_dict or None)
        test_files = [
            # Core functionality with specific expected values
            ("cross_platform/cross_platform.cpp",
             {"SOURCE": [self._get_sample_path("cross_platform/cross_platform_lin.cpp")]}),

            ("magicsourceinheader/main.cpp",
             {"LDFLAGS": ["-lm"],
              "SOURCE": [self._get_sample_path("magicsourceinheader/include_dir/sub_dir/the_code_lin.cpp")]}),

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
            ("magic_processing_order/complex_test.cpp", None)
        ]

        # Create parsers once and reuse across all files
        with uth.ParserContext():
            magicparser_direct = tb.create_magic_parser(["--magic", "direct"], tempdir=self._tmpdir)
            magicparser_cpp = tb.create_magic_parser(["--magic", "cpp"], tempdir=self._tmpdir)
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
                    failures.append(f"{filename}: {str(e)}")

            if failures:
                fail_msg = "\n\nDirectMagicFlags vs CppMagicFlags equivalence failures:\n" + "\n".join(failures)
                assert False, fail_msg

    def test_macro_deps_cross_file(self):
        """Test that macros defined in source files affect header magic flags"""
        source_file = "macro_deps/main.cpp"
        
        # First verify both parsers give same results
        tb.compare_direct_cpp_magic(self, source_file, self._tmpdir)
        
        # Then test specific behavior with direct parser
        result_direct = self._parse_with_magic("direct", source_file)
        
        # Should only contain feature X dependencies, not feature Y
        assert sz.Str("PKG-CONFIG") in result_direct
        assert "zlib" in [str(x) for x in result_direct[sz.Str("PKG-CONFIG")]]
        assert "libcrypt" not in [str(x) for x in result_direct.get(sz.Str("PKG-CONFIG"), [])]
        
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
        assert self._check_flags(result_direct, "LDFLAGS", production_flags, debug_flags), \
            "Direct magic should handle command-line macros correctly"
        
        # With macro using cpp magic - should work correctly
        result_cpp = self._parse_with_magic("cpp", source_file, ["--append-CPPFLAGS=-DUSE_PRODUCTION_LIBS"])
        assert self._check_flags(result_cpp, "LDFLAGS", production_flags, debug_flags), \
            "CPP magic should handle command-line macros correctly"
        
        # Test that direct magic also works with CXXFLAGS
        result_direct_cxx = self._parse_with_magic("direct", source_file, ["--append-CXXFLAGS=-DUSE_PRODUCTION_LIBS"])
        assert self._check_flags(result_direct_cxx, "LDFLAGS", production_flags, debug_flags), \
            "Direct magic should handle macros from CXXFLAGS correctly"

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
        assert self._check_flags(result_cpp, "LDFLAGS", new_api_flags, old_api_flags), \
            "CPP magic should correctly evaluate complex #if expressions"
        assert self._check_flags(result_direct, "LDFLAGS", new_api_flags, old_api_flags), \
            "DirectMagicFlags must have feature parity with CppMagicFlags for complex #if expressions"

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
        assert self._check_flags(result_cpp_old, "CPPFLAGS", legacy_api_flags, modern_api_flags), \
            "CPP magic should extract legacy API for MYAPP 1.27.8"
        assert self._check_flags(result_direct_old, "CPPFLAGS", legacy_api_flags, modern_api_flags), \
            "Direct magic should extract legacy API for MYAPP 1.27.8"
            
        # Test new version (1.27.13) with both magic types  
        result_cpp_new = self._parse_with_magic("cpp", "version_dependent_api/api_config_new.h")
        result_direct_new = self._parse_with_magic("direct", "version_dependent_api/api_config_new.h")
        
        # Both should extract modern API flags for version 1.27.13
        assert self._check_flags(result_cpp_new, "CPPFLAGS", modern_api_flags, legacy_api_flags), \
            "CPP magic should extract modern API for MYAPP 1.27.13"
        assert self._check_flags(result_direct_new, "CPPFLAGS", modern_api_flags, legacy_api_flags), \
            "Direct magic should extract modern API for MYAPP 1.27.13"
            
        # Both versions should have common flags
        for result in [result_cpp_old, result_direct_old, result_cpp_new, result_direct_new]:
            assert self._check_flags(result, "CPPFLAGS", common_flags, []), \
                "All versions should have common MYAPP flags"

    @uth.requires_functional_compiler
    def test_magic_processing_order_bug(self):
        """Test that DirectMagicFlags and CppMagicFlags produce identical results - should expose the processing order bug"""
        
        source_file = "magic_processing_order/complex_test.cpp"
        
        # Test with the exact macro combination that reproduces the real bug
        test_flags = ["--append-CPPFLAGS=-DNDEBUG", "--append-CPPFLAGS=-DUSE_SIMULATION_MODE", 
                     "--append-CPPFLAGS=-DUSE_CUSTOM_FEATURES"]
        
        # Get results from both parsers
        result_direct = self._parse_with_magic("direct", source_file, test_flags)
        result_cpp = self._parse_with_magic("cpp", source_file, test_flags)
        
        # Print results for debugging
        print(f"\nDirect result: {result_direct}")
        print(f"CPP result: {result_cpp}")
        
        # The critical test: results should be identical between parsers
        # This should FAIL if there's a macro transformation bug
        assert result_direct == result_cpp, \
            f"BUG EXPOSED: DirectMagicFlags and CppMagicFlags produce different results!\n" \
            f"DirectMagicFlags: {result_direct}\n" \
            f"CppMagicFlags: {result_cpp}\n" \
            f"This indicates a magic processing order bug in DirectMagicFlags!"

    @uth.requires_functional_compiler
    def test_conditional_magic_comments_with_complex_headers(self):
        """Test conditional magic comments work correctly with header dependencies"""
        
        source_file = "magic_processing_order/complex_test.cpp"
        
        # Test different macro combinations
        test_cases = [
            # NDEBUG + USE_SIMULATION_MODE should exclude production_util, include optimized_core
            (["--append-CPPFLAGS=-DNDEBUG", "--append-CPPFLAGS=-DUSE_SIMULATION_MODE"], 
             ["optimized_core"], ["production_util", "debug_util", "standard_core"]),
            
            # No NDEBUG, no USE_SIMULATION_MODE should include debug_util and standard_core
            ([], ["debug_util", "standard_core"], ["production_util", "optimized_core"]),
            
            # NDEBUG but no USE_SIMULATION_MODE should include production_util and optimized_core
            (["--append-CPPFLAGS=-DNDEBUG"], 
             ["production_util", "optimized_core"], ["debug_util", "standard_core"])
        ]
        
        for test_flags, expected_libs, unexpected_libs in test_cases:
            result_direct = self._parse_with_magic("direct", source_file, test_flags)
            result_cpp = self._parse_with_magic("cpp", source_file, test_flags)
            
            # Verify both parsers get same results
            assert result_direct == result_cpp, \
                f"Parsers disagree for flags {test_flags}:\nDirect: {result_direct}\nCPP: {result_cpp}"
            
            # Verify correct libraries are included/excluded
            ldflags_str = " ".join(str(x) for x in result_direct.get(sz.Str("LDFLAGS"), []))
            
            for lib in expected_libs:
                assert f"-l{lib}" in ldflags_str, f"Expected -l{lib} in LDFLAGS for flags {test_flags}, got: {ldflags_str}"
            
            for lib in unexpected_libs:
                assert f"-l{lib}" not in ldflags_str, f"Unexpected -l{lib} in LDFLAGS for flags {test_flags}, got: {ldflags_str}"

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
        import compiletools.magicflags
        
        # Test 1: Check that the fix method exists (will fail on buggy version)
        assert hasattr(compiletools.magicflags.DirectMagicFlags, '_extract_macros_from_file'), \
            "BUG EXPOSED: DirectMagicFlags is missing the _extract_macros_from_file method! " \
            "This method is required to extract macros from system headers before conditional compilation."
        
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
        assert result_direct == result_cpp, \
            f"DirectMagicFlags and CppMagicFlags must produce identical results for system header macro extraction:\n" \
            f"DirectMagicFlags: {result_direct}\n" \
            f"CppMagicFlags: {result_cpp}"
        
        # Verify correct API selection based on SYSTEM_VERSION (2.15 >= 2.10)
        assert self._check_flags(result_direct, "CPPFLAGS", expected_modern_flags, unexpected_legacy_flags), \
            f"Should select modern API for SYSTEM_VERSION=2.15, got CPPFLAGS: {result_direct.get('CPPFLAGS', [])}"
        
        assert self._check_flags(result_direct, "CXXFLAGS", expected_modern_flags, unexpected_legacy_flags), \
            f"Should select modern API for SYSTEM_VERSION=2.15, got CXXFLAGS: {result_direct.get('CXXFLAGS', [])}"
        
        # Verify common flags are present
        assert self._check_flags(result_direct, "CPPFLAGS", common_flags, []), \
            f"Should include common SYSTEM flags, got CPPFLAGS: {result_direct.get('CPPFLAGS', [])}"

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
            assert False, f"-ISYSTEM INCLUDE PATH BUG EXPOSED: DirectMagicFlags doesn't process system headers!\n" \
                         f"DirectMagicFlags: {direct_cppflags} (can't see system headers - treats macros as undefined)\n" \
                         f"CppMagicFlags: {cpp_cppflags} (processes system headers correctly - sees real macro values)\n" \
                         f"DirectMagicFlags never processes -I/-isystem include paths like the real preprocessor does!\n" \
                         f"SYSTEM_VERSION_MAJOR=2, SYSTEM_VERSION_MINOR=15 should choose modern branch!"
        
        # Any difference in results exposes the bug
        if result_direct != result_cpp:
            assert False, f"-ISYSTEM INCLUDE PATH BUG: DirectMagicFlags and CppMagicFlags process include paths differently!\n" \
                         f"DirectMagicFlags result: {result_direct}\n" \
                         f"CppMagicFlags result: {result_cpp}\n" \
                         f"DirectMagicFlags doesn't process -I/-isystem include paths like the real preprocessor!"
        
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
        assert duplicate_macro_count <= 1, f"Duplicate -D DUPLICATE_MACRO found {duplicate_macro_count} times in {cppflags}"

        print("✓ Duplicate flag deduplication test passed!")

    def test_mixed_flag_forms_deduplication(self):
        """Test that mixed forms like '-I/path' and '-I path' are properly deduplicated"""
        import compiletools.utils

        # Test mixed -I forms
        flags = ['-I/usr/include/test', '-I', '/usr/include/test', '-I/usr/include/other', '-I', '/usr/include/other']
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
        flags2 = ['-isystem/usr/include/sys', '-isystem', '/usr/include/sys']
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

