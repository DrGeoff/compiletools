"""Tests for file_analyzer module."""

from types import SimpleNamespace

import configargparse
import stringzilla as sz

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import (
    FileAnalysisResult,
    MarkerType,
    PreprocessorDirective,
    _compute_line_byte_offsets,
    _extract_conditional_macros,
    _extract_defines,
    _extract_directives,
    _extract_includes,
    _extract_magic_flags,
    _include_positions_from_directives,
    add_arguments,
    cache_clear,
    detect_include_guard,
    find_directive_positions_simd_bulk,
    find_include_positions_simd_bulk,
    find_magic_positions_simd_bulk,
    get_cache_stats,
    is_inside_block_comment_simd,
    is_position_commented_simd_optimized,
    parse_directive_struct,
    print_cache_stats,
    read_file_mmap,
    read_file_traditional,
    set_analyzer_args,
)


def _parse_directive(directive_type, line, *continuation_lines):
    lines = [sz.Str(line), *[sz.Str(continuation) for continuation in continuation_lines]]
    return parse_directive_struct(directive_type, 0, 0, lines)


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

    def create_test_file(self, tmp_path, filename, content):
        """Helper to create temporary test files."""
        filepath = tmp_path / filename
        filepath.write_text(content)
        return str(filepath)

    def assert_reader_handles_full_and_limited_read(self, tmp_path, reader, filename, content, limit):
        filepath = self.create_test_file(tmp_path, filename, content)

        text, bytes_analyzed, was_truncated = reader(filepath, 0)
        assert text == content
        assert bytes_analyzed == len(content.encode("utf-8"))
        assert was_truncated is False

        text_limited, bytes_limited, was_truncated_limited = reader(filepath, limit)
        assert len(text_limited.encode("utf-8")) <= limit
        assert bytes_limited <= limit
        assert was_truncated_limited is True

    def test_read_file_mmap(self, tmp_path):
        """Test memory-mapped file reading."""
        self.assert_reader_handles_full_and_limited_read(
            tmp_path, read_file_mmap, "mmap_test.c", "Hello\nWorld\nTest", 5
        )

    def test_read_file_traditional(self, tmp_path):
        """Test traditional file reading."""
        self.assert_reader_handles_full_and_limited_read(
            tmp_path, read_file_traditional, "traditional_test.c", "Traditional\nFile\nReading", 8
        )

    def test_read_functions_consistency(self, tmp_path):
        """Test that mmap and traditional reading produce identical results."""
        content = "Consistency\nTest\nFile\nWith\nMultiple\nLines"
        filepath = self.create_test_file(tmp_path, "consistency_test.c", content)

        # Read with both methods
        mmap_text, mmap_bytes, mmap_truncated = read_file_mmap(filepath, 0)
        trad_text, trad_bytes, trad_truncated = read_file_traditional(filepath, 0)

        # Results should be identical
        assert mmap_text == trad_text
        assert mmap_bytes == trad_bytes
        assert mmap_truncated == trad_truncated

    def test_truncation_uses_byte_semantics_on_multibyte(self, tmp_path):
        # A6: read_file_traditional must truncate by BYTES (like the mmap path),
        # not by characters. With multibyte content, f.read(max_size) chars would
        # over-read and report bytes_analyzed > max_size, diverging from mmap.
        content = "é" * 10  # 20 bytes UTF-8, 10 chars
        filepath = self.create_test_file(tmp_path, "multibyte.c", content)
        limit = 5  # splits mid-character

        mmap_text, mmap_bytes, mmap_trunc = read_file_mmap(filepath, limit)
        trad_text, trad_bytes, trad_trunc = read_file_traditional(filepath, limit)

        assert trad_bytes == mmap_bytes == limit
        assert trad_text == mmap_text
        assert trad_trunc == mmap_trunc is True

    def test_empty_file_handling(self, tmp_path):
        """Test handling of empty files."""
        filepath = self.create_test_file(tmp_path, "empty_test.c", "")

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
        text = sz.Str("int x; // comment\nint y;")
        offsets = [0, 18]
        # Position inside the comment
        assert is_position_commented_simd_optimized(text, 10, offsets) is True

    def test_is_position_commented_not_commented(self):
        text = sz.Str("int x = 5;\nint y;")
        offsets = [0, 11]
        assert is_position_commented_simd_optimized(text, 4, offsets) is False

    def test_is_position_commented_empty_offsets(self):
        text = sz.Str("// all comment")
        offsets = [0]
        assert is_position_commented_simd_optimized(text, 5, offsets) is True

    def test_is_inside_block_comment_no_comments(self):
        text = sz.Str("int x = 5;")
        assert is_inside_block_comment_simd(text, 4) is False

    def test_is_inside_block_comment_inside(self):
        text = sz.Str("/* comment */ int x;")
        assert is_inside_block_comment_simd(text, 5) is True

    def test_is_inside_block_comment_outside(self):
        text = sz.Str("/* comment */ int x;")
        assert is_inside_block_comment_simd(text, 15) is False

    def test_block_comment_marker_inside_line_comment_is_not_block(self):
        # N10: a /* appearing inside a // line comment must NOT be treated as
        # opening a real block comment, otherwise every directive after it on
        # later lines is silently dropped.
        text = sz.Str("// has a /* marker\nint x;")
        pos = int(text.find("int x"))
        assert is_inside_block_comment_simd(text, pos) is False

    def test_block_comment_marker_inside_string_is_not_block(self):
        # A20: a /* appearing inside a string literal must NOT be treated as
        # opening a real block comment.
        text = sz.Str('const char* s = "/*";\nint y;')
        pos = int(text.find("int y"))
        assert is_inside_block_comment_simd(text, pos) is False

    def test_real_block_comment_still_detected_after_string_and_line_comment(self):
        # Guard: the forward scan must still find a genuine unterminated block
        # comment even when a string literal and a line comment precede it.
        text = sz.Str('s = "x"; // c\nint a; /* open\nint b;')
        pos = int(text.find("int b"))
        assert is_inside_block_comment_simd(text, pos) is True


