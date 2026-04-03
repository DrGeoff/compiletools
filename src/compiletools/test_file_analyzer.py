"""Tests for file_analyzer module."""

import os
import tempfile
from types import SimpleNamespace

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import FileAnalysisResult, FileAnalyzer, read_file_mmap, read_file_traditional


class TestFileAnalysisResult:
    """Test FileAnalysisResult dataclass."""

    def test_dataclass_creation(self):
        result = FileAnalysisResult(
            line_count=2,
            line_byte_offsets=[0, 5],
            include_positions=[10, 20],
            magic_positions=[5],
            directive_positions={"include": [10, 20], "define": [30]},
            directives=[],
            directive_by_line={},
            bytes_analyzed=100,
            was_truncated=False,
        )

        assert result.line_count == 2
        assert list(result.line_byte_offsets) == [0, 5]
        assert result.include_positions == [10, 20]
        assert result.magic_positions == [5]
        assert result.directive_positions == {"include": [10, 20], "define": [30]}
        assert result.bytes_analyzed == 100
        assert result.was_truncated is False


class TestFileAnalyzer:
    """Test FileAnalyzer implementation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.test_files = {}
        self.ctx = BuildContext()

    def teardown_method(self):
        """Clean up test files."""
        for filepath in self.test_files.values():
            try:
                os.unlink(filepath)
            except OSError:
                pass

    def create_test_file(self, filename, content):
        """Helper to create temporary test files."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(content)
        self.test_files[filename] = f.name
        return f.name

    def test_simple_include_file(self):
        """Test FileAnalyzer on a simple file with includes."""
        content = """#include <stdio.h>
#include <stdlib.h>
// #include "commented.h"
int main() {
    return 0;
}"""

        filepath = self.create_test_file("test.c", content)
        from compiletools.global_hash_registry import get_file_hash

        content_hash = get_file_hash(filepath, self.ctx)
        args = SimpleNamespace(max_read_size=0, verbose=0)
        analyzer = FileAnalyzer(content_hash, args, self.ctx)
        result = analyzer.analyze()

        # Should have 2 include positions (not the commented one)
        assert len(result.include_positions) == 2
        # Verify the includes were detected correctly
        assert len(result.includes) == 2
        include_files = [inc["filename"] for inc in result.includes]
        assert "stdio.h" in include_files
        assert "stdlib.h" in include_files

    def test_magic_flags_detection(self):
        """Test magic flags detection."""
        content = """// Magic flags test
//#LIBS=pthread m
//#CFLAGS=-O2 -g
#include <stdio.h>
int main() {
    return 0;
}"""

        filepath = self.create_test_file("magic.c", content)
        from compiletools.global_hash_registry import get_file_hash

        content_hash = get_file_hash(filepath, self.ctx)
        args = SimpleNamespace(max_read_size=0, verbose=0)
        analyzer = FileAnalyzer(content_hash, args, self.ctx)
        result = analyzer.analyze()

        # Should detect 2 magic flags
        assert len(result.magic_positions) == 2
        assert len(result.magic_flags) == 2

        # Check magic flag content
        magic_keys = [flag["key"] for flag in result.magic_flags]
        assert "LIBS" in magic_keys
        assert "CFLAGS" in magic_keys


