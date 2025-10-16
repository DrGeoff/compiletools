"""
Test macro state isolation using temporary isolated test files.

This test creates its own isolated temporary project to verify that DirectHeaderDeps
properly isolates macro state between different file analyses. Uses a simple
ENABLE_FEATURE macro to test conditional header inclusion.
"""
import pytest
import os
from pathlib import Path
import tempfile
import shutil

import compiletools.headerdeps
from types import SimpleNamespace

@pytest.fixture
def temp_sample_dir():
    """Create a temporary directory with test files for macro state testing"""
    temp_dir = Path(tempfile.mkdtemp())
    
    # File that defines a macro
    (temp_dir / "with_macro.cpp").write_text("""#define ENABLE_FEATURE
#include "feature.h"
int main() { return 0; }
""")
    
    # File that does NOT define the macro
    (temp_dir / "without_macro.cpp").write_text("""// ENABLE_FEATURE not defined
#include "feature.h"
int main() { return 0; }
""")
    
    # Header with conditional inclusion
    (temp_dir / "feature.h").write_text("""#ifdef ENABLE_FEATURE
#include "enabled_feature.h"
#endif
""")
    
    # Header that should only be included when macro is defined
    (temp_dir / "enabled_feature.h").write_text("""// Only included when ENABLE_FEATURE is defined
void feature_function();
""")
    
    yield temp_dir
    
    # Cleanup
    shutil.rmtree(temp_dir)

def test_macro_state_isolation_with_temp_files(temp_sample_dir):
    """
    Test macro state isolation using temporary isolated test files.

    This test creates its own minimal project to verify that macro state
    doesn't bleed between analyses of different files.

    This test should FAIL when the bug is present (macro state pollution)
    and PASS when the bug is fixed (proper macro state isolation).
    """
    
    # Setup DirectHeaderDeps
    args = SimpleNamespace()
    args.verbose = 0
    args.headerdeps = 'direct'
    args.max_file_read_size = 0
    args.CPPFLAGS = f'-I {temp_sample_dir}'
    args.CFLAGS = ''
    args.CXXFLAGS = ''
    args.CXX = 'g++'
    
    original_cwd = os.getcwd()
    os.chdir(temp_sample_dir)
    
    try:
        # Create single DirectHeaderDeps instance 
        # This is where macro state pollution occurs
        headerdeps = compiletools.headerdeps.DirectHeaderDeps(args)
        
        # First analysis: file WITH macro
        # Should include enabled_feature.h
        with_macro_deps = headerdeps.process('with_macro.cpp', frozenset())
        has_feature_with_macro = any('enabled_feature.h' in dep for dep in with_macro_deps)

        # Second analysis: file WITHOUT macro
        # Should NOT include enabled_feature.h
        # But macro state pollution might cause incorrect inclusion
        without_macro_deps = headerdeps.process('without_macro.cpp', frozenset())
        has_feature_without_macro = any('enabled_feature.h' in dep for dep in without_macro_deps)
        
        # Debug output
        print("\nDependency analysis results:")
        print(f"  with_macro.cpp deps: {with_macro_deps}")
        print(f"  without_macro.cpp deps: {without_macro_deps}")
        print(f"  with_macro includes enabled_feature.h: {has_feature_with_macro}")
        print(f"  without_macro includes enabled_feature.h: {has_feature_without_macro}")
        
        # Assertions that expose the bug
        assert has_feature_with_macro, \
            "with_macro.cpp should include enabled_feature.h (defines ENABLE_FEATURE)"
            
        assert not has_feature_without_macro, \
            "without_macro.cpp should NOT include enabled_feature.h (no ENABLE_FEATURE defined). " \
            "If this fails, it indicates macro state pollution between analyses."
            
    finally:
        os.chdir(original_cwd)

