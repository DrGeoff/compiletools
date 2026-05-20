"""
Test macro state isolation using temporary isolated test files.

This test creates its own isolated temporary project to verify that DirectHeaderDeps
properly isolates macro state between different file analyses. Uses a simple
ENABLE_FEATURE macro to test conditional header inclusion.
"""

from types import SimpleNamespace

import pytest

import compiletools.headerdeps
from compiletools.build_context import BuildContext


@pytest.fixture
def temp_sample_dir(tmp_path):
    """Populate a pytest tmp_path with test files for macro state testing."""
    # File that defines a macro
    (tmp_path / "with_macro.cpp").write_text("""#define ENABLE_FEATURE
#include "feature.h"
int main() { return 0; }
""")

    # File that does NOT define the macro
    (tmp_path / "without_macro.cpp").write_text("""// ENABLE_FEATURE not defined
#include "feature.h"
int main() { return 0; }
""")

    # Header with conditional inclusion
    (tmp_path / "feature.h").write_text("""#ifdef ENABLE_FEATURE
#include "enabled_feature.h"
#endif
""")

    # Header that should only be included when macro is defined
    (tmp_path / "enabled_feature.h").write_text("""// Only included when ENABLE_FEATURE is defined
void feature_function();
""")

    return tmp_path


def test_macro_state_isolation_with_temp_files(temp_sample_dir, monkeypatch):
    """
    Test macro state isolation using temporary isolated test files.

    This test creates its own minimal project to verify that macro state
    doesn't bleed between analyses of different files.

    This test should FAIL when the bug is present (macro state pollution)
    and PASS when the bug is fixed (proper macro state isolation).
    """
    args = SimpleNamespace()
    args.verbose = 0
    args.headerdeps = "direct"
    args.max_file_read_size = 0
    args.CPPFLAGS = f"-I {temp_sample_dir}"
    args.CFLAGS = ""
    args.CXXFLAGS = ""
    args.CXX = "g++"

    monkeypatch.chdir(temp_sample_dir)

    # Create single DirectHeaderDeps instance
    # This is where macro state pollution occurs
    ctx = BuildContext()
    headerdeps = compiletools.headerdeps.DirectHeaderDeps(args, context=ctx)

    # First analysis: file WITH macro -- should include enabled_feature.h.
    with_macro_deps = headerdeps.process("with_macro.cpp", frozenset())
    has_feature_with_macro = any("enabled_feature.h" in dep for dep in with_macro_deps)

    # Second analysis: file WITHOUT macro -- should NOT include enabled_feature.h.
    # Macro state pollution might cause incorrect inclusion.
    without_macro_deps = headerdeps.process("without_macro.cpp", frozenset())
    has_feature_without_macro = any("enabled_feature.h" in dep for dep in without_macro_deps)

    assert has_feature_with_macro, (
        f"with_macro.cpp should include enabled_feature.h (defines ENABLE_FEATURE); got deps={with_macro_deps}"
    )
    assert not has_feature_without_macro, (
        "without_macro.cpp should NOT include enabled_feature.h (no ENABLE_FEATURE defined). "
        "If this fails, it indicates macro state pollution between analyses. "
        f"deps={without_macro_deps}"
    )