class TestReadFunctions:
    """Test different file reading functions."""

    def setup_method(self):
        """Set up test fixtures."""
        self.test_files = {}

    def teardown_method(self):
        """Clean up test files."""
        for filepath in self.test_files.values():
            try:
                os.unlink(filepath)
            except OSError:
                pass

    def create_test_file(self, filename, content):
        """Helper to create temporary test files."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(content)
        self.test_files[filename] = f.name
        return f.name

    def test_read_file_mmap(self):
        """Test memory-mapped file reading."""
        content = "Hello\nWorld\nTest"
        filepath = self.create_test_file("mmap_test.c", content)

        # Test full file read
        text, bytes_analyzed, was_truncated = read_file_mmap(filepath, 0)
        assert text == content
        assert bytes_analyzed == len(content.encode("utf-8"))
        assert was_truncated is False

        # Test limited read
        text_limited, bytes_limited, was_truncated_limited = read_file_mmap(filepath, 5)
        assert len(text_limited.encode("utf-8")) <= 5
        assert bytes_limited <= 5
        assert was_truncated_limited is True

    def test_read_file_traditional(self):
        """Test traditional file reading."""
        content = "Traditional\nFile\nReading"
        filepath = self.create_test_file("traditional_test.c", content)

        # Test full file read
        text, bytes_analyzed, was_truncated = read_file_traditional(filepath, 0)
        assert text == content
        assert bytes_analyzed == len(content.encode("utf-8"))
        assert was_truncated is False

        # Test limited read
        text_limited, bytes_limited, was_truncated_limited = read_file_traditional(filepath, 8)
        assert len(text_limited.encode("utf-8")) <= 8
        assert bytes_limited <= 8
        assert was_truncated_limited is True

    def test_read_functions_consistency(self):
        """Test that mmap and traditional reading produce identical results."""
        content = "Consistency\nTest\nFile\nWith\nMultiple\nLines"
        filepath = self.create_test_file("consistency_test.c", content)

        # Read with both methods
        mmap_text, mmap_bytes, mmap_truncated = read_file_mmap(filepath, 0)
        trad_text, trad_bytes, trad_truncated = read_file_traditional(filepath, 0)

        # Results should be identical
        assert mmap_text == trad_text
        assert mmap_bytes == trad_bytes
        assert mmap_truncated == trad_truncated

    def test_empty_file_handling(self):
        """Test handling of empty files."""
        filepath = self.create_test_file("empty_test.c", "")

        # Both methods should handle empty files gracefully
        mmap_text, mmap_bytes, mmap_truncated = read_file_mmap(filepath, 0)
        trad_text, trad_bytes, trad_truncated = read_file_traditional(filepath, 0)

        assert mmap_text == ""
        assert trad_text == ""
        assert mmap_bytes == 0
        assert trad_bytes == 0
        assert mmap_truncated is False
        assert trad_truncated is False


class TestCommentDetection:
    """Test comment detection functions."""

    def test_is_position_commented_in_line_comment(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_position_commented_simd_optimized

        text = sz.Str("int x; // comment\nint y;")
        offsets = [0, 18]
        # Position inside the comment
        assert is_position_commented_simd_optimized(text, 10, offsets) is True

    def test_is_position_commented_not_commented(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_position_commented_simd_optimized

        text = sz.Str("int x = 5;\nint y;")
        offsets = [0, 11]
        assert is_position_commented_simd_optimized(text, 4, offsets) is False

    def test_is_position_commented_empty_offsets(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_position_commented_simd_optimized

        text = sz.Str("// all comment")
        offsets = [0]
        assert is_position_commented_simd_optimized(text, 5, offsets) is True

    def test_is_inside_block_comment_no_comments(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_inside_block_comment_simd

        text = sz.Str("int x = 5;")
        assert is_inside_block_comment_simd(text, 4) is False

    def test_is_inside_block_comment_inside(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_inside_block_comment_simd

        text = sz.Str("/* comment */ int x;")
        assert is_inside_block_comment_simd(text, 5) is True

    def test_is_inside_block_comment_outside(self):
        import stringzilla as sz

        from compiletools.file_analyzer import is_inside_block_comment_simd

        text = sz.Str("/* comment */ int x;")
        assert is_inside_block_comment_simd(text, 15) is False


class TestParseDirectiveStruct:
    """Test parse_directive_struct()."""

    def test_parse_define_no_value(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("define", 0, 0, [sz.Str("#define FOO")])
        assert result.directive_type == "define"
        assert str(result.macro_name) == "FOO"
        assert result.macro_value is None

    def test_parse_define_with_value(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("define", 0, 0, [sz.Str("#define FOO 42")])
        assert str(result.macro_name) == "FOO"
        assert str(result.macro_value) == "42"

    def test_parse_ifdef(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("ifdef", 0, 0, [sz.Str("#ifdef MY_MACRO")])
        assert result.directive_type == "ifdef"
        assert str(result.macro_name) == "MY_MACRO"

    def test_parse_directive_empty_content(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("endif", 0, 0, [sz.Str("#endif")])
        assert result.directive_type == "endif"

    def test_parse_include_directive(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("include", 0, 0, [sz.Str('#include "foo.h"')])
        assert result.directive_type == "include"
        assert '"foo.h"' in str(result.condition)


class TestCacheStatsFunctions:
    """Test cache stats and clear functions."""

    def test_get_cache_stats(self):
        from compiletools.file_analyzer import get_cache_stats

        ctx = BuildContext()
        stats = get_cache_stats(ctx)
        assert "cache_size" in stats

    def test_print_cache_stats(self, capsys):
        from compiletools.file_analyzer import print_cache_stats

        ctx = BuildContext()
        print_cache_stats(ctx)
        captured = capsys.readouterr()
        assert "Cache" in captured.out

    def test_cache_clear(self):
        from compiletools.file_analyzer import cache_clear

        ctx = BuildContext()
        cache_clear(ctx)  # Should not raise


class TestReadFileTraditionalEdgeCases:
    """Test read_file_traditional edge cases."""

    def test_nonexistent_file_returns_empty(self):
        # read_file_traditional catches OSError and returns empty content
        text, bytes_analyzed, was_truncated = read_file_traditional("/nonexistent/path/file.c", 0)
        assert text == ""
        assert bytes_analyzed == 0
        assert was_truncated is False


class TestMarkerType:
    """Test MarkerType enum."""

    def test_marker_values(self):
        from compiletools.file_analyzer import MarkerType

        assert MarkerType.NONE.value == 0
        assert MarkerType.EXE.value == 1
        assert MarkerType.TEST.value == 2
        assert MarkerType.LIBRARY.value == 3


class TestFileAnalyzerFactory:
    """Test FileAnalyzer constructor."""

    def test_analyzer_constructor(self):
        """Test that FileAnalyzer constructor works correctly."""
        ctx = BuildContext()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int main() { return 0; }")
            filepath = f.name

        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, ctx)
            args = SimpleNamespace(max_read_size=0, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, ctx)
            assert isinstance(analyzer, FileAnalyzer)
        finally:
            os.unlink(filepath)

    def test_analyzer_null_hash_raises(self):
        """Test that FileAnalyzer raises ValueError for None content_hash."""
        import pytest

        ctx = BuildContext()
        args = SimpleNamespace(max_read_size=0, verbose=0)
        with pytest.raises(ValueError, match="content_hash must be provided"):
            FileAnalyzer(None, args, ctx)


class TestShouldReadEntireFile:
    """Test FileAnalyzer._should_read_entire_file method."""

    def setup_method(self):
        self.ctx = BuildContext()

    def test_max_read_size_zero_reads_entire(self):
        """When max_read_size is 0, should always read entire file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int x;")
            filepath = f.name
        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, self.ctx)
            args = SimpleNamespace(max_read_size=0, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, self.ctx)
            assert analyzer._should_read_entire_file(1000) is True
        finally:
            os.unlink(filepath)

    def test_file_smaller_than_max(self):
        """When file is smaller than max_read_size, should read entire file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int x;")
            filepath = f.name
        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, self.ctx)
            args = SimpleNamespace(max_read_size=1000, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, self.ctx)
            assert analyzer._should_read_entire_file(500) is True
        finally:
            os.unlink(filepath)

    def test_file_larger_than_max(self):
        """When file is larger than max_read_size, should not read entire file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int x;")
            filepath = f.name
        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, self.ctx)
            args = SimpleNamespace(max_read_size=100, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, self.ctx)
            assert analyzer._should_read_entire_file(500) is False
        finally:
            os.unlink(filepath)

    def test_no_file_size_with_max_set(self):
        """When file_size is None and max_read_size is set, should not read entire."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int x;")
            filepath = f.name
        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, self.ctx)
            args = SimpleNamespace(max_read_size=100, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, self.ctx)
            assert analyzer._should_read_entire_file(None) is False
        finally:
            os.unlink(filepath)


class TestInstanceBulkMethods:
    """Test FileAnalyzer instance-level bulk search methods (fallback paths)."""

    def setup_method(self):
        self.ctx = BuildContext()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write("int x;")
            self.filepath = f.name
        from compiletools.global_hash_registry import get_file_hash

        content_hash = get_file_hash(self.filepath, self.ctx)
        args = SimpleNamespace(max_read_size=0, verbose=0)
        self.analyzer = FileAnalyzer(content_hash, args, self.ctx)

    def teardown_method(self):
        os.unlink(self.filepath)

    def test_find_include_positions_simd_bulk(self):
        """Test instance _find_include_positions_simd_bulk method."""
        import stringzilla as sz

        text = sz.Str('#include <stdio.h>\n#include "foo.h"\n// #include "bar.h"\n')
        offsets = [0, 19, 36, 55]
        positions = self.analyzer._find_include_positions_simd_bulk(text, offsets)
        assert len(positions) == 2  # commented include excluded

    def test_find_magic_positions_simd_bulk(self):
        """Test instance _find_magic_positions_simd_bulk method."""
        import stringzilla as sz

        text = sz.Str('//#LIBS=pthread\n//#CFLAGS=-O2\nint x;\n')
        offsets = [0, 15, 29, 36]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 2

    def test_find_magic_positions_with_prefix_text(self):
        """Test magic positions are rejected when non-whitespace precedes //#."""
        import stringzilla as sz

        text = sz.Str('int x; //#LIBS=pthread\n//#CFLAGS=-O2\n')
        offsets = [0, 22, 36]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1  # first one rejected due to prefix

    def test_find_magic_positions_in_block_comment(self):
        """Test magic positions inside block comments are rejected."""
        import stringzilla as sz

        text = sz.Str('/* //#LIBS=pthread */\n//#CFLAGS=-O2\n')
        offsets = [0, 21, 35]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_magic_positions_empty_key(self):
        """Test magic positions with empty key are rejected."""
        import stringzilla as sz

        text = sz.Str('//# =value\n//#VALID=yes\n')
        offsets = [0, 11, 23]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_magic_positions_invalid_key_chars(self):
        """Test magic positions with invalid key characters are rejected."""
        import stringzilla as sz

        text = sz.Str('//#KEY$=value\n//#VALID=yes\n')
        offsets = [0, 14, 26]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_magic_positions_key_starts_with_digit(self):
        """Test magic positions where key starts with digit are rejected."""
        import stringzilla as sz

        text = sz.Str('//#1KEY=value\n//#VALID=yes\n')
        offsets = [0, 14, 26]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_magic_positions_no_equals(self):
        """Test magic positions without = sign are rejected."""
        import stringzilla as sz

        text = sz.Str('//# just a comment\n//#VALID=yes\n')
        offsets = [0, 19, 31]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_magic_positions_last_line(self):
        """Test magic positions on the last line (no trailing newline)."""
        import stringzilla as sz

        text = sz.Str('//#LIBS=pthread')
        offsets = [0]
        positions = self.analyzer._find_magic_positions_simd_bulk(text, offsets)
        assert len(positions) == 1

    def test_find_directive_positions_simd_bulk(self):
        """Test instance _find_directive_positions_simd_bulk method."""
        import stringzilla as sz

        text = sz.Str('#include <stdio.h>\n#define FOO 1\n#ifdef BAR\n#endif\n')
        offsets = [0, 19, 33, 44, 51]
        positions = self.analyzer._find_directive_positions_simd_bulk(text, offsets)
        assert "include" in positions
        assert "define" in positions
        assert "ifdef" in positions
        assert "endif" in positions

    def test_find_directive_positions_with_indented_directives(self):
        """Test directives with leading whitespace are detected."""
        import stringzilla as sz

        text = sz.Str('  #define FOO\n\t#ifdef BAR\n')
        offsets = [0, 14, 26]
        positions = self.analyzer._find_directive_positions_simd_bulk(text, offsets)
        assert "define" in positions
        assert "ifdef" in positions

    def test_find_directive_positions_hash_in_code_ignored(self):
        """Test that # in non-directive context is ignored."""
        import stringzilla as sz

        text = sz.Str('x = a # b;\n#define FOO\n')
        offsets = [0, 11, 23]
        positions = self.analyzer._find_directive_positions_simd_bulk(text, offsets)
        # The # in "a # b" has non-whitespace before it, so rejected
        assert positions.get("define") is not None
        assert len(positions.get("define", [])) == 1

    def test_find_directive_positions_all_types(self):
        """Test all directive types are recognized."""
        import stringzilla as sz

        source = (
            '#if 1\n#ifdef A\n#ifndef B\n#elif 0\n#else\n'
            '#endif\n#define C\n#undef D\n#include <x>\n'
            '#pragma once\n#error msg\n#warning msg\n#line 1\n'
        )
        text = sz.Str(source)
        offsets = [0]
        pos = 0
        for ch in source:
            if ch == '\n':
                offsets.append(pos + 1)
            pos += 1
        positions = self.analyzer._find_directive_positions_simd_bulk(text, offsets)
        expected = {"if", "ifdef", "ifndef", "elif", "else", "endif",
                    "define", "undef", "include", "pragma", "error", "warning", "line"}
        assert set(positions.keys()) == expected


