"""Tests for unified preprocessing cache."""

import sys
import os
from textwrap import dedent

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import stringzilla as sz
from compiletools.preprocessing_cache import (
    get_or_compute_preprocessing,
    get_cache_stats,
    clear_cache
)
from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective


class TestPreprocessingCache:
    """Tests for unified preprocessing cache correctness."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def _create_simple_file_result(self, text: str, content_hash: str = "test_hash_001") -> FileAnalysisResult:
        """Helper to create FileAnalysisResult for testing."""
        lines = text.split('\n')

        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode('utf-8')) + 1

        # Parse directives for conditional compilation
        directives = []
        directive_by_line = {}

        for line_num, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('#ifdef'):
                macro_name = sz.Str(stripped.split()[1] if len(stripped.split()) > 1 else "")
                directive = PreprocessorDirective(
                    line_num=line_num,
                    byte_pos=line_byte_offsets[line_num],
                    directive_type='ifdef',
                    continuation_lines=0,
                    condition=None,
                    macro_name=macro_name,
                    macro_value=None
                )
                directives.append(directive)
                directive_by_line[line_num] = directive
            elif stripped.startswith('#endif'):
                directive = PreprocessorDirective(
                    line_num=line_num,
                    byte_pos=line_byte_offsets[line_num],
                    directive_type='endif',
                    continuation_lines=0,
                    condition=None,
                    macro_name=None,
                    macro_value=None
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
            if '#include' in line:
                includes.append({
                    'line_num': line_num,
                    'filename': sz.Str(line.split('"')[1] if '"' in line else "test.h"),
                    'type': 'quoted'
                })

        # Extract conditional_macros from directives (critical for cache logic)
        conditional_macros = set()
        for directive in directives:
            if directive.directive_type in ('ifdef', 'ifndef') and directive.macro_name:
                conditional_macros.add(directive.macro_name)

        return FileAnalysisResult(
            line_count=len(lines),
            line_byte_offsets=line_byte_offsets,
            include_positions=[],
            magic_positions=[],
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=len(text.encode('utf-8')),
            was_truncated=False,
            includes=includes,
            defines=[],
            magic_flags=[],
            content_hash=content_hash,
            include_guard=None,
            conditional_macros=frozenset(conditional_macros)
        )

    def test_cache_basic_hit(self):
        """Test basic cache hit scenario."""
        text = dedent('''
            #ifdef TEST_MACRO
            #include "test.h"
            #endif
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_001")
        macros = {sz.Str("TEST_MACRO"): sz.Str("1")}

        # First call - cache miss
        result1 = get_or_compute_preprocessing(file_result, macros, 0)

        # Second call - cache hit
        result2 = get_or_compute_preprocessing(file_result, macros, 0)

        # Results should be identical
        assert result1.active_lines == result2.active_lines
        assert result1.active_includes == result2.active_includes

        # Verify cache was used
        stats = get_cache_stats()
        assert stats['hits'] == 1
        assert stats['misses'] == 1
        assert stats['total_calls'] == 2

    def test_cache_macro_value_change(self):
        """Test that macro value changes produce different results."""
        text = dedent('''
            #ifdef FOO
            #include "enabled.h"
            #endif
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_002")
        macros1 = {sz.Str("FOO"): sz.Str("1")}
        macros2 = {sz.Str("FOO"): sz.Str("2")}

        result1 = get_or_compute_preprocessing(file_result, macros1, 0)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0)

        # Both should include the file (FOO is defined in both cases)
        # But cache keys should be different
        assert 1 in result1.active_lines
        assert 1 in result2.active_lines

        # Different macro values = different cache keys
        stats = get_cache_stats()
        assert stats['misses'] == 2  # Both are misses

    def test_cache_macro_addition(self):
        """Test that adding macros creates different cache keys."""
        text = dedent('''
            #ifdef FOO
            #include "foo.h"
            #endif
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_003")
        macros1 = {sz.Str("FOO"): sz.Str("1")}
        macros2 = {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("1")}

        result1 = get_or_compute_preprocessing(file_result, macros1, 0)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0)

        # Both should have same active lines (FOO is defined in both)
        assert result1.active_lines == result2.active_lines
        assert 1 in result1.active_lines  # #include line is active

        # Different macro sets = different cache keys = both misses
        stats = get_cache_stats()
        assert stats['misses'] == 2

    def test_cache_macro_removal(self):
        """Test that removing macros creates different cache keys."""
        text = dedent('''
            #ifdef FOO
            #include "foo.h"
            #endif
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_004")
        macros1 = {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("1")}
        macros2 = {sz.Str("FOO"): sz.Str("1")}

        result1 = get_or_compute_preprocessing(file_result, macros1, 0)
        result2 = get_or_compute_preprocessing(file_result, macros2, 0)

        # Different macro sets = different results
        assert result1.active_lines == result2.active_lines  # Same active lines

        # But different cache keys
        stats = get_cache_stats()
        assert stats['misses'] == 2

    def test_cache_file_change(self):
        """Test that file content changes create different cache keys."""
        text1 = dedent('''
            #ifdef FOO
            #include "test1.h"
            #endif
        ''').strip()

        text2 = dedent('''
            #ifdef FOO
            #include "test2.h"
            #endif
        ''').strip()

        file_result1 = self._create_simple_file_result(text1, "hash_005a")
        file_result2 = self._create_simple_file_result(text2, "hash_005b")
        macros = {sz.Str("FOO"): sz.Str("1")}

        result1 = get_or_compute_preprocessing(file_result1, macros, 0)
        result2 = get_or_compute_preprocessing(file_result2, macros, 0)

        # Both should have active lines (FOO is defined)
        assert 1 in result1.active_lines  # #include line is active
        assert 1 in result2.active_lines  # #include line is active

        # But different includes
        assert len(result1.active_includes) == 1
        assert len(result2.active_includes) == 1
        assert str(result1.active_includes[0]['filename']) == "test1.h"
        assert str(result2.active_includes[0]['filename']) == "test2.h"

        # Different content_hash = different cache keys
        stats = get_cache_stats()
        assert stats['misses'] == 2

    def test_macro_state_propagation(self):
        """Test that macro state is correctly returned in updated_macros."""
        text = dedent('''
            #define NEW_MACRO 42
        ''').strip()

        # Create file result with define
        lines = text.split('\n')
        line_byte_offsets = [0]

        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type='define',
            continuation_lines=0,
            condition=None,
            macro_name=sz.Str("NEW_MACRO"),
            macro_value=sz.Str("42")
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
            defines=[{'line_num': 0, 'name': sz.Str("NEW_MACRO"), 'value': sz.Str("42"), 'is_function_like': False}],
            magic_flags=[],
            content_hash="hash_006",
            include_guard=None
        )

        initial_macros = {}
        result = get_or_compute_preprocessing(file_result, initial_macros, 0)

        # Verify NEW_MACRO is in updated_macros
        assert sz.Str("NEW_MACRO") in result.updated_macros
        assert result.updated_macros[sz.Str("NEW_MACRO")] == sz.Str("42")

        # Verify initial_macros is unchanged (immutable input)
        assert sz.Str("NEW_MACRO") not in initial_macros

    def test_empty_macros(self):
        """Test cache behavior with empty macro state."""
        text = dedent('''
            #include "test.h"
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_007")
        empty_macros = {}

        result1 = get_or_compute_preprocessing(file_result, empty_macros, 0)
        result2 = get_or_compute_preprocessing(file_result, empty_macros, 0)

        # Cache should work with empty macros
        assert result1.active_lines == result2.active_lines

        stats = get_cache_stats()
        assert stats['hits'] == 1

    def test_cache_stats_accuracy(self):
        """Test that cache statistics are accurate."""
        text = dedent('''
            #include "test.h"
        ''').strip()

        file_result = self._create_simple_file_result(text, "hash_008")
        macros = {}

        # Clear stats
        clear_cache()
        initial_stats = get_cache_stats()
        assert initial_stats['entries'] == 0
        assert initial_stats['hits'] == 0
        assert initial_stats['misses'] == 0

        # First call - miss
        get_or_compute_preprocessing(file_result, macros, 0)
        stats1 = get_cache_stats()
        assert stats1['entries'] == 1
        assert stats1['misses'] == 1
        assert stats1['hits'] == 0

        # Second call - hit
        get_or_compute_preprocessing(file_result, macros, 0)
        stats2 = get_cache_stats()
        assert stats2['entries'] == 1
        assert stats2['misses'] == 1
        assert stats2['hits'] == 1

        # Third call - hit
        get_or_compute_preprocessing(file_result, macros, 0)
        stats3 = get_cache_stats()
        assert stats3['hits'] == 2
        assert stats3['hit_rate'] > 66.0  # 2/3 = 66.7%


class TestCacheManagement:
    """Tests for cache management functions."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def test_clear_cache(self):
        """Test cache clearing."""
        text = "#include \"test.h\""

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
            include_guard=None
        )

        # Add entry to cache
        get_or_compute_preprocessing(file_result, {}, 0)
        stats1 = get_cache_stats()
        assert stats1['entries'] == 1

        # Clear cache
        clear_cache()
        stats2 = get_cache_stats()
        assert stats2['entries'] == 0
        assert stats2['hits'] == 0
        assert stats2['misses'] == 0

    def test_get_cache_stats_memory(self):
        """Test that cache stats include memory information."""
        clear_cache()
        stats = get_cache_stats()

        assert 'memory_bytes' in stats
        assert 'memory_mb' in stats
        assert stats['memory_bytes'] >= 0
        assert stats['memory_mb'] >= 0.0

    def test_memory_usage_reasonable(self):
        """Test that cache memory usage stays reasonable."""
        import tracemalloc

        clear_cache()
        tracemalloc.start()

        # Create 100 cache entries
        for i in range(100):
            text = f"#include \"test{i}.h\""
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
                includes=[{'line_num': 0, 'filename': sz.Str(f"test{i}.h"), 'type': 'quoted'}],
                defines=[],
                magic_flags=[],
                content_hash=f"hash_{i:03d}",
                include_guard=None
            )
            macros = {sz.Str(f"MACRO_{i}"): sz.Str(str(i))}
            get_or_compute_preprocessing(file_result, macros, 0)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Verify cache has 100 entries
        stats = get_cache_stats()
        assert stats['entries'] == 100

        # Peak memory should be reasonable (< 10MB for 100 entries)
        peak_mb = peak / (1024 * 1024)
        assert peak_mb < 10.0, f"Peak memory {peak_mb:.1f} MB exceeds 10 MB limit"

        clear_cache()
