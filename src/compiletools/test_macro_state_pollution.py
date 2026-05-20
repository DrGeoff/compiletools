"""
Test case for the macro state dependency bug found in ct-cake workflow.

This test demonstrates the specific bug where DirectHeaderDeps returned
inconsistent results due to macro state pollution between calls.
"""

from pathlib import Path
from types import SimpleNamespace

import compiletools.headerdeps
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path


def test_sequential_dependency_analysis_consistency(monkeypatch):
    """
    Test that demonstrates the macro state pollution bug discovered in ct-cake.

    This test simulates the ct-cake workflow where multiple files are processed
    sequentially, and macro state changes can affect subsequent analyses.
    """
    sample_dir = Path(example_path("macro_state_dependency"))
    monkeypatch.chdir(sample_dir)

    args = SimpleNamespace()
    args.verbose = 0
    args.headerdeps = "direct"
    args.max_file_read_size = 0
    args.CPPFLAGS = f"-I {sample_dir}"
    args.CFLAGS = ""
    args.CXXFLAGS = ""
    args.CXX = "g++"

    # Create single DirectHeaderDeps instance (simulates ct-cake behavior)
    ctx = BuildContext()
    headerdeps = compiletools.headerdeps.DirectHeaderDeps(args, context=ctx)

    # First analysis: main.cpp (defines FEATURE_A_ENABLED -> FEATURE_B_ENABLED)
    # This should include module_b.h via config.h -> core.h chain
    main_deps_1 = headerdeps.process("main.cpp", frozenset())
    main_has_module_b_1 = any("module_b.h" in dep for dep in main_deps_1)

    # Second analysis: clean_main.cpp (no FEATURE_A_ENABLED)
    # This should NOT include module_b.h
    # But macro state pollution could cause it to be included incorrectly
    clean_deps = headerdeps.process("clean_main.cpp", frozenset())
    clean_has_module_b = any("module_b.h" in dep for dep in clean_deps)

    # Third analysis: main.cpp again (should be consistent with first)
    main_deps_2 = headerdeps.process("main.cpp", frozenset())
    main_has_module_b_2 = any("module_b.h" in dep for dep in main_deps_2)

    assert main_has_module_b_1, "main.cpp should include module_b.h (first analysis)"
    assert main_has_module_b_2, "main.cpp should include module_b.h (repeat analysis)"
    assert not clean_has_module_b, "clean_main.cpp should NOT include module_b.h"
    assert main_has_module_b_1 == main_has_module_b_2, (
        "main.cpp analysis should be consistent across multiple calls"
    )
