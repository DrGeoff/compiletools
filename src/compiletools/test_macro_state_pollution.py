"""
Test case for the macro state dependency bug found in ct-cake workflow.

This test demonstrates the specific bug where DirectHeaderDeps returned
inconsistent results due to macro state pollution between calls.
"""
import os
from pathlib import Path

import compiletools.headerdeps
from types import SimpleNamespace

def test_sequential_dependency_analysis_consistency():
    """
    Test that demonstrates the macro state pollution bug discovered in ct-cake.

    This test simulates the ct-cake workflow where multiple files are processed
    sequentially, and macro state changes can affect subsequent analyses.
    """
    # Setup directory and change to sample directory
    from compiletools.testhelper import samplesdir
    sample_dir = Path(samplesdir()) / "macro_state_dependency"
    original_cwd = os.getcwd()
    os.chdir(sample_dir)
    
    try:
        # Create args for DirectHeaderDeps
        args = SimpleNamespace()
        args.verbose = 0
        args.headerdeps = 'direct'
        args.max_file_read_size = 0
        args.CPPFLAGS = f'-I {sample_dir}'
        args.CFLAGS = ''
        args.CXXFLAGS = ''
        args.CXX = 'g++'
        
        # Create single DirectHeaderDeps instance (simulates ct-cake behavior)
        headerdeps = compiletools.headerdeps.DirectHeaderDeps(args)
        
        # First analysis: main.cpp (defines FEATURE_A_ENABLED -> FEATURE_B_ENABLED)
        # This should include module_b.h via config.h -> core.h chain
        main_deps_1 = headerdeps.process('main.cpp', frozenset())
        main_has_module_b_1 = any('module_b.h' in dep for dep in main_deps_1)

        # Second analysis: clean_main.cpp (no FEATURE_A_ENABLED)
        # This should NOT include module_b.h
        # But macro state pollution could cause it to be included incorrectly
        clean_deps = headerdeps.process('clean_main.cpp', frozenset())
        clean_has_module_b = any('module_b.h' in dep for dep in clean_deps)

        # Third analysis: main.cpp again (should be consistent with first)
        main_deps_2 = headerdeps.process('main.cpp', frozenset())
        main_has_module_b_2 = any('module_b.h' in dep for dep in main_deps_2)
        
        # The critical assertion: results should be consistent
        # main.cpp should ALWAYS include module_b.h
        # clean_main.cpp should NEVER include module_b.h  
        # Regardless of the order of analysis
        
        assert main_has_module_b_1, "main.cpp should include module_b.h (first analysis)"
        assert main_has_module_b_2, "main.cpp should include module_b.h (repeat analysis)"
        assert not clean_has_module_b, "clean_main.cpp should NOT include module_b.h"
        
        # Verify consistency between repeated analyses
        assert main_has_module_b_1 == main_has_module_b_2, \
            "main.cpp analysis should be consistent across multiple calls"
            
        print("✅ PASS: Sequential dependency analysis is consistent")
        print(f"   main.cpp includes module_b.h: {main_has_module_b_1} (consistent)")
        print(f"   clean_main.cpp includes module_b.h: {clean_has_module_b}")
        
    finally:
        os.chdir(original_cwd)

