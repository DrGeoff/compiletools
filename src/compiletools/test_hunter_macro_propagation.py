#!/usr/bin/env python3
"""
Test case that exposes the Hunter macro propagation bug.

This test demonstrates a bug where Hunter._get_immediate_deps() is cached by
(realpath, macro_state_key), but when it calls headerdeps.process(), the
headerdeps resets macros to core-only instead of using the macro_state_key.

When analyzing headers as dependencies, file-level #define macros are lost,
causing conditional includes to be incorrectly evaluated.
"""
import os
import sys
from pathlib import Path

# Add compiletools to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import compiletools.hunter
import compiletools.headerdeps
import compiletools.magicflags
from types import SimpleNamespace


def test_hunter_propagates_macros_to_header_dependencies():
    """
    Test that Hunter correctly propagates macros when analyzing header files
    with different macro contexts.

    This test directly calls Hunter._get_immediate_deps() on a header file
    with different macro_state_key values to verify that the macro state
    is properly used when preprocessing the header.

    Setup:
    - config.h conditionally includes renderer.h based on ENABLE_RENDERING

    Expected:
    - When macro_state_key is empty: should NOT find renderer.h
    - When macro_state_key has ENABLE_RENDERING: SHOULD find renderer.h

    Bug: headerdeps.process() resets macros to empty regardless of the
    macro_state_key, so both calls return the same (wrong) result.
    """
    # Setup directory - point to sample files
    sample_dir = Path(__file__).parent / 'samples' / 'hunter_macro_propagation'
    original_cwd = os.getcwd()
    os.chdir(sample_dir)

    try:
        # Create args
        args = SimpleNamespace()
        args.verbose = 0
        args.headerdeps = 'direct'
        args.magic = 'direct'
        args.max_file_read_size = 0
        args.allow_magic_source_in_header = False
        args.CPPFLAGS = f'-I {sample_dir}'
        args.CFLAGS = ''
        args.CXXFLAGS = ''
        args.CXX = 'g++'

        # Create components
        headerdeps = compiletools.headerdeps.DirectHeaderDeps(args)
        magicparser = compiletools.magicflags.DirectMagicFlags(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        config_h_path = str(sample_dir / 'config.h')

        # Test 1: Analyze config.h WITHOUT the macro
        import stringzilla as sz
        macro_key_without = frozenset()
        headers_without, _ = hunter._get_immediate_deps(config_h_path, macro_key_without)
        has_renderer_without = any('renderer.h' in h for h in headers_without)

        print("\n" + "="*70)
        print("Test 1: config.h dependencies WITHOUT ENABLE_RENDERING")
        print("="*70)
        print(f"Headers: {headers_without}")
        print(f"Includes renderer.h? {has_renderer_without}")

        # Test 2: Analyze config.h WITH the macro
        macro_key_with = frozenset({(sz.Str('ENABLE_RENDERING'), sz.Str('1'))})
        headers_with, _ = hunter._get_immediate_deps(config_h_path, macro_key_with)
        has_renderer_with = any('renderer.h' in h for h in headers_with)

        print("\n" + "="*70)
        print("Test 2: config.h dependencies WITH ENABLE_RENDERING")
        print("="*70)
        print(f"Headers: {headers_with}")
        print(f"Includes renderer.h? {has_renderer_with}")
        print("="*70)

        # Assertions
        assert not has_renderer_without, \
            "config.h should NOT include renderer.h when ENABLE_RENDERING is not in macro_state_key"

        assert has_renderer_with, \
            "config.h SHOULD include renderer.h when ENABLE_RENDERING is in macro_state_key. " \
            "If this fails, it means Hunter._get_immediate_deps() calls headerdeps.process() " \
            "which resets macros to empty, ignoring the macro_state_key parameter."

        print("\n✅ PASS: Hunter correctly uses macro_state_key when analyzing headers")

    finally:
        os.chdir(original_cwd)


if __name__ == '__main__':
    try:
        test_hunter_propagates_macros_to_header_dependencies()
        print("\n✅ Test passed - Hunter macro propagation works correctly!")
        sys.exit(0)
    except AssertionError as e:
        print("\n❌ Test FAILED - Hunter macro propagation bug detected!")
        print(f"\nAssertion Error: {e}")
        print("\n🔍 ROOT CAUSE:")
        print("   Hunter._get_immediate_deps(realpath, macro_state_key) calls headerdeps.process(realpath),")
        print("   but headerdeps.process() ALWAYS resets macros to core-only (empty variable macros)")
        print("   via _initialize_includes_and_macros(), completely ignoring the macro_state_key.")
        print("\n   This causes headers to be preprocessed without file-level #define directives,")
        print("   leading to incorrect conditional compilation evaluation and missing dependencies.")
        sys.exit(1)
