"""Test that reproduces the EXACT bug: initial header discovery with empty macros misses conditional includes.

ROOT CAUSE: In magicflags.py line 743:
    headers = self._headerdeps.process(filename, frozenset())

This calls headerdeps with EMPTY macro state, so:
1. g++ -MM doesn't see macros defined in conditionally-included headers
2. Conditional includes fail (e.g., #ifdef HASH_MAP_NAME in string.hpp)
3. Headers like hash_utility.hpp are missing from initial headers list
4. Convergence only processes files in all_files (built from initial headers)
5. Missing headers are NEVER discovered, even after macros converge

The bug has been fixed — this test now passes, verifying the fix works correctly.
"""

import shutil
from pathlib import Path

import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path
from compiletools.test_base import BaseCompileToolsTestCase


class TestEmptyMacroBug(BaseCompileToolsTestCase):
    """Test that reproduces the empty macro state bug in initial header discovery."""

    def setup_method(self):
        """Copy sample C++ code into the per-test tmp dir provided by the base class."""
        super().setup_method()
        self.test_dir = Path(self._tmpdir)
        sample_src = Path(example_path("empty_macro_bug"))
        self.libs_dir = self.test_dir / "libs"
        shutil.copytree(sample_src / "libs", self.libs_dir)
        self.base_hpp = self.libs_dir / "base.hpp"
        self.conditional_hpp = self.libs_dir / "conditional.hpp"
        self.dependency_hpp = self.libs_dir / "dependency.hpp"
        self.main_cpp = self.libs_dir / "main.cpp"

    @pytest.mark.usefixtures("pkgconfig_env")
    def test_empty_macro_state_causes_missing_headers(self):
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
        argv = ["-vvv", f"--INCLUDE={self.test_dir}", str(self.main_cpp)]

        cap = compiletools.apptools.create_parser("test_empty_macro", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add_argument("filename", nargs="+")

        ctx = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        headerdeps = compiletools.headerdeps.create(args, context=ctx)
        magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

        main_deps = hunter.header_dependencies(str(self.main_cpp))
        has_conditional = any("conditional.hpp" in str(h) for h in main_deps)
        has_base = any("base.hpp" in str(h) for h in main_deps)
        has_dependency = any("dependency.hpp" in str(h) for h in main_deps)

        magic_flags = hunter.magicflags(str(self.main_cpp))
        pkg_configs = [str(f) for f in magic_flags.get(sz.Str("PKG-CONFIG"), [])]
        has_conditional_pkg = "conditional" in pkg_configs

        # Check that CPPFLAGS were added from conditional.pc
        cppflags = [str(f) for f in magic_flags.get(sz.Str("CPPFLAGS"), [])]
        cppflags_str = " ".join(cppflags)
        has_conditional_cflags = "/usr/local/include/testpkg" in cppflags_str or "TEST_PKG_ENABLED" in cppflags_str

        # EXPECTED: dependency.hpp should be found because:
        # - conditional.hpp includes base.hpp which defines USE_HASH
        # - Then #ifdef USE_HASH should succeed
        # - dependency.hpp should be included
        # - PKG-CONFIG=conditional should be extracted
        assert has_conditional, f"conditional.hpp should always be found (direct include); deps={main_deps}"
        assert has_base, f"base.hpp should be found (included by conditional.hpp); deps={main_deps}"
        assert has_dependency, (
            "BUG: dependency.hpp NOT found! Initial header discovery used empty macro state.\n"
            "     When g++ -MM processed conditional.hpp without USE_HASH defined,\n"
            "     the #ifdef USE_HASH failed and dependency.hpp was not included.\n"
            "     FIX: magicflags.py line 743 should use current macro state, not frozenset().\n"
            f"     deps={main_deps}"
        )
        assert has_conditional_pkg, (
            "BUG: PKG-CONFIG=conditional NOT found! dependency.hpp was not discovered, "
            f"so its magic flags were not extracted. pkg_configs={pkg_configs}"
        )
        assert has_conditional_cflags, (
            "BUG: conditional Cflags NOT found in CPPFLAGS! PKG-CONFIG=conditional was not processed correctly.\n"
            f"     Expected to find '/usr/local/include/testpkg' or 'TEST_PKG_ENABLED' in: {cppflags_str}"
        )
