"""Test that reproduces the EXACT bug: initial header discovery with empty macros misses conditional includes.

ROOT CAUSE: In magicflags.py line 743:
    headers = self._headerdeps.process(filename, frozenset())

This calls headerdeps with EMPTY macro state, so:
1. g++ -MM doesn't see macros defined in conditionally-included headers
2. Conditional includes fail (e.g., #ifdef HASH_MAP_NAME in string.hpp)
3. Headers like hash_utility.hpp are missing from initial headers list
4. Convergence only processes files in all_files (built from initial headers)
5. Missing headers are NEVER discovered, even after macros converge

This test MUST FAIL to be valid.
"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.wrappedos
import compiletools.testhelper as uth
from compiletools.test_base import BaseCompileToolsTestCase


class TestEmptyMacroBug(BaseCompileToolsTestCase):
    """Test that reproduces the empty macro state bug in initial header discovery."""

    def setup_method(self):
        """Set up test environment matching the real bug scenario."""
        super().setup_method()

        # Set up temporary directory for test execution
        self.test_dir = Path(tempfile.mkdtemp(prefix="ct_test_empty_macro_"))

        # Copy sample C++ code to temp directory
        from compiletools.testhelper import samplesdir
        sample_src = Path(samplesdir()) / "empty_macro_bug"

        # Copy libs directory
        self.libs_dir = self.test_dir / "libs"
        shutil.copytree(sample_src / "libs", self.libs_dir)

        # Set up file references (paths now point to copied files)
        self.base_hpp = self.libs_dir / "base.hpp"
        self.conditional_hpp = self.libs_dir / "conditional.hpp"
        self.dependency_hpp = self.libs_dir / "dependency.hpp"
        self.main_cpp = self.libs_dir / "main.cpp"

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, 'test_dir') and self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        super().teardown_method()

    def test_empty_macro_state_causes_missing_headers(self, pkgconfig_env):
        """FAILING TEST: Demonstrates that empty macro state in initial header discovery causes missing headers.

        Expected flow WITH macros:
        1. Process main.cpp
        2. Find conditional.hpp as dependency
        3. Process conditional.hpp with USE_HASH defined (from base.hpp)
        4. Find dependency.hpp via #ifdef USE_HASH
        5. Extract PKG-CONFIG=testpkg from dependency.hpp

        Actual buggy flow with EMPTY macro state:
        1. Process main.cpp
        2. headerdeps.process(main.cpp, frozenset()) ← EMPTY MACROS!
        3. g++ -MM runs without USE_HASH defined
        4. #ifdef USE_HASH fails
        5. dependency.hpp NOT found
        6. convergence only processes files in initial headers
        7. dependency.hpp NEVER discovered
        8. PKG-CONFIG=testpkg NEVER extracted

        This test should FAIL until line 743 of magicflags.py is fixed.
        """
        # pkgconfig_env fixture already set PKG_CONFIG_PATH to samples/pkgs/

        # Create hunter
        argv = ['-vvv', f'--INCLUDE={self.test_dir}', str(self.main_cpp)]

        cap = compiletools.apptools.create_parser("test_empty_macro", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add('filename', nargs='+')

        args = compiletools.apptools.parseargs(cap, argv)

        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        print(f"\n=== Testing Empty Macro Bug ===")

        # Get header dependencies for main.cpp
        main_deps = hunter.header_dependencies(str(self.main_cpp))

        has_conditional = any('conditional.hpp' in str(h) for h in main_deps)
        has_base = any('base.hpp' in str(h) for h in main_deps)
        has_dependency = any('dependency.hpp' in str(h) for h in main_deps)

        print(f"\nHeaders found for main.cpp: {len(main_deps)}")
        print(f"  conditional.hpp: {has_conditional}")
        print(f"  base.hpp: {has_base}")
        print(f"  dependency.hpp: {has_dependency}")

        # Get magic flags to check PKG-CONFIG
        magic_flags = hunter.magicflags(str(self.main_cpp))
        import stringzilla as sz
        pkg_config_key = sz.Str('PKG-CONFIG')
        pkg_configs = [str(f) for f in magic_flags.get(pkg_config_key, [])]

        print(f"\nPKG-CONFIG flags found: {pkg_configs}")
        has_conditional_pkg = 'conditional' in pkg_configs

        # Check that CPPFLAGS were added from conditional.pc
        cppflags_key = sz.Str('CPPFLAGS')
        cppflags = [str(f) for f in magic_flags.get(cppflags_key, [])]
        cppflags_str = ' '.join(cppflags)
        has_conditional_cflags = '/usr/local/include/testpkg' in cppflags_str or 'TEST_PKG_ENABLED' in cppflags_str

        print(f"CPPFLAGS: {cppflags_str}")
        print(f"Has conditional Cflags: {has_conditional_cflags}")

        print(f"\n=== Analysis ===")
        if has_dependency and has_conditional_pkg and has_conditional_cflags:
            print(f"✓ PASS: dependency.hpp found and PKG-CONFIG extracted with correct flags")
            print(f"        The bug has been FIXED!")
        elif not has_dependency and not has_conditional_pkg:
            print(f"✗ FAIL: dependency.hpp NOT found and PKG-CONFIG NOT extracted")
            print(f"        ROOT CAUSE: Initial header discovery uses frozenset() (empty macros)")
            print(f"        LOCATION: magicflags.py line 743")
            print(f"        FIX NEEDED: Use current macro state instead of frozenset()")
        else:
            print(f"? PARTIAL: dependency.hpp={has_dependency}, PKG-CONFIG={has_conditional_pkg}, Cflags={has_conditional_cflags}")
            print(f"           Unexpected state - investigate further")

        # EXPECTED: dependency.hpp should be found because:
        # - conditional.hpp includes base.hpp which defines USE_HASH
        # - Then #ifdef USE_HASH should succeed
        # - dependency.hpp should be included
        # - PKG-CONFIG=conditional should be extracted

        assert has_conditional, "conditional.hpp should always be found (direct include)"
        assert has_base, "base.hpp should be found (included by conditional.hpp)"
        assert has_dependency, \
            f"BUG: dependency.hpp NOT found! Initial header discovery used empty macro state.\n" \
            f"     When g++ -MM processed conditional.hpp without USE_HASH defined,\n" \
            f"     the #ifdef USE_HASH failed and dependency.hpp was not included.\n" \
            f"     FIX: magicflags.py line 743 should use current macro state, not frozenset()"
        assert has_conditional_pkg, \
            f"BUG: PKG-CONFIG=conditional NOT found! dependency.hpp was not discovered, so its magic flags were not extracted."
        assert has_conditional_cflags, \
            f"BUG: conditional Cflags NOT found in CPPFLAGS! PKG-CONFIG=conditional was not processed correctly.\n" \
            f"     Expected to find '/usr/local/include/testpkg' or 'TEST_PKG_ENABLED' in: {cppflags_str}"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