class TestDirectiveAndIncludeFinders:
    """Finder/extractor behavior: directive- and include-start validation.

    Covers the consolidation of N9 (include finder lacked the whitespace
    line-prefix gate), A3 (directive finder lacked the block-comment gate), and
    A18 (`# include` with whitespace between `#` and the keyword).
    """

    def _includes_from_text(self, src):
        text = sz.Str(src)
        offsets = _compute_line_byte_offsets(text)
        positions = find_include_positions_simd_bulk(text, offsets)
        lines = text.splitlines()
        return _extract_includes(positions, lines, offsets, text)

    def _directives_from_text(self, src):
        text = sz.Str(src)
        offsets = _compute_line_byte_offsets(text)
        return find_directive_positions_simd_bulk(text, offsets)

    def test_real_include_is_detected(self):
        incs = self._includes_from_text('#include "foo.h"\n')
        assert any(str(inc["filename"]) == "foo.h" for inc in incs)

    def test_include_in_string_literal_is_rejected(self):
        # N9: a #include appearing inside a string literal must NOT be recorded
        # as a real dependency (ghost header).
        incs = self._includes_from_text('const char* s = "#include <ghost.h>";\nint x;\n')
        assert not any("ghost.h" in str(inc["filename"]) for inc in incs)

    def test_directive_inside_block_comment_is_rejected(self):
        # A3: directives inside a /* */ block comment must NOT be recorded.
        dp = self._directives_from_text("/*\n#define FOO 1\n#include <ghost.h>\n*/\nint x;\n")
        assert dp.get("define", []) == []
        assert dp.get("include", []) == []

    def test_include_inside_block_comment_is_rejected(self):
        # A3 (include path): a commented-out include yields no header.
        incs = self._includes_from_text("/*\n#include <ghost.h>\n*/\nint x;\n")
        assert not any("ghost.h" in str(inc["filename"]) for inc in incs)

    def test_spaced_hash_include_is_detected(self):
        # A18: `# include` (whitespace between # and keyword) is a valid directive.
        incs = self._includes_from_text('#  include "foo.h"\n')
        assert any(str(inc["filename"]) == "foo.h" for inc in incs)

    def test_real_directive_still_detected_after_block_comment(self):
        # Guard: a genuine directive after a closed block comment is still found.
        dp = self._directives_from_text("/* c */\n#define BAR 2\n")
        assert len(dp.get("define", [])) == 1

    def test_directive_after_closed_block_comment_same_line(self):
        # A21: a closed block comment is whitespace per the standard, so a
        # directive after one ON THE SAME LINE is valid.
        dp = self._directives_from_text("/* banner */ #if FOO\n#endif\n")
        assert len(dp.get("if", [])) == 1

    def test_include_after_closed_block_comment_same_line(self):
        # A21 (include path): `/* c */ #include "foo.h"` resolves its header.
        incs = self._includes_from_text('/* c */ #include "foo.h"\n')
        assert any(str(inc["filename"]) == "foo.h" for inc in incs)

    def test_directive_after_open_block_comment_same_line_rejected(self):
        # Guard against over-eager A21: an UNclosed block comment swallows the
        # rest of the line, so the marker is still inside the comment.
        dp = self._directives_from_text("/* open #if FOO\nstill comment\n")
        assert dp.get("if", []) == []

    def test_include_next_system_header_resolves(self):
        # Claim 3 regression: `#include_next <foo.h>` names a real dependency
        # (header-wrapper idiom). The legacy substring scan caught it via the
        # `#include` prefix; the exact-match directive finder must not drop it.
        incs = self._includes_from_text("#include_next <foo.h>\n")
        assert any(str(inc["filename"]) == "foo.h" and inc["is_system"] for inc in incs)

    def test_include_next_quoted_header_resolves(self):
        # Claim 3 (quoted form): `#include_next "bar.h"` is likewise a dependency.
        incs = self._includes_from_text('#include_next "bar.h"\n')
        assert any(str(inc["filename"]) == "bar.h" and not inc["is_system"] for inc in incs)


