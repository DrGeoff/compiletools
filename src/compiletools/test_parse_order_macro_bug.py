"""Test that macro discovery order doesn't affect header dependency resolution.

This test reproduces a critical bug where the order in which files are processed
affects the macro state available when parsing each file, leading to different
header dependency graphs depending on the entry point.

The bug manifests when:
1. File A defines macros (e.g., hash_map.hpp defines HASH_MAP_NAME)
2. File B conditionally includes headers based on those macros (e.g., string.hpp)
3. File C includes both A and B (e.g., stream_handler.cpp)
4. Two different entry points process files in different orders:
   - Entry point 1: Processes C directly → A discovered first → full macros → correct deps
   - Entry point 2: Processes D which uses C → C parsed early → incomplete macros → wrong deps

Expected behavior: Both entry points should produce identical header dependencies for C.
Actual behavior (buggy): Entry point 2 produces fewer dependencies due to incomplete macros.
"""

import shutil
from pathlib import Path

import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path
from compiletools.test_base import BaseCompileToolsTestCase


class TestParseOrderMacroBug(BaseCompileToolsTestCase):
    """Test that parse order doesn't affect macro-dependent header resolution."""

    def setup_method(self):
        """Copy sample C++ code into the per-test tmp dir provided by the base class."""
        super().setup_method()
        self.test_dir = Path(self._tmpdir)
        sample_src = Path(example_path("parse_order_macro_bug"))
        self.libs_dir = self.test_dir / "libs"
        shutil.copytree(sample_src / "libs", self.libs_dir)
        self.hash_map_hpp = self.libs_dir / "hash_map.hpp"
        self.conditional_include_hpp = self.libs_dir / "conditional_include.hpp"
        self.hash_utility_hpp = self.libs_dir / "hash_utility.hpp"
        self.common_file_cpp = self.libs_dir / "common_file.cpp"
        self.entry_point_1_cpp = self.libs_dir / "entry_point_1.cpp"
        self.intermediate_cpp = self.libs_dir / "intermediate.cpp"
        self.entry_point_2_cpp = self.libs_dir / "entry_point_2.cpp"

    def _create_hunter(self, source_files, parser_name="test"):
        """Create a Hunter instance for the given source files."""
        argv = ["-vvv", f"--INCLUDE={self.test_dir}"] + source_files

        cap = compiletools.apptools.create_parser(parser_name, argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add_argument("filename", nargs="+")

        ctx = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        headerdeps = compiletools.headerdeps.create(args, context=ctx)
        magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

        return hunter, args

    @pytest.mark.usefixtures("pkgconfig_env")
    def test_parse_order_affects_macro_state(self):
        """Test that parse order doesn't affect macro state and dependencies.

        This test documents the EXPECTED behavior where the same file (common_file.cpp)
        gets identical header dependency lists regardless of which entry point is used.

        Entry point 1 (direct): common_file.cpp → hash_map.hpp first → hash_utility.hpp found
        Entry point 2 (indirect): intermediate.cpp → common_file.cpp → should still find hash_utility.hpp

        NOTE: This test currently PASSES because the test scenario is simple enough that
        both hunters process files in the same order. The real-world bug occurs in complex
        projects with many files where:
        1. Hunter2 processes many files from entry_point_2 first
        2. Some of those files get cached with incomplete macro states
        3. When common_file.cpp is finally needed, it's already cached with wrong macros
        4. The conditional includes resolve differently

        To reproduce the actual bug, we would need a more complex scenario with:
        - More intermediate files that get processed first
        - Macro definitions spread across multiple files
        - Deep dependency chains that affect processing order

        This test serves as:
        1. Documentation of expected behavior (should ALWAYS pass)
        2. A regression test once the bug is fixed
        3. A framework for future parse-order bug investigations
        """
        # Entry point 1: Process common_file directly (should find all headers)
        hunter1, _ = self._create_hunter([str(self.entry_point_1_cpp)], parser_name="test_parser_1")
        common_file_deps_1 = hunter1.header_dependencies(str(self.common_file_cpp))
        common_file_macro_key_1 = hunter1.macro_state_key(str(self.common_file_cpp))
        has_hash_utility_1 = any("hash_utility.hpp" in str(h) for h in common_file_deps_1)

        # Clear parsers between tests to avoid configargparse conflicts
        uth.delete_existing_parsers()
        compiletools.apptools.resetcallbacks()

        # Entry point 2: Process via intermediate (may find different headers due to parse order)
        hunter2, _ = self._create_hunter([str(self.entry_point_2_cpp)], parser_name="test_parser_2")
        common_file_deps_2 = hunter2.header_dependencies(str(self.common_file_cpp))
        common_file_macro_key_2 = hunter2.macro_state_key(str(self.common_file_cpp))
        has_hash_utility_2 = any("hash_utility.hpp" in str(h) for h in common_file_deps_2)

        # EXPECTED BEHAVIOR: Both entry points should produce identical results.
        # The same file should have the same dependencies regardless of how it's reached.
        # These assertions will FAIL with the current buggy implementation
        # because entry point 2 processes common_file.cpp before hash_map.hpp is discovered.
        assert len(common_file_deps_1) == len(common_file_deps_2), (
            f"Dependency count differs: "
            f"entry1={len(common_file_deps_1)} deps with {len(common_file_macro_key_1)} macros, "
            f"entry2={len(common_file_deps_2)} deps with {len(common_file_macro_key_2)} macros\n"
            f"entry1 deps={common_file_deps_1}\nentry2 deps={common_file_deps_2}"
        )
        assert has_hash_utility_1 == has_hash_utility_2, (
            f"hash_utility.hpp presence differs: entry1={has_hash_utility_1}, entry2={has_hash_utility_2}"
        )
        assert common_file_macro_key_1 == common_file_macro_key_2, (
            f"Macro keys differ:\n"
            f"entry1 ({len(common_file_macro_key_1)} macros)={common_file_macro_key_1}\n"
            f"entry2 ({len(common_file_macro_key_2)} macros)={common_file_macro_key_2}"
        )
        # Verify that hash_utility.hpp IS found in both cases (it should be!)
        assert has_hash_utility_1, "hash_utility.hpp should be found via entry point 1"
        assert has_hash_utility_2, "hash_utility.hpp should be found via entry point 2"