class TestGetDirectiveLineNumbers:
    """Test FileAnalysisResult.get_directive_line_numbers method."""

    def test_basic(self):
        result = FileAnalysisResult(
            line_count=5,
            line_byte_offsets=[0, 10, 20, 30, 40],
            include_positions=[],
            magic_positions=[],
            directive_positions={"define": [0, 20], "include": [10]},
            directives=[],
            directive_by_line={},
            bytes_analyzed=50,
            was_truncated=False,
        )
        line_nums = result.get_directive_line_numbers()
        assert line_nums["define"] == {0, 2}
        assert line_nums["include"] == {1}


class TestDetectIncludeGuard:
    """Test detect_include_guard function."""

    def test_pragma_once(self):
        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="pragma",
                                  continuation_lines=0, macro_name="once"),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="define",
                                  continuation_lines=0, macro_name="FOO"),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "pragma_once"

    def test_pragma_once_via_condition(self):
        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="pragma",
                                  continuation_lines=0, condition="once"),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="define",
                                  continuation_lines=0, macro_name="FOO"),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "pragma_once"

    def test_traditional_include_guard(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="ifndef",
                                  continuation_lines=0, macro_name=sz.Str("MY_HEADER_H")),
            PreprocessorDirective(line_num=1, byte_pos=20, directive_type="define",
                                  continuation_lines=0, macro_name=sz.Str("MY_HEADER_H")),
            PreprocessorDirective(line_num=10, byte_pos=100, directive_type="endif",
                                  continuation_lines=0),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "MY_HEADER_H"

    def test_no_guard_empty(self):
        from compiletools.file_analyzer import detect_include_guard

        assert detect_include_guard([]) is None

    def test_no_guard_too_few_directives(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="ifndef",
                                  continuation_lines=0, macro_name=sz.Str("X")),
            PreprocessorDirective(line_num=1, byte_pos=10, directive_type="define",
                                  continuation_lines=0, macro_name=sz.Str("X")),
        ]
        assert detect_include_guard(directives) is None

    def test_no_guard_last_not_endif(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="ifndef",
                                  continuation_lines=0, macro_name=sz.Str("X")),
            PreprocessorDirective(line_num=1, byte_pos=10, directive_type="define",
                                  continuation_lines=0, macro_name=sz.Str("X")),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="define",
                                  continuation_lines=0, macro_name=sz.Str("Y")),
        ]
        assert detect_include_guard(directives) is None

    def test_no_guard_mismatched_names(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="ifndef",
                                  continuation_lines=0, macro_name=sz.Str("X")),
            PreprocessorDirective(line_num=1, byte_pos=10, directive_type="define",
                                  continuation_lines=0, macro_name=sz.Str("Y")),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="endif",
                                  continuation_lines=0),
        ]
        assert detect_include_guard(directives) is None