class TestCommentStripAtExtraction:
    """Trailing comments must not leak into directive operands/values/conditions.

    Covers N5 (ifdef/ifndef/undef operand), N7 (define value via
    parse_directive_struct), A19 (define value via _extract_defines), and A2
    (conditional-macro extraction). The strip is string-literal aware so a `//`
    or `/*` inside a literal is preserved.
    """

    def _defines_from_text(self, src):
        text = sz.Str(src)
        offsets = _compute_line_byte_offsets(text)
        lines = text.splitlines()
        dp = find_directive_positions_simd_bulk(text, offsets)
        return _extract_defines(dp.get("define", []), lines, offsets, None)

    def test_ifdef_operand_line_comment_stripped(self):
        # N5
        result = _parse_directive("ifdef", "#ifdef FOO // trailing")
        assert str(result.macro_name) == "FOO"

    def test_ifdef_operand_block_comment_stripped(self):
        result = _parse_directive("ifdef", "#ifdef FOO /* trailing */")
        assert str(result.macro_name) == "FOO"

    def test_define_value_line_comment_stripped(self):
        # N7
        result = _parse_directive("define", "#define V 100 // note")
        assert str(result.macro_name) == "V"
        assert str(result.macro_value) == "100"

    def test_define_value_in_string_preserves_slashes(self):
        # Guard: // inside a string literal is NOT a comment.
        result = _parse_directive("define", '#define URL "http://example.com"')
        assert str(result.macro_value) == '"http://example.com"'

    def test_extract_defines_value_comment_stripped(self):
        # A19
        defines = self._defines_from_text("#define V 100 // note\n")
        v = next(d for d in defines if str(d["name"]) == "V")
        assert str(v["value"]) == "100"

    def test_extract_defines_flag_with_comment_has_no_value(self):
        # A19 corollary: `#define FLAG // c` is a valueless flag, not value "// c".
        defines = self._defines_from_text("#define FLAG // enable\n")
        flag = next(d for d in defines if str(d["name"]) == "FLAG")
        assert flag["value"] is None

    def test_conditional_macros_exclude_comment_words(self):
        # A2
        directive = _parse_directive("if", "#if defined(FOO) // note")
        macros = _extract_conditional_macros([directive])
        names = {str(m) for m in macros}
        assert "FOO" in names
        assert "note" not in names


