"""Tests for unified preprocessing cache."""

import os
import sys
from textwrap import dedent
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import stringzilla as sz

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective
from compiletools.preprocessing_cache import MacroState, clear_cache, get_cache_stats, get_or_compute_preprocessing


class TestPreprocessingCache:
    """Tests for unified preprocessing cache correctness."""

    def setup_method(self):
        """Clear cache before each test."""
        self.ctx = BuildContext()
        clear_cache(self.ctx)

        # Mock get_filepath_by_hash since tests don't have real files in registry
        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock_get_filepath = self.patcher.start()
        self.mock_get_filepath.return_value = "<test-file>"

    def teardown_method(self):
        """Clean up after each test method."""
        self.patcher.stop()

    def _get_stats(self):
        return get_cache_stats(self.ctx)

    def _clear(self):
        clear_cache(self.ctx)

    def _create_simple_file_result(self, text: str, content_hash: str = "test_hash_001") -> FileAnalysisResult:
        """Helper to create FileAnalysisResult for testing."""
        lines = text.split("\n")

        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode("utf-8")) + 1

        # Parse directives for conditional compilation
        directives = []
        directive_by_line = {}

        for line_num, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#ifdef"):
                macro_name = sz.Str(stripped.split()[1] if len(stripped.split()) > 1 else "")
                directive = PreprocessorDirective(
                    line_num=line_num,
                    byte_pos=line_byte_offsets[line_num],
                    directive_type="ifdef",
                    continuation_lines=0,
                    condition=None,
                    macro_name=macro_name,
                    macro_value=None,
                )
                directives.append(directive)
                directive_by_line[line_num] = directive
            elif stripped.startswith("#endif"):
                directive = PreprocessorDirective(
                    line_num=line_num,
                    byte_pos=line_byte_offsets[line_num],
                    directive_type="endif",
                    continuation_lines=0,
                    condition=None,
                    macro_name=None,
                    macro_value=None,
                )
                directives.append(directive)
                directive_by_line[line_num] = directive

        # Build directive_positions from parsed directives
        directive_positions = {}
        for directive in directives:
            dtype = directive.directive_type
            if dtype not in directive_positions:
                directive_positions[dtype] = []
            directive_positions[dtype].append(directive.byte_pos)

        # Create includes list
        includes = []
        for line_num, line in enumerate(lines):
            if "#include" in line:
                includes.append(
                    {
                        "line_num": line_num,
                        "filename": sz.Str(line.split('"')[1] if '"' in line else "test.h"),
                        "type": "quoted",
                    }
                )

        # Extract conditional_macros from directives (critical for cache logic)
        conditional_macros = set()
        for directive in directives:
            if directive.directive_type in ("ifdef", "ifndef") and directive.macro_name:
                conditional_macros.add(directive.macro_name)

        return FileAnalysisResult(
            line_count=len(lines),
            line_byte_offsets=line_byte_offsets,
            include_positions=[],
            magic_positions=[],
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=len(text.encode("utf-8")),
            was_truncated=False,
            includes=includes,
            defines=[],
            magic_flags=[],
            content_hash=content_hash,
            include_guard=None,
            conditional_macros=frozenset(conditional_macros),
        )

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_basic_hit(self):
        """Test basic cache hit scenario."""
        text = dedent("""
            #ifdef TEST_MACRO
            #include "test.h"
            #endif
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_001")
        macros = MacroState({}, {sz.Str("TEST_MACRO"): sz.Str("1")}, anchor_root="")

        # First call - cache miss
        result1 = get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)

        # Second call - cache hit
        result2 = get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)

        # Results should be identical
        assert result1.active_lines == result2.active_lines
        assert result1.active_includes == result2.active_includes

        # Verify cache was used
        stats = get_cache_stats(self.ctx)
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["total_calls"] == 2

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_macro_value_change(self):
        """Test that macro value changes produce different results."""
        text = dedent("""
            #ifdef FOO
            #include "enabled.h"
            #endif
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_002")
        macros1 = MacroState({}, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")
        macros2 = MacroState({}, {sz.Str("FOO"): sz.Str("2")}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result, macros1, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0, context=self.ctx)

        # Both should include the file (FOO is defined in both cases)
        # But cache keys should be different
        assert 1 in result1.active_lines
        assert 1 in result2.active_lines

        # Different macro values = different cache keys
        stats = get_cache_stats(self.ctx)
        assert stats["misses"] == 2  # Both are misses

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_irrelevant_macro_addition(self):
        """Test that adding irrelevant macros results in cache HIT (optimization)."""
        text = dedent("""
            #ifdef FOO
            #include "foo.h"
            #endif
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_003")
        macros1 = MacroState({}, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")
        macros2 = MacroState({}, {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("1")}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result, macros1, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0, context=self.ctx)

        # Both should have same active lines (FOO is defined in both)
        assert result1.active_lines == result2.active_lines
        assert 1 in result1.active_lines  # #include line is active

        # BAR is not in conditional_macros, so it's ignored in cache key
        # Second call should be a cache HIT (optimization working)
        stats = get_cache_stats(self.ctx)
        assert stats["misses"] == 1  # Only first call is a miss
        assert stats["hits"] == 1  # Second call is a hit (same relevant macros)

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_irrelevant_macro_removal(self):
        """Test that removing irrelevant macros results in cache HIT (optimization)."""
        text = dedent("""
            #ifdef FOO
            #include "foo.h"
            #endif
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_004")
        macros1 = MacroState({}, {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("1")}, anchor_root="")
        macros2 = MacroState({}, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result, macros1, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0, context=self.ctx)

        # Same active lines (FOO unchanged)
        assert result1.active_lines == result2.active_lines

        # BAR is not in conditional_macros, so it's ignored in cache key
        # Second call should be a cache HIT (optimization working)
        stats = get_cache_stats(self.ctx)
        assert stats["misses"] == 1  # Only first call is a miss
        assert stats["hits"] == 1  # Second call is a hit (same relevant macros)

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_relevant_macro_change(self):
        """Test that changing relevant macros creates different cache keys."""
        text = dedent("""
            #ifdef FOO
            #include "foo.h"
            #endif
            #ifdef BAR
            #include "bar.h"
            #endif
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_003b")
        # Both FOO and BAR are in conditional_macros for this file
        macros1 = MacroState({}, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")
        macros2 = MacroState({}, {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("1")}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result, macros1, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0, context=self.ctx)

        # Different active lines (BAR adds the second include)
        assert result1.active_lines != result2.active_lines
        assert len(result1.active_includes) == 1  # Only foo.h
        assert len(result2.active_includes) == 2  # foo.h and bar.h

        # BAR IS in conditional_macros, so it creates a different cache key
        # Both calls should be misses
        stats = get_cache_stats(self.ctx)
        assert stats["misses"] == 2

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_file_change(self):
        """Test that file content changes create different cache keys."""
        text1 = dedent("""
            #ifdef FOO
            #include "test1.h"
            #endif
        """).strip()

        text2 = dedent("""
            #ifdef FOO
            #include "test2.h"
            #endif
        """).strip()

        file_result1 = self._create_simple_file_result(text1, "hash_005a")
        file_result2 = self._create_simple_file_result(text2, "hash_005b")
        macros = MacroState({}, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result1, macros, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result2, macros, 0, context=self.ctx)

        # Both should have active lines (FOO is defined)
        assert 1 in result1.active_lines  # #include line is active
        assert 1 in result2.active_lines  # #include line is active

        # But different includes
        assert len(result1.active_includes) == 1
        assert len(result2.active_includes) == 1
        assert str(result1.active_includes[0]["filename"]) == "test1.h"
        assert str(result2.active_includes[0]["filename"]) == "test2.h"

        # Different content_hash = different cache keys
        stats = get_cache_stats(self.ctx)
        assert stats["misses"] == 2

    def test_macro_state_propagation(self):
        """Test that macro state is correctly returned in updated_macros."""
        text = dedent("""
            #define NEW_MACRO 42
        """).strip()

        # Create file result with define
        lines = text.split("\n")
        line_byte_offsets = [0]

        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="define",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("NEW_MACRO"),
            macro_value=sz.Str("42"),
        )

        file_result = FileAnalysisResult(
            line_count=len(lines),
            line_byte_offsets=line_byte_offsets,
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[directive],
            directive_by_line={0: directive},
            bytes_analyzed=len(text),
            was_truncated=False,
            includes=[],
            defines=[{"line_num": 0, "name": sz.Str("NEW_MACRO"), "value": sz.Str("42"), "is_function_like": False}],
            magic_flags=[],
            content_hash="hash_006",
            include_guard=None,
        )

        initial_macros = MacroState({}, {}, anchor_root="")
        result = get_or_compute_preprocessing(file_result, initial_macros, 0, context=self.ctx)

        # Verify NEW_MACRO is in updated_macros
        assert sz.Str("NEW_MACRO") in result.updated_macros.variable
        assert result.updated_macros.variable[sz.Str("NEW_MACRO")] == sz.Str("42")

        # Verify initial_macros is unchanged (immutable input)
        assert sz.Str("NEW_MACRO") not in initial_macros.variable

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_invariant_cache_honors_undef(self):
        """Ensure invariant cache does not resurrect macros removed via #undef."""

        text = "#undef REMOVED_MACRO\n"

        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="undef",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("REMOVED_MACRO"),
            macro_value=None,
        )

        file_result = FileAnalysisResult(
            line_count=1,
            line_byte_offsets=[0],
            include_positions=[],
            magic_positions=[],
            directive_positions={"undef": [0]},
            directives=[directive],
            directive_by_line={0: directive},
            bytes_analyzed=len(text),
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[],
            content_hash="hash_undef_001",
            include_guard=None,
            conditional_macros=frozenset(),
        )

        initial_macros = MacroState({}, {sz.Str("REMOVED_MACRO"): sz.Str("1")}, anchor_root="")

        # First call computes result and should drop REMOVED_MACRO from updated macros
        result1 = get_or_compute_preprocessing(file_result, initial_macros, 0, context=self.ctx)
        assert sz.Str("REMOVED_MACRO") not in result1.updated_macros.variable, (
            "#undef should remove macro on initial processing"
        )

        # Second call hits invariant cache but must preserve the removal semantics
        result2 = get_or_compute_preprocessing(file_result, initial_macros, 0, context=self.ctx)
        assert sz.Str("REMOVED_MACRO") not in result2.updated_macros.variable, (
            "Invariant cache should not reintroduce macros removed via #undef"
        )

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_empty_macros(self):
        """Test cache behavior with empty macro state."""
        text = dedent("""
            #include "test.h"
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_007")
        empty_macros = MacroState({}, {}, anchor_root="")

        result1 = get_or_compute_preprocessing(file_result, empty_macros, 0, context=self.ctx)
        result2 = get_or_compute_preprocessing(file_result, empty_macros, 0, context=self.ctx)

        # Cache should work with empty macros
        assert result1.active_lines == result2.active_lines

        stats = get_cache_stats(self.ctx)
        assert stats["hits"] == 1

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_cache_stats_accuracy(self):
        """Test that cache statistics are accurate."""
        text = dedent("""
            #include "test.h"
        """).strip()

        file_result = self._create_simple_file_result(text, "hash_008")
        macros = MacroState({}, {}, anchor_root="")

        # Clear stats
        clear_cache(self.ctx)
        initial_stats = get_cache_stats(self.ctx)
        assert initial_stats["entries"] == 0
        assert initial_stats["hits"] == 0
        assert initial_stats["misses"] == 0

        # First call - miss
        get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)
        stats1 = get_cache_stats(self.ctx)
        assert stats1["entries"] == 1
        assert stats1["misses"] == 1
        assert stats1["hits"] == 0

        # Second call - hit
        get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)
        stats2 = get_cache_stats(self.ctx)
        assert stats2["entries"] == 1
        assert stats2["misses"] == 1
        assert stats2["hits"] == 1

        # Third call - hit
        get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)
        stats3 = get_cache_stats(self.ctx)
        assert stats3["hits"] == 2
        assert stats3["hit_rate"] > 66.0  # 2/3 = 66.7%


class TestCacheManagement:
    """Tests for cache management functions."""

    def setup_method(self):
        """Clear cache before each test."""
        self.ctx = BuildContext()
        clear_cache(self.ctx)

        # Mock get_filepath_by_hash since tests don't have real files in registry
        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock_get_filepath = self.patcher.start()
        self.mock_get_filepath.return_value = "<test-file>"

    def teardown_method(self):
        """Clean up after each test method."""
        self.patcher.stop()

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_clear_cache(self):
        """Test cache clearing."""
        text = '#include "test.h"'

        file_result = FileAnalysisResult(
            line_count=1,
            line_byte_offsets=[0],
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[],
            directive_by_line={},
            bytes_analyzed=len(text),
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[],
            content_hash="hash_clear",
            include_guard=None,
        )

        # Add entry to cache
        get_or_compute_preprocessing(file_result, MacroState({}, {}, anchor_root=""), 0, context=self.ctx)
        stats1 = get_cache_stats(self.ctx)
        assert stats1["entries"] == 1

        # Clear cache
        clear_cache(self.ctx)
        stats2 = get_cache_stats(self.ctx)
        assert stats2["entries"] == 0
        assert stats2["hits"] == 0
        assert stats2["misses"] == 0

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="sys.getsizeof not meaningful in PyPy")
    def test_get_cache_stats_memory(self):
        """Test that cache stats include memory information."""
        clear_cache(self.ctx)
        stats = get_cache_stats(self.ctx)

        assert "memory_bytes" in stats
        assert "memory_mb" in stats
        assert stats["memory_bytes"] >= 0
        assert stats["memory_mb"] >= 0.0

    @pytest.mark.skipif(hasattr(sys, "pypy_version_info"), reason="tracemalloc not available in PyPy")
    def test_memory_usage_reasonable(self):
        """Test that cache memory usage stays reasonable."""
        import tracemalloc

        clear_cache(self.ctx)
        tracemalloc.start()

        # Create 100 cache entries
        for i in range(100):
            text = f'#include "test{i}.h"'
            file_result = FileAnalysisResult(
                line_count=1,
                line_byte_offsets=[0],
                include_positions=[],
                magic_positions=[],
                directive_positions={},
                directives=[],
                directive_by_line={},
                bytes_analyzed=len(text),
                was_truncated=False,
                includes=[{"line_num": 0, "filename": sz.Str(f"test{i}.h"), "type": "quoted"}],
                defines=[],
                magic_flags=[],
                content_hash=f"hash_{i:03d}",
                include_guard=None,
            )
            macros = MacroState({}, {sz.Str(f"MACRO_{i}"): sz.Str(str(i))}, anchor_root="")
            get_or_compute_preprocessing(file_result, macros, 0, context=self.ctx)

        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Verify cache has 100 entries
        stats = get_cache_stats(self.ctx)
        assert stats["entries"] == 100

        # Peak memory should be reasonable (< 20MB for 100 entries including baseline overhead)
        peak_mb = peak / (1024 * 1024)
        assert peak_mb < 20.0, f"Peak memory {peak_mb:.1f} MB exceeds 20 MB limit"

        clear_cache(self.ctx)


class TestMacroStateImmutability:
    """Tests for MacroState immutability and with_updates behavior."""

    def test_with_updates_returns_new_instance_on_change(self):
        """with_updates should return a new instance when actual changes occur."""
        state = MacroState(core={}, variable={}, anchor_root="")

        # Add new macro
        new_state = state.with_updates({sz.Str("FOO"): sz.Str("1")})
        assert new_state is not state
        assert sz.Str("FOO") in new_state.variable
        assert sz.Str("FOO") not in state.variable  # Original unchanged

    def test_with_updates_returns_self_on_no_change(self):
        """with_updates should return self when updates don't change state."""
        state = MacroState(core={}, variable={sz.Str("FOO"): sz.Str("1")}, anchor_root="")

        # Update with same value
        new_state = state.with_updates({sz.Str("FOO"): sz.Str("1")})
        assert new_state is state  # Identity equality

        # Update with empty dict
        new_state_empty = state.with_updates({})
        assert new_state_empty is state

    def test_with_updates_returns_new_instance_value_change(self):
        """with_updates should return new instance on value change."""
        state = MacroState(core={}, variable={sz.Str("FOO"): sz.Str("1")}, anchor_root="")

        # Update with different value
        new_state = state.with_updates({sz.Str("FOO"): sz.Str("2")})
        assert new_state is not state
        assert new_state.variable[sz.Str("FOO")] == sz.Str("2")
        assert state.variable[sz.Str("FOO")] == sz.Str("1")

    def test_copy_returns_self(self):
        """copy() should return self for immutable object."""
        state = MacroState(core={}, variable={sz.Str("FOO"): sz.Str("1")}, anchor_root="")
        copied = state.copy()
        assert copied is state

    def test_immutability_enforced(self):
        """Verify that mutation methods are gone."""
        state = MacroState(core={}, variable={}, anchor_root="")

        # Check that setters raise AttributeError (methods removed). The
        # subscripted assignment below is exactly the API we're asserting
        # *doesn't* exist — pyright correctly flags it, hence the ignore.
        try:
            state[sz.Str("FOO")] = sz.Str("1")  # type: ignore[index]
            assert False, "__setitem__ should not exist"
        except TypeError:
            pass  # 'MacroState' object does not support item assignment

        assert not hasattr(state, "update"), "update method should not exist"
        assert not hasattr(state, "get_version"), "get_version method should not exist"

    def test_with_updates_incremental_cache_key_on_pure_addition(self):
        """Pure-addition fast path must produce a cache key equal to a full rebuild.

        The optimization at lines 242-245 sets new_state._cache_key via
        frozenset union when no overwrites are involved. The invariant
        (incremental key == recomputed key) is documented but not currently
        pinned by a test, so a refactor could silently break it.
        """
        state = MacroState(
            core={}, variable={sz.Str("A"): sz.Str("1")}, anchor_root=""
        )
        # Materialize the cache key so the incremental path engages.
        baseline_key = state.get_cache_key()
        assert baseline_key == frozenset({(sz.Str("A"), sz.Str("1"))})

        # Pure addition (B is new, A unchanged).
        new_state = state.with_updates({sz.Str("B"): sz.Str("2")})
        assert new_state is not state

        # The incremental key cached on new_state must equal a full rebuild.
        cached_key = new_state.get_cached_key_if_available()
        rebuilt_key = frozenset(new_state.variable.items())
        assert cached_key is not None, "pure-addition fast path should populate _cache_key"
        assert cached_key == rebuilt_key

        # Overwrite path must NOT pre-populate the incremental key.
        overwrite_state = state.with_updates({sz.Str("A"): sz.Str("9")})
        assert overwrite_state.get_cached_key_if_available() is None


class TestGetHashVariableOnly:
    """Tests for the default include_core=False path of MacroState.get_hash().

    This is the hot path used for preprocessing-cache convergence detection;
    the scoping test file only exercises include_core=True.
    """

    def test_variable_only_hash_changes_with_variable(self):
        s1 = MacroState({}, {sz.Str("V"): sz.Str("1")}, anchor_root="")
        s2 = MacroState({}, {sz.Str("V"): sz.Str("2")}, anchor_root="")
        assert s1.get_hash() != s2.get_hash()

    def test_variable_only_hash_ignores_core_and_build_context(self):
        """Variable-only path must NOT include core or build context."""
        s_plain = MacroState({}, {sz.Str("V"): sz.Str("1")}, anchor_root="")
        s_with_extras = MacroState(
            core={sz.Str("__GNUC__"): sz.Str("13")},
            variable={sz.Str("V"): sz.Str("1")},
            compiler_path="gcc",
            cppflags="-I/usr/include",
            cflags="-O2",
            cxxflags="-std=c++17",
            anchor_root="",
        )
        assert s_plain.get_hash() == s_with_extras.get_hash()

    def test_variable_only_hash_memoised(self):
        """Repeat calls must return the cached value (line 382-383 early return)."""
        state = MacroState({}, {sz.Str("V"): sz.Str("1")}, anchor_root="")
        h1 = state.get_hash()
        # Mutate the underlying variable dict to prove a recompute would
        # produce a different hash — but the cached value must be returned.
        state.variable[sz.Str("V")] = sz.Str("CHANGED")
        h2 = state.get_hash()
        assert h1 == h2

    def test_variable_only_empty_variable_hash_is_zero(self):
        """Empty variable produces the all-zeros XOR result."""
        state = MacroState({}, {}, anchor_root="")
        assert state.get_hash() == "0000000000000000"


class TestCacheHitMacroReconstruction:
    """Regression tests for the anti-pollution invariant: cache hits must
    rebuild updated_macros from the *current* caller's input, not from the
    first caller's context that produced the cached entry.
    """

    def setup_method(self):
        self.ctx = BuildContext()
        clear_cache(self.ctx)
        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock = self.patcher.start()
        self.mock.return_value = "<test-file>"

    def teardown_method(self):
        self.patcher.stop()

    def _file_with_define(self, content_hash: str) -> FileAnalysisResult:
        """File with one #define and no conditionals (invariant)."""
        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="define",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("LOCAL_DEF"),
            macro_value=sz.Str("42"),
        )
        return FileAnalysisResult(
            line_count=1,
            line_byte_offsets=[0],
            include_positions=[],
            magic_positions=[],
            directive_positions={"define": [0]},
            directives=[directive],
            directive_by_line={0: directive},
            bytes_analyzed=18,
            was_truncated=False,
            includes=[],
            defines=[{"line_num": 0, "name": sz.Str("LOCAL_DEF"), "value": sz.Str("42"), "is_function_like": False}],
            magic_flags=[],
            content_hash=content_hash,
            include_guard=None,
            conditional_macros=frozenset(),
        )

    def test_invariant_cache_hit_reapplies_file_defines(self):
        """Invariant cache hit must merge cached file_defines into the
        *current* caller's input macros, not return the first caller's state.
        """
        file_result = self._file_with_define("hash_inv_defines")

        # First caller: empty input. LOCAL_DEF will be captured as a file_define.
        first_input = MacroState({}, {}, anchor_root="")
        first_result = get_or_compute_preprocessing(file_result, first_input, 0, context=self.ctx)
        assert sz.Str("LOCAL_DEF") in first_result.updated_macros.variable
        assert sz.Str("LOCAL_DEF") in first_result.file_defines

        # Second caller: brings its own pre-existing macro OTHER. Cache hits
        # but must combine OTHER (from input) + LOCAL_DEF (from cached file_defines).
        second_input = MacroState({}, {sz.Str("OTHER"): sz.Str("99")}, anchor_root="")
        second_result = get_or_compute_preprocessing(file_result, second_input, 0, context=self.ctx)

        stats = get_cache_stats(self.ctx)
        assert stats["invariant_hits"] == 1, "second call should hit invariant cache"

        # Both inputs and file-defined macros must be present.
        merged = second_result.updated_macros.variable
        assert merged[sz.Str("OTHER")] == sz.Str("99")
        assert merged[sz.Str("LOCAL_DEF")] == sz.Str("42")

    def test_variant_cache_hit_reapplies_file_defines(self):
        """Variant cache hit must also merge cached file_defines into the
        current caller's input (lines 601, 603 in preprocessing_cache.py).
        """
        # File: #ifdef GATE / #define LOCAL_DEF 42 / #endif
        # Variant (has conditional), define becomes active when GATE is set.
        define_directive = PreprocessorDirective(
            line_num=1,
            byte_pos=14,
            directive_type="define",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("LOCAL_DEF"),
            macro_value=sz.Str("42"),
        )
        ifdef_directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="ifdef",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("GATE"),
            macro_value=None,
        )
        endif_directive = PreprocessorDirective(
            line_num=2,
            byte_pos=32,
            directive_type="endif",
            continuation_lines=0,
            condition=None,
            macro_name=None,
            macro_value=None,
        )
        file_result = FileAnalysisResult(
            line_count=3,
            line_byte_offsets=[0, 14, 32],
            include_positions=[],
            magic_positions=[],
            directive_positions={"ifdef": [0], "define": [14], "endif": [32]},
            directives=[ifdef_directive, define_directive, endif_directive],
            directive_by_line={0: ifdef_directive, 1: define_directive, 2: endif_directive},
            bytes_analyzed=40,
            was_truncated=False,
            includes=[],
            defines=[{"line_num": 1, "name": sz.Str("LOCAL_DEF"), "value": sz.Str("42"), "is_function_like": False}],
            magic_flags=[],
            content_hash="hash_var_defines",
            include_guard=None,
            conditional_macros=frozenset({sz.Str("GATE")}),
        )

        # First caller: GATE set so #define becomes active.
        first_input = MacroState({}, {sz.Str("GATE"): sz.Str("1")}, anchor_root="")
        first_result = get_or_compute_preprocessing(file_result, first_input, 0, context=self.ctx)
        assert sz.Str("LOCAL_DEF") in first_result.file_defines
        assert sz.Str("LOCAL_DEF") in first_result.updated_macros.variable

        # Second caller: same relevant key (GATE=1) plus an extra unrelated
        # variable macro that DOES NOT appear in conditional_macros, so the
        # variant cache key matches and we hit.
        second_input = MacroState(
            {},
            {sz.Str("GATE"): sz.Str("1"), sz.Str("OTHER"): sz.Str("99")},
            anchor_root="",
        )
        second_result = get_or_compute_preprocessing(file_result, second_input, 0, context=self.ctx)

        stats = get_cache_stats(self.ctx)
        assert stats["variant_hits"] == 1, "second call should hit variant cache"

        # OTHER preserved from caller; LOCAL_DEF reapplied from cached file_defines.
        merged = second_result.updated_macros.variable
        assert merged[sz.Str("OTHER")] == sz.Str("99")
        assert merged[sz.Str("LOCAL_DEF")] == sz.Str("42")
        assert merged[sz.Str("GATE")] == sz.Str("1")


class TestClearVariantCache:
    """clear_variant_cache must purge variant entries but preserve invariant
    entries. Used in two-pass header discovery convergence."""

    def setup_method(self):
        self.ctx = BuildContext()
        clear_cache(self.ctx)
        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock = self.patcher.start()
        self.mock.return_value = "<test-file>"

    def teardown_method(self):
        self.patcher.stop()

    def test_clear_variant_cache_preserves_invariant_entries(self):
        from compiletools.preprocessing_cache import clear_variant_cache

        # Invariant entry: file with no conditionals.
        invariant_file = FileAnalysisResult(
            line_count=1,
            line_byte_offsets=[0],
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[],
            directive_by_line={},
            bytes_analyzed=18,
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[],
            content_hash="hash_inv",
            include_guard=None,
            conditional_macros=frozenset(),
        )

        # Variant entry: file with an #ifdef referencing a defined macro.
        ifdef_directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="ifdef",
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("GATE"),
            macro_value=None,
        )
        endif_directive = PreprocessorDirective(
            line_num=1,
            byte_pos=14,
            directive_type="endif",
            continuation_lines=0,
            condition=None,
            macro_name=None,
            macro_value=None,
        )
        variant_file = FileAnalysisResult(
            line_count=2,
            line_byte_offsets=[0, 14],
            include_positions=[],
            magic_positions=[],
            directive_positions={"ifdef": [0], "endif": [14]},
            directives=[ifdef_directive, endif_directive],
            directive_by_line={0: ifdef_directive, 1: endif_directive},
            bytes_analyzed=22,
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[],
            content_hash="hash_var",
            include_guard=None,
            conditional_macros=frozenset({sz.Str("GATE")}),
        )

        empty = MacroState({}, {}, anchor_root="")
        with_gate = MacroState({}, {sz.Str("GATE"): sz.Str("1")}, anchor_root="")

        get_or_compute_preprocessing(invariant_file, empty, 0, context=self.ctx)
        get_or_compute_preprocessing(variant_file, with_gate, 0, context=self.ctx)

        before = get_cache_stats(self.ctx)
        assert before["invariant_entries"] == 1
        assert before["variant_entries"] == 1

        clear_variant_cache(self.ctx)

        after = get_cache_stats(self.ctx)
        assert after["invariant_entries"] == 1, "invariant cache must be preserved"
        assert after["variant_entries"] == 0, "variant cache must be cleared"