class TestAnalyzeFileFeatures:
    """Test analyze_file with various source file features."""

    def setup_method(self):
        self.test_files = []
        self.ctx = BuildContext()

    def teardown_method(self):
        for fp in self.test_files:
            try:
                os.unlink(fp)
            except OSError:
                pass

    _counter = 0

    def _create_and_analyze(self, content, **extra_args):
        # Add unique comment to avoid hash collisions across tests
        TestAnalyzeFileFeatures._counter += 1
        unique_content = f"// unique-{TestAnalyzeFileFeatures._counter}-{id(self)}\n{content}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(unique_content)
            filepath = f.name
        self.test_files.append(filepath)
        from compiletools.global_hash_registry import get_file_hash

        content_hash = get_file_hash(filepath, self.ctx)
        args = SimpleNamespace(max_read_size=0, verbose=0, **extra_args)
        analyzer = FileAnalyzer(content_hash, args, self.ctx)
        return analyzer.analyze()

    def test_defines_extraction(self):
        """Test that #define directives are extracted with names and values."""
        content = '#define FOO 42\n#define BAR\n#define MAX(a,b) ((a)>(b)?(a):(b))\n'
        result = self._create_and_analyze(content)
        names = [str(d["name"]) for d in result.defines]
        assert "FOO" in names
        assert "BAR" in names
        assert "MAX" in names
        # Check FOO has value
        foo = next(d for d in result.defines if str(d["name"]) == "FOO")
        assert foo["value"] is not None
        assert "42" in str(foo["value"])
        # Check BAR has no value
        bar = next(d for d in result.defines if str(d["name"]) == "BAR")
        assert bar["value"] is None
        # Check MAX is function-like
        maxd = next(d for d in result.defines if str(d["name"]) == "MAX")
        assert maxd["is_function_like"] is True

    def test_include_guard_excluded_from_defines(self):
        """Test that include guard defines are excluded from the defines list."""
        content = '#ifndef MY_GUARD_H\n#define MY_GUARD_H\nint x;\n#endif\n'
        result = self._create_and_analyze(content)
        names = [str(d["name"]) for d in result.defines]
        assert "MY_GUARD_H" not in names
        assert result.include_guard is not None
        assert str(result.include_guard) == "MY_GUARD_H"

    def test_marker_type_exe(self):
        """Test exe marker detection."""
        content = 'int main() { return 0; }\n'
        result = self._create_and_analyze(content, exemarkers=["int main("], testmarkers=[], librarymarkers=[])
        from compiletools.file_analyzer import MarkerType
        assert result.marker_type == MarkerType.EXE

    def test_marker_type_test(self):
        """Test test marker detection."""
        content = '#include "unit_test.hpp"\nTEST(foo) {}\n'
        result = self._create_and_analyze(content, exemarkers=["int main("], testmarkers=["unit_test.hpp"], librarymarkers=[])
        from compiletools.file_analyzer import MarkerType
        assert result.marker_type == MarkerType.TEST

    def test_marker_type_library(self):
        """Test library marker detection."""
        content = '//#LIBRARY=mylib\nvoid helper() {}\n'
        result = self._create_and_analyze(content, exemarkers=["int main("], testmarkers=["unit_test.hpp"], librarymarkers=["//#LIBRARY="])
        from compiletools.file_analyzer import MarkerType
        assert result.marker_type == MarkerType.LIBRARY

    def test_marker_type_none(self):
        """Test no marker detected."""
        content = 'void helper() {}\n'
        result = self._create_and_analyze(content, exemarkers=["int main("], testmarkers=["unit_test.hpp"], librarymarkers=[])
        from compiletools.file_analyzer import MarkerType
        assert result.marker_type == MarkerType.NONE

    def test_no_directives_file(self):
        """Test analysis of file with no directives."""
        result = self._create_and_analyze("int x;\n")
        assert result.was_truncated is False
        assert len(result.includes) == 0
        assert len(result.defines) == 0

    def test_truncated_file(self):
        """Test analysis with max_read_size limiting the read."""
        content = '#include <stdio.h>\n' * 100  # Large content
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(content)
            filepath = f.name
        self.test_files.append(filepath)
        from compiletools.global_hash_registry import get_file_hash

        # Use fresh context to avoid cache hits from other tests
        ctx = BuildContext()
        content_hash = get_file_hash(filepath, ctx)
        args = SimpleNamespace(max_read_size=50, verbose=0)
        analyzer = FileAnalyzer(content_hash, args, ctx)
        result = analyzer.analyze()
        assert result.was_truncated is True
        assert result.bytes_analyzed <= 50

    def test_conditional_macros_extraction(self):
        """Test that macros in #ifdef/#if conditions are extracted."""
        content = '#ifdef DEBUG\nint x;\n#endif\n#if defined(FEATURE_A) && FEATURE_B\nint y;\n#endif\n'
        result = self._create_and_analyze(content)
        macro_names = {str(m) for m in result.conditional_macros}
        assert "DEBUG" in macro_names
        assert "FEATURE_A" in macro_names
        assert "FEATURE_B" in macro_names

    def test_undef_targets(self):
        """Test that #undef targets are tracked."""
        content = '#define FOO 1\n#undef FOO\n#undef BAR\n'
        result = self._create_and_analyze(content)
        undef_names = {str(u) for u in result.undef_targets}
        assert "FOO" in undef_names
        assert "BAR" in undef_names

    def test_system_and_quoted_headers(self):
        """Test system vs quoted header classification."""
        content = '#include <vector>\n#include "myheader.h"\n'
        result = self._create_and_analyze(content)
        system_header_strs = {str(h) for h in result.system_headers}
        quoted_header_strs = {str(h) for h in result.quoted_headers}
        assert "vector" in system_header_strs
        assert "myheader.h" in quoted_header_strs

    def test_multiline_define(self):
        """Test multi-line #define with backslash continuations."""
        content = '#define MACRO(x) \\\n  do { \\\n    x; \\\n  } while(0)\nint y;\n'
        result = self._create_and_analyze(content)
        names = [str(d["name"]) for d in result.defines]
        assert "MACRO" in names

    def test_pragma_once_guard(self):
        """Test #pragma once detection."""
        content = '#pragma once\nint x;\n'
        result = self._create_and_analyze(content)
        assert result.include_guard is not None
        assert str(result.include_guard) == "pragma_once"