class TestExtractionOrdering:
    """Directive extraction must proceed in strict source order (N12).

    The directive finder records the ``#include`` on a ``#define`` continuation
    line under the "include" type. Iterating by type (include before define)
    processes that continuation position as a phantom standalone include before
    the owning ``#define`` consumes it. Sorting by byte position fixes it.
    """

    # The phantom trigger: an #include on a #define continuation line.
    _PHANTOM_SRC = '#include "first.h"\n#define M \\\n#include "cont.h"\n'

    def _extract(self, src):
        text = sz.Str(src)
        offsets = _compute_line_byte_offsets(text)
        lines = text.splitlines()
        dp = find_directive_positions_simd_bulk(text, offsets)
        directives, _ = _extract_directives(dp, lines, offsets)
        return directives, lines, offsets, text

    def test_no_phantom_include_directive_from_continuation(self):
        directives, _, _, _ = self._extract(self._PHANTOM_SRC)
        include_lines = sorted(d.line_num for d in directives if d.directive_type == "include")
        assert include_lines == [0]  # only the real include on line 0
        defines = [d for d in directives if d.directive_type == "define"]
        assert len(defines) == 1
        assert defines[0].continuation_lines == 1  # spans lines 1-2

    def test_no_phantom_header_reaches_includes(self):
        # Production derives include records from the cleaned directives list,
        # so the continuation header must not appear as a real dependency.
        directives, lines, offsets, text = self._extract(self._PHANTOM_SRC)
        include_positions = _include_positions_from_directives(directives)
        incs = _extract_includes(include_positions, lines, offsets, text)
        names = {str(inc["filename"]) for inc in incs}
        assert "first.h" in names
        assert "cont.h" not in names

    def test_include_next_reaches_includes_via_production_derivation(self):
        # Claim 3 (production path): analyze_file derives include records from
        # _include_positions_from_directives, NOT the standalone include finder.
        # #include_next names a real dependency (header-wrapper idiom) and must
        # survive that derivation too.
        directives, lines, offsets, text = self._extract("#include_next <foo.h>\n")
        include_positions = _include_positions_from_directives(directives)
        incs = _extract_includes(include_positions, lines, offsets, text)
        assert any(str(inc["filename"]) == "foo.h" and inc["is_system"] for inc in incs)


class TestIncludeContinuationSplicing:
    """Backslash-continued #include directives must resolve their header (N13).

    C++ phase-2 line splicing joins a line ending in `\\` with the next
    physical line before tokenization, so the header token may legitimately
    sit on the continuation line.
    """

    def _includes_from_text(self, src):
        text = sz.Str(src)
        offsets = _compute_line_byte_offsets(text)
        positions = find_include_positions_simd_bulk(text, offsets)
        lines = text.splitlines()
        return _extract_includes(positions, lines, offsets, text)

    def test_continued_quoted_include_resolves_header(self):
        incs = self._includes_from_text('#include \\\n"foo.h"\nint x;\n')
        assert any(str(inc["filename"]) == "foo.h" for inc in incs)

    def test_continued_system_include_resolves_header(self):
        incs = self._includes_from_text("#include \\\n<vector>\nint x;\n")
        assert any(str(inc["filename"]) == "vector" for inc in incs)

    def test_uncontinued_include_still_resolves(self):
        # Guard: the common single-line case is unaffected.
        incs = self._includes_from_text('#include "foo.h"\n')
        assert any(str(inc["filename"]) == "foo.h" for inc in incs)


