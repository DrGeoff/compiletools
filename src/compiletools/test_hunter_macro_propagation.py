"""
Test case that exposes the Hunter macro propagation bug.

This test demonstrates a bug where Hunter._get_immediate_deps() is cached by
(realpath, macro_state_key), but when it calls headerdeps.process(), the
headerdeps resets macros to core-only instead of using the macro_state_key.

When analyzing headers as dependencies, file-level #define macros are lost,
causing conditional includes to be incorrectly evaluated.
"""

import sys
from argparse import Namespace
from pathlib import Path

import stringzilla as sz

# Add compiletools to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path


def test_hunter_propagates_macros_to_header_dependencies(monkeypatch):
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

    Bug: Hunter._get_immediate_deps(realpath, macro_state_key) calls
    headerdeps.process(realpath), but headerdeps.process() ALWAYS resets
    macros to core-only (empty variable macros) via
    _initialize_includes_and_macros(), completely ignoring the
    macro_state_key. Both calls then return the same (wrong) result.
    """
    sample_dir = Path(example_path("hunter_macro_propagation"))
    monkeypatch.chdir(sample_dir)

    args = Namespace()
    args.verbose = 0
    args.headerdeps = "direct"
    args.magic = "direct"
    args.max_file_read_size = 0
    args.allow_magic_source_in_header = False
    args.CPPFLAGS = f"-I {sample_dir}"
    args.CFLAGS = ""
    args.CXXFLAGS = ""
    args.CXX = "g++"
    uth.finalize_flag_state(args)

    ctx = BuildContext()
    headerdeps = compiletools.headerdeps.DirectHeaderDeps(args, context=ctx)
    magicparser = compiletools.magicflags.DirectMagicFlags(args, headerdeps, context=ctx)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

    config_h_path = str(sample_dir / "config.h")

    # Test 1: Analyze config.h WITHOUT the macro.
    macro_key_without = frozenset()
    headers_without, _ = hunter._get_immediate_deps(config_h_path, macro_key_without)
    has_renderer_without = any("renderer.h" in h for h in headers_without)

    # Test 2: Analyze config.h WITH the macro.
    macro_key_with = frozenset({(sz.Str("ENABLE_RENDERING"), sz.Str("1"))})
    headers_with, _ = hunter._get_immediate_deps(config_h_path, macro_key_with)
    has_renderer_with = any("renderer.h" in h for h in headers_with)

    assert not has_renderer_without, (
        "config.h should NOT include renderer.h when ENABLE_RENDERING is not in macro_state_key. "
        f"Got headers={headers_without}"
    )
    assert has_renderer_with, (
        "config.h SHOULD include renderer.h when ENABLE_RENDERING is in macro_state_key. "
        "If this fails, Hunter._get_immediate_deps() is calling headerdeps.process() "
        "which resets macros to empty, ignoring the macro_state_key parameter. "
        f"Got headers={headers_with}"
    )