class TestPrintCacheStatsWithCalls:
    """Test print_cache_stats with actual cache activity."""

    def test_print_cache_stats_with_hits(self, capsys):
        """Test that hit rate is computed when there are calls."""
        import time

        from compiletools.file_analyzer import print_cache_stats

        ctx = BuildContext()
        # Create a file with unique content to avoid hash collisions
        unique = f"int x_{time.time_ns()};"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".c", delete=False) as f:
            f.write(unique)
            filepath = f.name
        try:
            from compiletools.global_hash_registry import get_file_hash

            content_hash = get_file_hash(filepath, ctx)
            args = SimpleNamespace(max_read_size=0, verbose=0)
            analyzer = FileAnalyzer(content_hash, args, ctx)
            analyzer.analyze()  # miss
            analyzer.analyze()  # hit
            print_cache_stats(ctx)
            captured = capsys.readouterr()
            assert "Cache size:" in captured.out
        finally:
            os.unlink(filepath)


class TestAddArguments:
    """Test FileAnalyzer.add_arguments static method."""

    def test_add_arguments(self):
        """Test that add_arguments adds expected flags."""
        import configargparse

        cap = configargparse.ArgParser()
        FileAnalyzer.add_arguments(cap)
        # Parse with defaults
        args = cap.parse_args([])
        assert args.use_mmap is True
        assert args.force_mmap is False
        assert args.suppress_fd_warnings is False
        assert args.suppress_filesystem_warnings is False