class TestBufferDetachment:
    """A7: a cached FileAnalysisResult must not pin the whole file buffer.

    Every retained stringzilla.Str (filenames, macro names, directive operands,
    define lines, magic flags, headers, include guard, conditional macros) is a
    *view* into the parent decoded-text buffer. Caching the result would keep the
    entire file alive for the build lifetime; the retained tokens are tiny by
    comparison. _detach_file_analysis_result rebuilds each Str so it owns its
    bytes, releasing the parent. Observed via stringzilla's ``.address``: a view
    lies inside ``[parent.address, parent.address + parent.nbytes)``; a detached
    copy does not.
    """

    _SRC = (
        "y" * 5000 + "\n"
        '#include "header.h"\n'
        "#include_next <sys.h>\n"
        "#define MACRO(a, b) some_value  // c\n"
        "#ifdef FEATURE\n"
        "//#CXXFLAGS=-O2\n"
        "#endif\n"
        "#ifndef GUARD_H\n"
        "#define GUARD_H\n" + "z" * 5000 + "\n"
    )

    def _build_result(self, text):
        # Mirror analyze_file's result construction without a BuildContext.
        lines = text.splitlines()
        offsets = _compute_line_byte_offsets(text)
        magic_positions = find_magic_positions_simd_bulk(text, offsets)
        directive_positions = find_directive_positions_simd_bulk(text, offsets)
        directives, directive_by_line = _extract_directives(directive_positions, lines, offsets)
        include_positions = _include_positions_from_directives(directives)
        includes = _extract_includes(include_positions, lines, offsets, text)
        magic_flags = _extract_magic_flags(magic_positions, lines, offsets)
        include_guard = detect_include_guard(directives)
        define_positions = [d.byte_pos for d in directives if d.directive_type == "define"]
        defines = _extract_defines(define_positions, lines, offsets, include_guard)
        system_headers = {inc["filename"] for inc in includes if inc["is_system"]}
        quoted_headers = {inc["filename"] for inc in includes if not inc["is_system"]}
        conditional_macros = _extract_conditional_macros(directives)
        return FileAnalysisResult(
            line_count=len(lines),
            line_byte_offsets=offsets,
            include_positions=include_positions,
            magic_positions=magic_positions,
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=len(text),
            was_truncated=False,
            includes=includes,
            magic_flags=magic_flags,
            defines=defines,
            system_headers=system_headers,
            quoted_headers=quoted_headers,
            include_guard=include_guard,
            conditional_macros=conditional_macros,
        )

    def _iter_strs(self, result):
        """Yield every stringzilla.Str reachable from the result's fields."""

        def walk(obj):
            if isinstance(obj, sz.Str):
                yield obj
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from walk(v)
            elif isinstance(obj, (list, tuple, set, frozenset)):
                for v in obj:
                    yield from walk(v)

        yield from walk(result.includes)
        yield from walk(result.magic_flags)
        yield from walk(result.defines)
        yield from walk(result.system_headers)
        yield from walk(result.quoted_headers)
        yield from walk(result.conditional_macros)
        if result.include_guard is not None:
            yield from walk(result.include_guard)
        for d in result.directives:
            for v in (d.condition, d.macro_name, d.macro_value):
                if v is not None:
                    yield from walk(v)

    def test_strs_are_views_before_detach(self):
        # Sanity: the un-detached result really does pin the parent buffer, so
        # the detach assertion below is meaningful.
        text = sz.Str(self._SRC)
        lo, hi = text.address, text.address + text.nbytes
        result = self._build_result(text)
        assert any(lo <= s.address < hi for s in self._iter_strs(result))

    def test_detach_frees_parent_buffer(self):
        from compiletools.file_analyzer import _detach_file_analysis_result

        text = sz.Str(self._SRC)
        lo, hi = text.address, text.address + text.nbytes
        result = self._build_result(text)
        _detach_file_analysis_result(result)
        offenders = [str(s) for s in self._iter_strs(result) if lo <= s.address < hi]
        assert offenders == [], f"fields still pinning parent buffer: {offenders}"

    def test_detach_preserves_values(self):
        from compiletools.file_analyzer import _detach_file_analysis_result

        text = sz.Str(self._SRC)
        result = self._build_result(text)
        before_inc = sorted(str(i["filename"]) for i in result.includes)
        before_guard = str(result.include_guard)
        before_cond = sorted(str(m) for m in result.conditional_macros)
        before_defs = sorted(str(d["name"]) for d in result.defines)
        before_magic = sorted(str(m["key"]) for m in result.magic_flags)
        _detach_file_analysis_result(result)
        assert sorted(str(i["filename"]) for i in result.includes) == before_inc
        assert str(result.include_guard) == before_guard
        assert sorted(str(m) for m in result.conditional_macros) == before_cond
        assert sorted(str(d["name"]) for d in result.defines) == before_defs
        assert sorted(str(m["key"]) for m in result.magic_flags) == before_magic
        # System-header membership must survive (set keyed by Str content).
        assert any(str(h) == "sys.h" for h in result.system_headers)


