#!/usr/bin/env python3
"""
Test that exposes the macro state dependency bug in DirectHeaderDeps.

This test is designed to FAIL when the bug is present and PASS when the bug is fixed.
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
    - file_with_macro.cpp should include conditional.h (because it defines TEST_MACRO)
    - file_without_macro.cpp should NOT include conditional.h (because it doesn't define TEST_MACRO)
    
    Bug behavior:
    - Both files include conditional.h due to macro state pollution
    """
    
    # Create test files that demonstrate the issue
    sample_dir = Path(__file__).parent
    
    # File 1: Defines a macro that enables conditional inclusion
    file_with_macro = sample_dir / "file_with_macro.cpp"
    file_with_macro.write_text("""#define TEST_MACRO
#include "conditional_header.h"

int main() { return 0; }
""")
    
    # File 2: Does NOT define the macro
    file_without_macro = sample_dir / "file_without_macro.cpp" 
    file_without_macro.write_text("""// No TEST_MACRO defined here
#include "conditional_header.h"

int main() { return 0; }
""")
    
    # Header that conditionally includes another header based on macro
    conditional_header = sample_dir / "conditional_header.h"
    conditional_header.write_text("""#ifdef TEST_MACRO
#include "only_when_macro_defined.h"
#endif
""")
    
    # Header that should only be included when TEST_MACRO is defined
    only_when_macro = sample_dir / "only_when_macro_defined.h"
    only_when_macro.write_text("""// This header should only be included when TEST_MACRO is defined
void test_function();
""")
    
    try:
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
        os.chdir(sample_dir)
        
        # Create single DirectHeaderDeps instance (this is where the bug manifests)
        headerdeps = compiletools.headerdeps.DirectHeaderDeps(args)
        
        # First analysis: file WITH macro (should include only_when_macro_defined.h)
        deps_with_macro = headerdeps.process(str(file_with_macro))
        has_conditional_with_macro = any('only_when_macro_defined.h' in dep for dep in deps_with_macro)
        
        # Second analysis: file WITHOUT macro (should NOT include only_when_macro_defined.h)
        # But due to macro state pollution, it might incorrectly include it
        deps_without_macro = headerdeps.process(str(file_without_macro))
        has_conditional_without_macro = any('only_when_macro_defined.h' in dep for dep in deps_without_macro)
        
        print(f"File WITH macro includes conditional header: {has_conditional_with_macro}")
        print(f"File WITHOUT macro includes conditional header: {has_conditional_without_macro}")
        
        # Pytest assertions (no return values needed)
        assert has_conditional_with_macro, \
            "File with macro should include conditional header (defines TEST_MACRO)"
            
        assert not has_conditional_without_macro, \
            "File without macro should NOT include conditional header (no TEST_MACRO defined). " \
            "If this fails, it indicates macro state pollution between analyses."
            
        print("✅ PASS: Macro state is properly isolated")
            
    finally:
        os.chdir(original_cwd)
        # Clean up test files
        for f in [file_with_macro, file_without_macro, conditional_header, only_when_macro]:
            if f.exists():
                f.unlink()

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