class TestDetermineFileReadingStrategy:
    """Test _determine_file_reading_strategy."""

    def test_no_mmap_flag(self):
        from compiletools.file_analyzer import set_analyzer_args

        ctx = BuildContext()
        args = SimpleNamespace(use_mmap=False)
        set_analyzer_args(args, ctx)
        assert ctx.file_reading_strategy == "no_mmap"

    def test_force_mmap_flag(self):
        from compiletools.file_analyzer import set_analyzer_args

        ctx = BuildContext()
        args = SimpleNamespace(use_mmap=True, force_mmap=True)
        set_analyzer_args(args, ctx)
        assert ctx.file_reading_strategy == "mmap"


class TestExtractConditionalMacros:
    """Test _extract_conditional_macros function."""

    def test_ifdef_macros(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, _extract_conditional_macros

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="ifdef",
                                  continuation_lines=0, macro_name=sz.Str("DEBUG")),
            PreprocessorDirective(line_num=1, byte_pos=10, directive_type="ifndef",
                                  continuation_lines=0, macro_name=sz.Str("NDEBUG")),
        ]
        macros = _extract_conditional_macros(directives)
        names = {str(m) for m in macros}
        assert "DEBUG" in names
        assert "NDEBUG" in names

    def test_if_elif_conditions(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, _extract_conditional_macros

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="if",
                                  continuation_lines=0, condition=sz.Str("defined(FOO) && BAR > 1")),
            PreprocessorDirective(line_num=5, byte_pos=50, directive_type="elif",
                                  continuation_lines=0, condition=sz.Str("BAZ")),
        ]
        macros = _extract_conditional_macros(directives)
        names = {str(m) for m in macros}
        assert "FOO" in names
        assert "BAR" in names
        assert "BAZ" in names
        # "defined" is a keyword and should be excluded
        assert "defined" not in names

    def test_no_conditionals(self):
        from compiletools.file_analyzer import PreprocessorDirective, _extract_conditional_macros

        directives = [
            PreprocessorDirective(line_num=0, byte_pos=0, directive_type="define",
                                  continuation_lines=0, macro_name="X"),
        ]
        macros = _extract_conditional_macros(directives)
        assert len(macros) == 0