class TestParseDirectiveStruct:
    """Test parse_directive_struct()."""

    def test_parse_define_no_value(self):
        result = _parse_directive("define", "#define FOO")
        assert result.directive_type == "define"
        assert str(result.macro_name) == "FOO"
        assert result.macro_value is None

    def test_parse_define_with_value(self):
        result = _parse_directive("define", "#define FOO 42")
        assert str(result.macro_name) == "FOO"
        assert str(result.macro_value) == "42"

    def test_parse_ifdef(self):
        result = _parse_directive("ifdef", "#ifdef MY_MACRO")
        assert result.directive_type == "ifdef"
        assert str(result.macro_name) == "MY_MACRO"

    def test_parse_directive_empty_content(self):
        result = _parse_directive("endif", "#endif")
        assert result.directive_type == "endif"

    def test_parse_include_directive(self):
        result = _parse_directive("include", '#include "foo.h"')
        assert result.directive_type == "include"
        assert '"foo.h"' in str(result.condition)

    def test_function_like_macro_with_space_in_params(self):
        # N2: a space inside the parameter list must NOT split the value. The
        # value is everything after the closing ')', not after the first space.
        result = _parse_directive("define", "#define F(a, b) ((a) + (b))")
        assert str(result.macro_name) == "F"
        assert str(result.macro_value) == "((a) + (b))"

    def test_object_macro_with_parenthesized_value(self):
        # N2 corollary: `#define F (x)` (space before paren) is an OBJECT macro
        # named F whose value is `(x)`, not a function-like macro.
        result = _parse_directive("define", "#define F (x)")
        assert str(result.macro_name) == "F"
        assert str(result.macro_value) == "(x)"

    def test_function_like_macro_no_body(self):
        result = _parse_directive("define", "#define F(a, b)")
        assert str(result.macro_name) == "F"
        assert result.macro_value is None


class TestCacheStatsFunctions:
    """Test cache stats and clear functions."""

    def test_get_cache_stats(self):
        ctx = BuildContext()
        stats = get_cache_stats(ctx)
        assert "cache_size" in stats

    def test_print_cache_stats(self, capsys):
        ctx = BuildContext()
        print_cache_stats(ctx)
        captured = capsys.readouterr()
        assert "Cache" in captured.out

    def test_cache_clear(self):
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
            directives=[
                PreprocessorDirective(line_num=0, byte_pos=0, directive_type="define", continuation_lines=0),
                PreprocessorDirective(line_num=2, byte_pos=20, directive_type="define", continuation_lines=0),
                PreprocessorDirective(line_num=1, byte_pos=10, directive_type="include", continuation_lines=0),
            ],
            directive_by_line={},
            bytes_analyzed=50,
            was_truncated=False,
        )
        line_nums = result.get_directive_line_numbers()
        assert line_nums["define"] == {0, 2}
        assert line_nums["include"] == {1}

    def test_phantom_continuation_directive_excluded(self):
        # directive_positions carries a phantom continuation include at pos 31
        # (line 2), but the cleaned `directives` list correctly attributes line 2
        # to the #define continuation. Line numbers must come from `directives`,
        # so the phantom include line must NOT be reported. (N3)
        result = FileAnalysisResult(
            line_count=3,
            line_byte_offsets=[0, 19, 31],
            include_positions=[0],
            magic_positions=[],
            directive_positions={"include": [0, 31], "define": [19]},
            directives=[
                PreprocessorDirective(line_num=0, byte_pos=0, directive_type="include", continuation_lines=0),
                PreprocessorDirective(line_num=1, byte_pos=19, directive_type="define", continuation_lines=1),
            ],
            directive_by_line={},
            bytes_analyzed=40,
            was_truncated=False,
        )
        line_nums = result.get_directive_line_numbers()
        assert line_nums["include"] == {0}
        assert line_nums["define"] == {1}
        assert 2 not in line_nums.get("include", set())


