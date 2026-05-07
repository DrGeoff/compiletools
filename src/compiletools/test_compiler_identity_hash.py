"""Tests for the compiler_identity helper and its inclusion in MacroState's
build-context hash (TOKEN-4).

Symmetric to ``_pch_command_hash`` (which already folds in compiler identity
via ``build_backend._compiler_identity``): the per-TU object cache key now
also captures the compiler binary's realpath/size/mtime so that an in-place
toolchain swap that does not change the user-visible command name (``g++``)
still invalidates stale objects.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import stringzilla as sz

import compiletools.test_base as tb
from compiletools.apptools import compiler_identity
from compiletools.build_context import BuildContext
from compiletools.preprocessing_cache import (
    MacroState,
    get_or_compute_preprocessing,
)

# ---------------------------------------------------------------------------
# Tests 1-3: apptools.compiler_identity helper
# ---------------------------------------------------------------------------


def test_compiler_identity_module_helper_returns_realpath_size_mtime():
    """When ``cxx`` resolves to a real binary, the result has the
    ``realpath|size|mtime_ns`` shape. ``sys.executable`` is a guaranteed-
    to-exist binary, used as a stand-in for a real compiler."""
    result = compiler_identity(sys.executable)
    parts = result.split("|")
    assert len(parts) == 3, f"expected 3 |-separated parts, got {parts!r}"
    realpath_part, size_part, mtime_part = parts
    assert os.path.isabs(realpath_part)
    assert size_part.isdigit()
    assert int(size_part) > 0
    assert mtime_part.isdigit()
    assert int(mtime_part) > 0


def test_compiler_identity_falls_back_to_string_when_unstatable():
    """A nonexistent compiler name returns the raw string without raising."""
    result = compiler_identity("nonexistent_compiler_xyz")
    assert result == "nonexistent_compiler_xyz"


def test_compiler_identity_handles_ccache_g_plus_plus():
    """Multi-token commands like ``ccache g++`` cannot be ``shutil.which``'d
    as a single token; the helper must fall back to the raw string instead
    of crashing."""
    result = compiler_identity("ccache g++")
    assert result == "ccache g++"


# ---------------------------------------------------------------------------
# Tests 4-5: MacroState build-context hash includes compiler_identity
# ---------------------------------------------------------------------------


def _make_state(**kwargs):
    """Construct a MacroState with sane defaults; only set new fields when
    explicitly passed so we exercise both the explicit and the default
    paths."""
    base = {
        "core": {sz.Str("__GNUC__"): sz.Str("13")},
        "variable": {},
        "compiler_path": "g++",
        "cppflags": "-I/usr/include",
        "cflags": "-O2",
        "cxxflags": "-std=c++17",
    }
    base.update(kwargs)
    return MacroState(**base)


def test_macro_state_hash_changes_with_compiler_identity_change():
    """Two MacroStates that differ only in compiler_identity must produce
    different full hashes — the whole point of TOKEN-4."""
    a = _make_state(compiler_identity="/path/to/gcc-13|12345|99")
    b = _make_state(compiler_identity="/path/to/gcc-14|54321|100")
    assert a.get_hash(include_core=True) != b.get_hash(include_core=True)


def test_macro_state_hash_unchanged_with_compiler_identity_default():
    """Two MacroStates constructed without compiler_identity (default ``""``)
    must produce equal hashes — backward compatibility for every existing
    test that doesn't set the new field."""
    a = _make_state()
    b = _make_state()
    assert a.get_hash(include_core=True) == b.get_hash(include_core=True)


# ---------------------------------------------------------------------------
# Test 6: magicflags._initialize_macro_state populates compiler_identity
# ---------------------------------------------------------------------------


class TestInitialMacroStatePopulatesCompilerIdentity(tb.BaseCompileToolsTestCase):
    """Site 1: DirectMagicFlags._initialize_macro_state must populate
    compiler_identity from args.CXX."""

    def test_initial_macro_state_populates_compiler_identity(self):
        ctx = BuildContext()
        parser = tb.create_magic_parser(["--magic", "direct"], tempdir=self._tmpdir, context=ctx)
        expected = compiler_identity(parser._args.CXX)
        assert parser._initial_macro_state.compiler_identity == expected


# ---------------------------------------------------------------------------
# Tests 7-8: compiler_identity propagates through with_updates / preprocessing
# ---------------------------------------------------------------------------


def test_compiler_identity_propagates_through_with_updates():
    """``with_updates`` must carry compiler_identity through to the new
    MacroState, identically to how it propagates the other build-context
    fields."""
    state = _make_state(compiler_identity="foo")
    updated = state.with_updates({sz.Str("X"): sz.Str("1")})
    assert updated.compiler_identity == "foo"


def test_compiler_identity_propagates_through_get_or_compute_preprocessing():
    """The MacroState returned in ``ProcessingResult.updated_macros`` must
    keep compiler_identity. Mirrors Task C's regression test for the other
    build-context fields."""
    from compiletools.file_analyzer import FileAnalysisResult

    # Minimal FileAnalysisResult: no conditionals, no includes, no defines —
    # the file is permanently invariant so we exercise the simple cache path
    # in ``get_or_compute_preprocessing``.
    file_result = FileAnalysisResult(
        line_count=0,
        line_byte_offsets=[],
        include_positions=[],
        magic_positions=[],
        directive_positions={},
        directives=[],
        directive_by_line={},
        bytes_analyzed=0,
        was_truncated=False,
        content_hash="dummy_hash_for_compiler_identity_test",
    )

    initial = _make_state(compiler_identity="my-identity-string")
    ctx = BuildContext()
    # SimplePreprocessor reverse-resolves the content hash to a path on disk;
    # mock it since we synthesized a dummy hash above.
    with patch("compiletools.global_hash_registry.get_filepath_by_hash") as mock_lookup:
        mock_lookup.return_value = "<test-file>"
        result = get_or_compute_preprocessing(file_result, initial, verbose=0, context=ctx)
    assert result.updated_macros.compiler_identity == "my-identity-string"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