class TestParseDirectiveStructExtended:
    """Additional tests for parse_directive_struct edge cases."""

    def test_parse_ifndef(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("ifndef", 0, 0, [sz.Str("#ifndef GUARD_H")])
        assert result.directive_type == "ifndef"
        assert str(result.macro_name) == "GUARD_H"

    def test_parse_undef(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("undef", 0, 0, [sz.Str("#undef OLD_MACRO")])
        assert result.directive_type == "undef"
        assert str(result.macro_name) == "OLD_MACRO"

    def test_parse_if_condition(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("if", 0, 0, [sz.Str("#if defined(X) && Y > 1")])
        assert result.directive_type == "if"
        assert "defined(X)" in str(result.condition)

    def test_parse_elif_condition(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("elif", 0, 0, [sz.Str("#elif Z == 0")])
        assert result.directive_type == "elif"
        assert "Z == 0" in str(result.condition)

    def test_parse_pragma(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("pragma", 0, 0, [sz.Str("#pragma once")])
        assert result.directive_type == "pragma"
        assert str(result.macro_name) == "once"

    def test_parse_define_function_like(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("define", 0, 0, [sz.Str("#define MAX(a,b) ((a)>(b)?(a):(b))")])
        assert result.directive_type == "define"
        assert str(result.macro_name) == "MAX"

    def test_parse_multiline_directive(self):
        import stringzilla as sz

        from compiletools.file_analyzer import parse_directive_struct

        result = parse_directive_struct("define", 0, 0, [sz.Str("#define LONG \\"), sz.Str("  value")])
        assert result.directive_type == "define"
        assert str(result.macro_name) == "LONG"