class TestDetectIncludeGuard:
    """Test detect_include_guard function."""

    def test_pragma_once(self):
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
        assert detect_include_guard([]) is None

    def test_no_guard_too_few_directives(self):
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

    def test_no_guard_feature_flag_then_unrelated_conditional(self):
        # N1: a feature-flag #ifndef/#define/#endif at the top of the file is
        # NOT an include guard, even though the file happens to END with an
        # (unrelated) #endif. The endif that closes the first #ifndef is the
        # early one (index 2), not the last directive, so depth returns to 0
        # before EOF -> not a whole-file guard.
        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("FOO_FLAG")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="define", continuation_lines=0, macro_name=sz.Str("FOO_FLAG")
            ),
            PreprocessorDirective(line_num=2, byte_pos=20, directive_type="endif", continuation_lines=0),
            PreprocessorDirective(
                line_num=3, byte_pos=30, directive_type="if", continuation_lines=0, condition=sz.Str("BAR")
            ),
            PreprocessorDirective(
                line_num=4, byte_pos=40, directive_type="define", continuation_lines=0, macro_name=sz.Str("BAZ")
            ),
            PreprocessorDirective(line_num=5, byte_pos=50, directive_type="endif", continuation_lines=0),
        ]
        assert detect_include_guard(directives) is None

    def test_guard_with_nested_conditional_inside(self):
        # Guard: a real include guard wrapping inner conditionals is still
        # detected -- the first #ifndef's matching #endif IS the last directive.
        directives = [
            PreprocessorDirective(
                line_num=0, byte_pos=0, directive_type="ifndef", continuation_lines=0, macro_name=sz.Str("HDR_H")
            ),
            PreprocessorDirective(
                line_num=1, byte_pos=10, directive_type="define", continuation_lines=0, macro_name=sz.Str("HDR_H")
            ),
            PreprocessorDirective(
                line_num=2, byte_pos=20, directive_type="if", continuation_lines=0, condition=sz.Str("INNER")
            ),
            PreprocessorDirective(line_num=3, byte_pos=30, directive_type="endif", continuation_lines=0),
            PreprocessorDirective(line_num=4, byte_pos=40, directive_type="endif", continuation_lines=0),
        ]
        guard = detect_include_guard(directives)
        assert guard is not None
        assert str(guard) == "HDR_H"


class TestAddArguments:
    """Test the module-level add_arguments function."""

    def test_add_arguments(self):
        """Test that add_arguments adds expected flags."""
        cap = configargparse.ArgParser()
        add_arguments(cap)
        # Parse with defaults
        args = cap.parse_args([])
        assert args.use_mmap is True
        assert args.force_mmap is False
        assert args.suppress_fd_warnings is False
        assert args.suppress_filesystem_warnings is False


class TestDetermineFileReadingStrategy:
    """Test _determine_file_reading_strategy."""

    def test_no_mmap_flag(self):
        ctx = BuildContext()
        args = SimpleNamespace(use_mmap=False)
        set_analyzer_args(args, ctx)
        assert ctx.file_reading_strategy == "no_mmap"

    def test_force_mmap_flag(self):
        ctx = BuildContext()
        args = SimpleNamespace(use_mmap=True, force_mmap=True)
        set_analyzer_args(args, ctx)
        assert ctx.file_reading_strategy == "mmap"


class TestExtractConditionalMacros:
    """Test _extract_conditional_macros function."""

    def test_ifdef_macros(self):
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
        result = _parse_directive("ifndef", "#ifndef GUARD_H")
        assert result.directive_type == "ifndef"
        assert str(result.macro_name) == "GUARD_H"

    def test_parse_undef(self):
        result = _parse_directive("undef", "#undef OLD_MACRO")
        assert result.directive_type == "undef"
        assert str(result.macro_name) == "OLD_MACRO"

    def test_parse_if_condition(self):
        result = _parse_directive("if", "#if defined(X) && Y > 1")
        assert result.directive_type == "if"
        assert "defined(X)" in str(result.condition)

    def test_parse_elif_condition(self):
        result = _parse_directive("elif", "#elif Z == 0")
        assert result.directive_type == "elif"
        assert "Z == 0" in str(result.condition)

    def test_parse_pragma(self):
        result = _parse_directive("pragma", "#pragma once")
        assert result.directive_type == "pragma"
        assert str(result.macro_name) == "once"

    def test_parse_define_function_like(self):
        result = _parse_directive("define", "#define MAX(a,b) ((a)>(b)?(a):(b))")
        assert result.directive_type == "define"
        assert str(result.macro_name) == "MAX"

    def test_parse_multiline_directive(self):
        result = _parse_directive("define", "#define LONG \\", "  value")
        assert result.directive_type == "define"
        assert str(result.macro_name) == "LONG"
