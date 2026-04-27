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
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="pragma", continuation_lines=0, macro_name="once"
            ),
            PreprocessorDirective(
                line_num=2, byte_pos=20, directive_type="define", continuation_lines=0, macro_name="FOO"
            ),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "pragma_once"

    def test_pragma_once_via_condition(self):
        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="pragma", continuation_lines=0, condition="once"
            ),
            PreprocessorDirective(
                line_num=2, byte_pos=20, directive_type="define", continuation_lines=0, macro_name="FOO"
            ),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "pragma_once"

    def test_traditional_include_guard(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("MY_HEADER_H")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=20, directive_type="define", continuation_lines=0, macro_name=sz.Str("MY_HEADER_H")
            ),
            PreprocessorDirective(line_num=10, byte_pos=100, directive_type="endif", continuation_lines=0),
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
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("X")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="define", continuation_lines=0, macro_name=sz.Str("X")
            ),
        ]
        assert detect_include_guard(directives) is None

    def test_no_guard_last_not_endif(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("X")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="define", continuation_lines=0, macro_name=sz.Str("X")
            ),
            PreprocessorDirective(
                line_num=2, byte_pos=20, directive_type="define", continuation_lines=0, macro_name=sz.Str("Y")
            ),
        ]
        assert detect_include_guard(directives) is None

    def test_no_guard_mismatched_names(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, detect_include_guard

        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("X")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="define", continuation_lines=0, macro_name=sz.Str("Y")
            ),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="endif", continuation_lines=0),
        ]
        assert detect_include_guard(directives) is None


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
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifdef", continuation_lines=0, macro_name=sz.Str("DEBUG")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("NDEBUG")
            ),
        ]
        macros = _extract_conditional_macros(directives)
        names = {str(m) for m in macros}
        assert "DEBUG" in names
        assert "NDEBUG" in names

    def test_if_elif_conditions(self):
        import stringzilla as sz

        from compiletools.file_analyzer import PreprocessorDirective, _extract_conditional_macros

        directives = [
            PreprocessorDirective(
                line_num=0,
                byte_pos=0,
                directive_type="if",
                continuation_lines=0,
                condition=sz.Str("defined(FOO) && BAR > 1"),
            ),
            PreprocessorDirective(
                line_num=5, byte_pos=50, directive_type="elif", continuation_lines=0, condition=sz.Str("BAZ")
            ),
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
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="define", continuation_lines=0, macro_name="X"
            ),
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
