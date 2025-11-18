"""Test that PKG-CONFIG flags are properly extracted from headers with macros.

This test reproduces a bug where PKG-CONFIG flags in headers are not properly
included in the compiler flags when the header contains macros that affect
conditional compilation.

The bug occurs when:
1. Header A includes Header B
2. Header A defines macros via #define
3. Header B contains a #PKG-CONFIG directive
4. The caching system doesn't properly track the dependency on Header B

Regression test for: https://github.com/yourcompany/compiletools/issues/XXXX
"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.wrappedos
import compiletools.global_hash_registry
from compiletools.test_base import BaseCompileToolsTestCase


class TestPkgConfigHeaderDeps(BaseCompileToolsTestCase):
    """Test PKG-CONFIG extraction from headers with macro dependencies."""

    def setup_method(self):
        """Set up test environment."""
        super().setup_method()

        # Set up temporary directory for test execution
        self.test_dir = Path(tempfile.mkdtemp(prefix="ct_test_"))

        # Copy sample C++ code to temp directory (needed for cache invalidation test)
        from compiletools.testhelper import samplesdir
        sample_src = Path(samplesdir()) / "pkg_config_header_deps"

        # Copy directory structure
        self.src_dir = self.test_dir / "src"
        shutil.copytree(sample_src / "src", self.src_dir)

        self.libs_dir = self.test_dir / "libs"
        shutil.copytree(sample_src / "libs", self.libs_dir)

        # Set up file references
        self.header_with_macros = self.libs_dir / "header_with_macros.hpp"
        self.header_with_pkgconfig = self.libs_dir / "header_with_pkgconfig.hpp"
        self.main_header = self.libs_dir / "main_header.hpp"
        self.source_file = self.src_dir / "test.cpp"

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, 'test_dir') and self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        super().teardown_method()

    def test_pkgconfig_extracted_from_nested_header(self, pkgconfig_env):
        """Test that PKG-CONFIG is extracted from headers included after macro definitions.

        This test verifies that when:
        1. Source includes main_header.hpp
        2. main_header.hpp includes header_with_macros.hpp (defines macros)
        3. main_header.hpp includes header_with_pkgconfig.hpp (has #PKG-CONFIG=testpkg1)

        The PKG-CONFIG=testpkg1 directive is properly extracted and included in the flags.
        """
        # pkgconfig_env fixture already set PKG_CONFIG_PATH to samples/pkgs/

        # Set up arguments
        argv = [
            '-vvvv',
            '--magic=direct',
            '--headerdeps=direct',
            f'--INCLUDE={self.test_dir}',
            str(self.source_file)
        ]

        cap = compiletools.apptools.create_parser("Test PKG-CONFIG", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        cap.add("filename", help="File to analyze", nargs="+")

        # Change to test directory
        original_cwd = os.getcwd()
        try:
            os.chdir(self.test_dir)

            args = compiletools.apptools.parseargs(cap, argv)

            # Create headerdeps and magicflags
            headerdeps = compiletools.headerdeps.create(args)
            magicflags = compiletools.magicflags.create(args, headerdeps)

            # Get magic flags
            flags = magicflags.parse(str(self.source_file.resolve()))

            # Verify PKG-CONFIG was extracted (keys are StringZilla strings)
            import stringzilla as sz
            pkg_config_key = sz.Str('PKG-CONFIG')
            assert pkg_config_key in flags, f"PKG-CONFIG key missing from flags. Keys: {list(flags.keys())}"
            pkg_configs = [str(f) for f in flags[pkg_config_key]]
            assert 'nested' in pkg_configs, f"PKG-CONFIG=nested not found. Got: {pkg_configs}"

            # Verify nested Cflags were added
            cppflags_key = sz.Str('CPPFLAGS')
            assert cppflags_key in flags, "CPPFLAGS missing from flags"
            cppflags_str = ' '.join(str(f) for f in flags[cppflags_key])
            assert '/usr/local/include/testpkg1' in cppflags_str or 'TEST_PKG1_ENABLED' in cppflags_str, \
                f"nested Cflags not in CPPFLAGS: {cppflags_str}"

        finally:
            os.chdir(original_cwd)

    def test_cache_invalidation_after_header_change(self, pkgconfig_env):
        """Test that cache is properly invalidated when a nested header changes.

        This tests the regression where changing a nested header's PKG-CONFIG
        directive doesn't invalidate the cache properly.

        BUGFIX: The bug was in global_hash_registry.clear_global_registry() which
        cleared _HASHES and _REVERSE_HASHES but not the LRU cache on get_file_hash().
        This caused get_file_hash() to return cached hashes that weren't registered
        in _REVERSE_HASHES, leading to FileNotFoundError in get_filepath_by_hash().
        """
        # pkgconfig_env fixture already set PKG_CONFIG_PATH to samples/pkgs/

        # Set up arguments
        argv = [
            '--magic=direct',
            '--headerdeps=direct',
            f'--INCLUDE={self.test_dir}',
            str(self.source_file)
        ]

        cap = compiletools.apptools.create_parser("Test cache invalidation", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        cap.add("filename", help="File to analyze", nargs="+")

        original_cwd = os.getcwd()
        try:
            os.chdir(self.test_dir)

            args = compiletools.apptools.parseargs(cap, argv)

            # First pass - extract flags with nested
            headerdeps1 = compiletools.headerdeps.create(args)
            magicflags1 = compiletools.magicflags.create(args, headerdeps1)
            flags1 = magicflags1.parse(str(self.source_file.resolve()))

            # Verify nested is present
            import stringzilla as sz
            pkg_config_key = sz.Str('PKG-CONFIG')
            assert pkg_config_key in flags1
            pkg_configs1 = [str(f) for f in flags1[pkg_config_key]]
            assert 'nested' in pkg_configs1

            # Clear global hash registry to simulate new build
            compiletools.global_hash_registry.clear_global_registry()
            # Also clear the magicflags cache
            compiletools.magicflags.MagicFlagsBase.clear_cache()

            # Modify the header_with_pkgconfig to use modified instead of nested
            self.header_with_pkgconfig.write_text("""
#ifndef HEADER_WITH_PKGCONFIG_HPP
#define HEADER_WITH_PKGCONFIG_HPP

//#PKG-CONFIG=modified
void testpkg2_function();

#endif
""")

            # Second pass - should pick up the change
            headerdeps2 = compiletools.headerdeps.create(args)
            magicflags2 = compiletools.magicflags.create(args, headerdeps2)
            flags2 = magicflags2.parse(str(self.source_file.resolve()))

            # Verify modified is present and nested is NOT present
            pkg_config_key = sz.Str('PKG-CONFIG')
            assert pkg_config_key in flags2
            pkg_configs2 = [str(f) for f in flags2[pkg_config_key]]
            assert 'modified' in pkg_configs2, f"PKG-CONFIG=modified not found after change. Got: {pkg_configs2}"
            assert 'nested' not in pkg_configs2, f"PKG-CONFIG=nested still present after change. Got: {pkg_configs2}"

        finally:
            os.chdir(original_cwd)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
