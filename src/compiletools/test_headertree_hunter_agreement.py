"""Test disagreement between headertree.py and hunter.py on included files.

The bug: headertree.py and hunter.py can disagree on what files are included when
conditional includes depend on file-defined macros.

headertree.py: Uses DirectHeaderDeps directly, starting with core macros only
hunter.py: Uses magicflags two-pass discovery to handle file-defined macros

This test exposes cases where they disagree.
"""
import pytest
from pathlib import Path

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.tree import flatten
from compiletools.test_base import BaseCompileToolsTestCase


class TestHeadertreeHunterAgreement(BaseCompileToolsTestCase):
    """Test that headertree.py and hunter.py agree on header discovery.

    These tools can disagree when conditional includes depend on file-defined
    macros, since headertree lacks the two-pass discovery that hunter uses.
    """

    def _extract_headers_from_headertree(self, filename, compiler_macros=None):
        """Extract set of headers discovered by headertree.py.

        Args:
            filename: Path to source file (as string or Path)
            compiler_macros: Optional list of compiler macros (e.g., ['-DDEBUG'])

        Returns:
            Set of header file paths discovered by headertree
        """
        filename = str(filename)
        test_dir = str(Path(filename).parent)
        argv = [f'--INCLUDE={test_dir}', filename]
        if compiler_macros:
            argv.extend(compiler_macros)

        # Create parser and args
        cap = compiletools.apptools.create_parser("test_headertree", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        cap.add('filename', nargs='+')
        args = compiletools.apptools.parseargs(cap, argv)

        # Create DirectHeaderDeps and generate tree
        ht = compiletools.headerdeps.DirectHeaderDeps(args)
        tree = ht.generatetree(args.filename[0])

        # Flatten the tree to get all headers (excluding the root file itself)
        all_files = flatten(tree)
        all_files.discard(args.filename[0])

        return all_files

    def _extract_headers_from_hunter(self, filename, compiler_macros=None):
        """Extract set of headers discovered by hunter.py.

        Args:
            filename: Path to source file (as string or Path)
            compiler_macros: Optional list of compiler macros (e.g., ['-DDEBUG'])

        Returns:
            Set of header file paths discovered by hunter
        """
        filename = str(filename)
        test_dir = str(Path(filename).parent)
        argv = [f'--INCLUDE={test_dir}', filename]
        if compiler_macros:
            argv.extend(compiler_macros)

        # Create parser and args
        cap = compiletools.apptools.create_parser("test_hunter", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add('filename', nargs='+')
        args = compiletools.apptools.parseargs(cap, argv)

        # Create hunter components
        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        # Get header dependencies
        headers = hunter.header_dependencies(filename)

        return headers

    def _get_basenames(self, header_set):
        """Convert set of file paths to set of basenames for easier comparison."""
        return {Path(h).name for h in header_set}

    def test_empty_macro_bug_sample(self, pkgconfig_env):
        """Expose bug where headertree misses conditionally-included headers.

        The dependency chain:
        - main.cpp includes conditional.hpp
        - conditional.hpp includes base.hpp (which defines USE_HASH)
        - conditional.hpp conditionally includes dependency.hpp if USE_HASH is defined

        Expected: Both tools should find dependency.hpp via two-pass discovery
        Bug: headertree processes conditional.hpp without USE_HASH defined first,
             so the #ifdef USE_HASH fails and dependency.hpp is NOT included
        """
        main_cpp = Path(uth.samplesdir()) / "empty_macro_bug" / "libs" / "main.cpp"

        headertree_headers = self._extract_headers_from_headertree(main_cpp)
        uth.delete_existing_parsers()  # Clear parser registry between calls
        hunter_headers = self._extract_headers_from_hunter(main_cpp)

        headertree_basenames = self._get_basenames(headertree_headers)
        hunter_basenames = self._get_basenames(hunter_headers)

        print(f"\nheadertree found: {sorted(headertree_basenames)}")
        print(f"hunter found: {sorted(hunter_basenames)}")

        # Check for disagreement
        missing_in_headertree = hunter_basenames - headertree_basenames
        missing_in_hunter = headertree_basenames - hunter_basenames

        if missing_in_headertree:
            print(f"\nBUG EXPOSED: headertree missed: {missing_in_headertree}")
        if missing_in_hunter:
            print(f"\nWARNING: hunter missed: {missing_in_hunter}")

        assert headertree_basenames == hunter_basenames, \
            f"BUG EXPOSED: headertree and hunter disagree on included files!\n" \
            f"\n" \
            f"Root cause: headertree.py lacks two-pass macro discovery.\n" \
            f"- base.hpp defines USE_HASH=1 (file-defined macro)\n" \
            f"- conditional.hpp has #ifdef USE_HASH to include dependency.hpp\n" \
            f"- headertree processes conditional.hpp without USE_HASH first\n" \
            f"- The #ifdef fails, so dependency.hpp is NOT discovered\n" \
            f"\n" \
            f"headertree missed: {missing_in_headertree}\n" \
            f"hunter missed: {missing_in_hunter}\n" \
            f"\n" \
            f"Fix: Implement two-pass discovery in headertree.py like hunter does:\n" \
            f"     1. Initial discovery with core macros\n" \
            f"     2. Extract file-defined macros from discovered headers\n" \
            f"     3. Re-discover with converged macro state"

    def test_undef_bug_sample(self):
        """Test #undef handling agreement between headertree and hunter.

        The dependency chain:
        - main.cpp includes uses_conditional.hpp
        - uses_conditional.hpp includes cleans_up.hpp
        - cleans_up.hpp includes defines_macro.hpp (defines TEMP_BUFFER_SIZE)
        - cleans_up.hpp does #undef TEMP_BUFFER_SIZE
        - uses_conditional.hpp has #ifndef TEMP_BUFFER_SIZE to include should_be_included.hpp

        Expected: Both should find should_be_included.hpp after #undef
        Bug: If #undef handling is broken, tools might have stale macro state
        """
        main_cpp = Path(uth.samplesdir()) / "undef_bug" / "main.cpp"

        headertree_headers = self._extract_headers_from_headertree(main_cpp)
        uth.delete_existing_parsers()  # Clear parser registry between calls
        hunter_headers = self._extract_headers_from_hunter(main_cpp)

        headertree_basenames = self._get_basenames(headertree_headers)
        hunter_basenames = self._get_basenames(hunter_headers)

        print(f"\nheadertree found: {sorted(headertree_basenames)}")
        print(f"hunter found: {sorted(hunter_basenames)}")

        missing_in_headertree = hunter_basenames - headertree_basenames
        missing_in_hunter = headertree_basenames - hunter_basenames

        if missing_in_headertree:
            print(f"\nBUG: headertree missed: {missing_in_headertree}")
        if missing_in_hunter:
            print(f"\nBUG: hunter missed: {missing_in_hunter}")

        assert headertree_basenames == hunter_basenames, \
            f"Disagreement on #undef handling!\n" \
            f"\n" \
            f"Both tools should handle #undef TEMP_BUFFER_SIZE correctly.\n" \
            f"After cleans_up.hpp undefines the macro, uses_conditional.hpp\n" \
            f"should include should_be_included.hpp via #ifndef TEMP_BUFFER_SIZE.\n" \
            f"\n" \
            f"headertree missed: {missing_in_headertree}\n" \
            f"hunter missed: {missing_in_hunter}\n" \
            f"\n" \
            f"If hunter is missing files: hunter.header_dependencies() may only\n" \
            f"return direct dependencies, not the full transitive closure.\n" \
            f"If headertree is missing files: #undef handling in preprocessing may be broken."

    def test_macro_state_dependency_sample(self):
        """Test agreement when both start with same initial macro state.

        The dependency chain varies based on whether DEBUG is defined.
        Both tools should agree given the same initial macro state (no -DDEBUG).

        This test validates that when no file-defined macros affect includes,
        both tools produce consistent results.
        """
        main_cpp = Path(uth.samplesdir()) / "macro_state_dependency" / "main.cpp"

        # Test without DEBUG macro (both should agree on baseline)
        headertree_headers = self._extract_headers_from_headertree(main_cpp)
        uth.delete_existing_parsers()  # Clear parser registry between calls
        hunter_headers = self._extract_headers_from_hunter(main_cpp)

        headertree_basenames = self._get_basenames(headertree_headers)
        hunter_basenames = self._get_basenames(hunter_headers)

        print(f"\nWithout DEBUG macro:")
        print(f"headertree found: {sorted(headertree_basenames)}")
        print(f"hunter found: {sorted(hunter_basenames)}")

        missing_in_headertree = hunter_basenames - headertree_basenames
        missing_in_hunter = headertree_basenames - hunter_basenames

        if missing_in_headertree:
            print(f"\nBUG: headertree missed: {missing_in_headertree}")
        if missing_in_hunter:
            print(f"\nBUG: hunter missed: {missing_in_hunter}")

        assert headertree_basenames == hunter_basenames, \
            f"Disagreement on basic macro-conditional includes!\n" \
            f"\n" \
            f"Both tools should agree when starting with the same core macros\n" \
            f"and no file-defined macros affect the include paths.\n" \
            f"\n" \
            f"headertree missed: {missing_in_headertree}\n" \
            f"hunter missed: {missing_in_hunter}\n" \
            f"\n" \
            f"This is a baseline test - if this fails, there's a fundamental\n" \
            f"disagreement in how the tools process conditional includes."
