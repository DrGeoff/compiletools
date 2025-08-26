#!/usr/bin/env python3
"""
Test that exposes the macro state dependency bug in DirectHeaderDeps.

This test is designed to FAIL when the bug is present and PASS when the bug is fixed.
Uses existing sample files to avoid race conditions in parallel test execution.
"""
import os
import sys
from pathlib import Path

# Add compiletools to path
sys.path.insert(0, '/home/gericksson/compiletools/src')

import compiletools.headerdeps
from types import SimpleNamespace

def test_macro_state_pollution_bug():
    """
    This test exposes the macro state pollution bug where DirectHeaderDeps
    returns inconsistent results when the same instance is used to analyze
    multiple files with different macro contexts.
    
    Expected behavior:
    - main.cpp defines FEATURE_A_ENABLED, so module_b.h should be included via config.h -> core.h
    - clean_main.cpp does NOT define FEATURE_A_ENABLED, so module_b.h should NOT be included
    
    Bug behavior:
    - Both files include module_b.h due to macro state pollution
    """
    
    sample_dir = Path(__file__).parent
    file_with_macro = sample_dir / "main.cpp"
    file_without_macro = sample_dir / "clean_main.cpp"
    
    # Setup DirectHeaderDeps
    args = SimpleNamespace()
    args.verbose = 0
    args.headerdeps = 'direct'
    args.max_file_read_size = 0
    args.CPPFLAGS = f'-I {sample_dir}'
    args.CFLAGS = ''
    args.CXXFLAGS = ''
    args.CXX = 'g++'
    
    original_cwd = os.getcwd()
    
    try:
        os.chdir(sample_dir)
        
        # Create single DirectHeaderDeps instance (this is where the bug manifests)
        headerdeps = compiletools.headerdeps.DirectHeaderDeps(args)
        
        # First analysis: file WITH macro (should include module_b.h)
        # main.cpp defines FEATURE_A_ENABLED -> config.h defines FEATURE_B_ENABLED -> core.h includes module_b.h
        deps_with_macro = headerdeps.process(str(file_with_macro))
        has_conditional_with_macro = any('module_b.h' in dep for dep in deps_with_macro)
        
        # Second analysis: file WITHOUT macro (should NOT include module_b.h)
        # clean_main.cpp doesn't define FEATURE_A_ENABLED -> no FEATURE_B_ENABLED -> no module_b.h inclusion
        # But due to macro state pollution, it might incorrectly include it
        deps_without_macro = headerdeps.process(str(file_without_macro))
        has_conditional_without_macro = any('module_b.h' in dep for dep in deps_without_macro)
        
        print(f"File WITH macro includes module_b.h: {has_conditional_with_macro}")
        print(f"File WITHOUT macro includes module_b.h: {has_conditional_without_macro}")
        
        # Pytest assertions (no return values needed)
        assert has_conditional_with_macro, \
            "main.cpp should include module_b.h (defines FEATURE_A_ENABLED)"
            
        assert not has_conditional_without_macro, \
            "clean_main.cpp should NOT include module_b.h (no FEATURE_A_ENABLED defined). " \
            "If this fails, it indicates macro state pollution between analyses."
            
        print("✅ PASS: Macro state is properly isolated")
            
    finally:
        os.chdir(original_cwd)

if __name__ == '__main__':
    try:
        test_macro_state_pollution_bug()
        print("\n✅ Test passed - macro state bug is fixed!")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ Test failed - macro state bug detected: {e}")
        print("\n🔍 This test exposes the macro state dependency bug!")
        print("   Run this test before and after applying the fix to see the difference.")
        sys.exit(1)