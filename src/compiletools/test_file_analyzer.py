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
    _detect_marker_type,
    _extract_conditional_macros,
    _extract_defines,
    _extract_directives,
    _extract_includes,
    _extract_magic_flags,
    _include_positions_from_directives,
    add_arguments,
    analyze_file,
    detect_include_guard,
    find_comment_and_literal_spans,
    find_directive_positions_simd_bulk,
    find_magic_positions_simd_bulk,
    is_inside_block_comment_simd,
    is_position_commented_simd_optimized,
    parse_directive_struct,
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
        lines = text.splitlines()
        # Mirror the production path: directive finder -> _extract_directives
        # (absorbs N13 continuations) -> _include_positions_from_directives.
        directive_positions = find_directive_positions_simd_bulk(text, offsets)
        directives, _ = _extract_directives(directive_positions, lines, offsets)
        positions = _include_positions_from_directives(directives)
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
        lines = text.splitlines()
        # Mirror the production path: directive finder -> _extract_directives
        # (absorbs N13 continuations) -> _include_positions_from_directives.
        directive_positions = find_directive_positions_simd_bulk(text, offsets)
        directives, _ = _extract_directives(directive_positions, lines, offsets)
        positions = _include_positions_from_directives(directives)
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


class TestDetectMarkerTypeCommentAware:
    """Marker detection must ignore hits inside comments and string literals.

    doctest's DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN emits the boilerplate comment
    "Entry point: main() is generated by ..." into user files; a naive
    substring scan classifies those tests as executables (EXE outranks TEST).
    String data (help text, log messages) is filtered too, except literals on
    preprocessor lines so #include "unit_test.hpp" keeps classifying.
    """

    def _detect(self, src, exe=("main(",), test=(), lib=()):
        text = sz.Str(src)
        comments, literals = find_comment_and_literal_spans(text)
        return _detect_marker_type(text, list(exe), list(test), list(lib), comments, literals)

    def test_marker_in_line_comment_ignored(self):
        src = "// Entry point: main() is generated by DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_marker_in_block_comment_ignored(self):
        src = "/* main() lives elsewhere */\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_real_marker_still_detected(self):
        src = "#include <iostream>\nint main(int argc, char** argv) { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_commented_marker_then_real_marker_detected(self):
        # First occurrence is commented; scan must advance to the real one.
        src = "// main() described here\nint main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_doctest_boilerplate_classifies_as_test(self):
        # The user-reported case: commented main() plus a genuine test marker.
        src = (
            "// Entry point: main() is generated by DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN\n"
            '#include "doctest/doctest.h"\n'
            'TEST_CASE("x") {}\n'
        )
        assert self._detect(src, test=("doctest.h",)) == MarkerType.TEST

    def test_test_marker_in_comment_ignored(self):
        src = "// see doctest.h for details\nint helper();\n"
        assert self._detect(src, test=("doctest.h",)) == MarkerType.NONE

    def test_library_marker_in_comment_ignored(self):
        src = "// LIBRARY_MARKER documented here\nint helper();\n"
        assert self._detect(src, lib=("LIBRARY_MARKER",)) == MarkerType.NONE

    def test_no_markers_configured(self):
        assert self._detect("int main() {}\n", exe=()) == MarkerType.NONE

    def test_exe_priority_over_test_unchanged(self):
        # Both real: EXE must still win (priority is intentional).
        src = '#include "doctest/doctest.h"\nint main() { return 0; }\n'
        assert self._detect(src, test=("doctest.h",)) == MarkerType.EXE

    def test_url_in_string_before_real_marker(self):
        # A '//' inside a string literal is not a comment; the real main()
        # later on the same line must still classify.
        src = 'const char* kUrl = "https://example.com"; int main() { return 0; }\n'
        assert self._detect(src) == MarkerType.EXE

    def test_url_string_does_not_invert_priority(self):
        # Worse form of the above: masking the exe marker demoted a real
        # executable to TEST when a test marker existed elsewhere.
        src = '#include "unit_test.hpp"\nconst char* kUrl = "https://x.com"; int main() { return 0; }\n'
        assert self._detect(src, test=("unit_test.hpp",)) == MarkerType.EXE

    def test_line_comment_chars_in_string_not_a_comment(self):
        src = 'void f() { puts("// note"); }\nint main() { return 0; }\n'
        assert self._detect(src) == MarkerType.EXE

    def test_marker_after_closed_block_comment_same_line(self):
        src = "/* doc */ int main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_slashes_inside_closed_block_comment_before_marker(self):
        # A '//' inside a CLOSED block comment must not swallow the rest of
        # the line after '*/'.
        src = "/* see // docs */ int main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_url_inside_block_comment_before_marker(self):
        src = "/* docs: http://example.com/spec */ int main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_raw_string_does_not_blind_marker(self):
        # A '"' then '/*' inside a raw string must not open a phantom block
        # comment that hides the real main() on the next line.
        src = 'const char* s = R"(text " here /* )";\nint main() { return 0; }\n// */\n'
        assert self._detect(src) == MarkerType.EXE

    def test_marker_with_comment_leader_still_detected(self):
        # Compat: a marker configured WITH its comment leader (magic-flag
        # style, like //#CXXFLAGS) hits at the leader, before the comment
        # content starts, so it must still classify.
        src = "// CT-LIBRARY\nint helper();\n"
        assert self._detect(src, exe=(), lib=("// CT-LIBRARY",)) == MarkerType.LIBRARY

    def test_marker_in_string_literal_does_not_classify(self):
        # String data is not code: "main(" inside an ordinary literal (help
        # text, log messages) must not classify the file as an executable.
        src = 'const char* s = "call main() now";\n'
        assert self._detect(src) == MarkerType.NONE

    def test_marker_in_raw_string_does_not_classify(self):
        src = 'const char* s = R"(usage: main(argc, argv))";\nint helper();\n'
        assert self._detect(src) == MarkerType.NONE

    def test_include_string_still_classifies_test_marker(self):
        # The pp-line exemption: test markers normally live inside the quoted
        # filename of an #include directive, which the lexer sees as a string
        # literal. Literals on preprocessor lines keep classifying.
        src = '#include "unit_test.hpp"\nvoid test_widget();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.TEST

    def test_indented_include_still_classifies_test_marker(self):
        src = '  #  include "unit_test.hpp"\nvoid test_widget();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.TEST

    def test_define_string_still_classifies(self):
        # Deliberate breadth of the pp-line exemption: ANY literal on a
        # preprocessor line classifies, including #define bodies. Narrowing
        # to #include only is a separate decision.
        src = '#define USAGE "run main( first)"\nint helper();\n'
        assert self._detect(src) == MarkerType.EXE

    def test_real_main_after_string_with_marker(self):
        src = 'const char* s = "main( in a string";\nint main() { return 0; }\n'
        assert self._detect(src) == MarkerType.EXE

    def test_commented_include_does_not_classify_test_marker(self):
        # A commented-out include is neither code nor an active directive.
        src = '// #include "unit_test.hpp"\nvoid helper();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.NONE

    def test_digit_separator_does_not_mask_comment(self):
        # C++14 digit separator: the apostrophe in 5'000 must not open a
        # phantom char literal that hides the doctest boilerplate comment.
        src = (
            "constexpr int kTimeoutMs = 5'000;\n"
            "// Entry point: main() is generated by DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN\n"
            '#include "doctest/doctest.h"\n'
            'TEST_CASE("t") {}\n'
        )
        assert self._detect(src, test=("doctest.h",)) == MarkerType.TEST

    def test_digit_separator_odd_count_does_not_mask_marker(self):
        # Odd apostrophe parity + contraction in a comment used to open a
        # phantom span over real code.
        src = (
            "constexpr uint64_t kBudget = 1'000'000'000;  // ns\n"
            "constexpr int kRetries = 3;   // don't raise without checking /* legacy note\n"
            "int main(int argc, char** argv) { return 0; }\n"
        )
        assert self._detect(src) == MarkerType.EXE

    def test_char_literal_with_encoding_prefix_still_literal(self):
        # u8'a' is a char literal — the digit '8' before the quote must not
        # trigger the digit-separator heuristic ('a' is a hex char after it),
        # or the scan desyncs and the real comment below gets missed.
        src = "auto c = u8'a'; auto d = 'b';\n// main( commented\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_hex_digit_separator(self):
        src = "constexpr auto kMask = 0xDEAD'BEEF;  // main( commented\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_illformed_raw_string_does_not_blind_scanner(self):
        # R"x" has no '(' within the 16-char delimiter window; it must be
        # treated as an ordinary string, not swallow the rest of the file.
        src = 'auto s = R"x"; foo(bar); // main( commented\nint helper();\n'
        assert self._detect(src) == MarkerType.NONE

    def test_raw_string_with_nonempty_delimiter(self):
        src = 'const char* s = R"xx(text " here /* )xx";\nint main() { return 0; }\n'
        assert self._detect(src) == MarkerType.EXE

    def test_cr_only_line_endings(self):
        # Classic-Mac CR-only newlines: the // span must end at the \r, not
        # swallow the rest of the file.
        src = "// note\rint main() { return 0; }\r"
        assert self._detect(src) == MarkerType.EXE

    def test_backslash_continued_line_comment_masks_spliced_line(self):
        # Phase-2 line splicing joins the next physical line into the //
        # comment, so the main() there is never compiled.
        src = "// comment continues \\\nint main() { return 0; }\n"
        assert self._detect(src) == MarkerType.NONE

    def test_marker_in_multiline_block_comment_ignored(self):
        src = "/*\n main() \n*/\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_marker_after_unterminated_block_comment_ignored(self):
        src = "/* unterminated\nint main() { return 0; }\n"
        assert self._detect(src) == MarkerType.NONE

    def test_comment_leader_marker_with_doxygen_leader(self):
        # A comment-shaped marker ("// CT-LIBRARY") must match even when the
        # actual comment uses a Doxygen leader (///), where the marker hit is
        # inside the span rather than at its start.
        src = "/// CT-LIBRARY\nint helper();\n"
        assert self._detect(src, exe=(), lib=("// CT-LIBRARY",)) == MarkerType.LIBRARY

    def test_char_literals_with_comment_chars(self):
        src = "char a = '/'; char b = '\"';\nint main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_multibyte_char_literal_does_not_crash(self):
        # L'€' puts a UTF-8 multibyte sequence right after the quote; slicing
        # a single byte of it must not raise UnicodeDecodeError.
        src = "wchar_t c = L'€'; char32_t d = U'é';\nint main() { return 0; }\n"
        assert self._detect(src) == MarkerType.EXE

    def test_multibyte_near_digit_separator_window(self):
        # Multibyte chars inside the 40-byte back-scan window (and straddling
        # its start) must not crash or break separator detection.
        src = "// tempo: allegro™ ›→ set αβγδεζηθικλμ\nint x = 5'000; // main( commented\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_multibyte_before_raw_string_window(self):
        src = 'auto s = R"(日本語テキスト main( ここ)";\nint helper();\n'
        # Unterminated-ish raw string with multibyte content must not crash;
        # main( lives inside the raw string, which stays classifying (pinned
        # string-literal behavior) — the point is only: no exception.
        self._detect(src)

    def test_digit_separator_with_udl_suffix_multibyte(self):
        # The pp-number fast-skip after 1'000 lands on the µ of a Unicode UDL
        # suffix; must not crash and the comment below must still mask.
        src = "auto t = 1'000µs;\n// main( commented\nint helper();\n"
        assert self._detect(src) == MarkerType.NONE

    def test_binary_garbage_does_not_crash(self):
        # errors='replace' decoding of binary files yields multibyte
        # replacement chars in arbitrary positions; scanning must not raise.
        garbage = bytes(range(256)).decode("utf-8", errors="replace") * 4
        self._detect(garbage)

    def test_leading_dot_digit_separator_does_not_mask_comment(self):
        # .000'001 is a valid C++14 pp-number ([lex.ppnumber] allows a leading
        # dot); its apostrophe is a separator, not a char-literal opener.
        src = (
            "static const double kEps = .000'001;\n"
            "// Entry point: main() is defined in the generated harness\n"
            "void helper();\n"
        )
        assert self._detect(src) == MarkerType.NONE

    def test_leading_dot_digit_separator_does_not_blind_marker(self):
        # Same misfire in the other direction: the phantom literal chain must
        # not manufacture a fake comment span over a real main().
        src = (
            "static const double kEps = .000'001;\n"
            'const char* msg = "don\'t";\n'
            'const char* glob = "src/*.cpp";\n'
            "int main() { return 0; }\n"
        )
        assert self._detect(src) == MarkerType.EXE

    def test_cr_only_backslash_splice_then_blank_line(self):
        # CR-only file: '// header\' splices with the following EMPTY line,
        # whose own CR ends the comment — int main() is live code. The
        # CRLF-tail splice clause must not fire on backslash-CR-CR.
        src = "// header\\\r\rint main() { return 0; }\r"
        assert self._detect(src) == MarkerType.EXE

    def test_apostrophe_in_error_prose_does_not_mask_main(self):
        # The apostrophe in #error prose is prose, not a char-literal opener.
        # An unterminated literal must end at the line end ([lex.string]:
        # ordinary literals cannot span a bare newline), not swallow the
        # real main() below.
        src = (
            "#if !defined(_WIN32)\n"
            "#error This platform isn't supported yet\n"
            "#endif\n"
            "int main(int argc, char** argv) { return 0; }\n"
        )
        assert self._detect(src) == MarkerType.EXE

    def test_unterminated_string_does_not_mask_main(self):
        src = 'const char* s = "oops;\nint main() { return 0; }\n'
        assert self._detect(src) == MarkerType.EXE

    def test_pragma_contraction_does_not_mask_marker(self):
        src = "#pragma region Don't touch\nwxIMPLEMENT_APP(MyApp);\n"
        assert self._detect(src, exe=("wxIMPLEMENT_APP",)) == MarkerType.EXE

    def test_block_comment_before_include_still_classifies(self):
        # Comments are whitespace after phase 3, so this is a genuine
        # directive line and the pp-line exemption must fire.
        src = '/* lint -e537 */ #include "unit_test.hpp"\nvoid test_widget();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.TEST

    def test_multiline_comment_closing_on_include_line_still_classifies(self):
        src = '/* copyright\n blah */ #include "unit_test.hpp"\nvoid test_widget();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.TEST

    def test_hash_include_inside_raw_string_does_not_classify(self):
        # A '#'-leading line INSIDE a multi-line raw string is string data
        # (codegen templates, embedded scripts), never a directive; it must
        # not spoof the pp-line exemption.
        src = 'const char* tmpl = R"(\n#include "unit_test.hpp"\nvoid generated();\n)";\nvoid helper();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.NONE

    def test_hash_define_inside_raw_string_does_not_classify(self):
        src = 'const char* gen = R"(\n#define RUN main(argc, argv)\n)";\nvoid helper();\n'
        assert self._detect(src) == MarkerType.NONE

    def test_hash_in_block_comment_before_string_does_not_classify(self):
        # The '#' at line start sits inside a block comment, so the line is
        # not a directive; the literal marker after '*/' must stay filtered.
        src = 'const char* s = "abc" /*\n# */ "main( x";\nvoid helper();\n'
        assert self._detect(src) == MarkerType.NONE

    def test_spliced_define_string_still_classifies(self):
        # Backslash-newline splices the continuation line into the #define
        # directive, so its string body classifies exactly like the
        # single-line form pinned in test_define_string_still_classifies.
        src = '#define USAGE \\\n    "run main( now)"\nvoid h();\n'
        assert self._detect(src) == MarkerType.EXE

    def test_spliced_include_still_classifies(self):
        src = '#include \\\n  "unit_test.hpp"\nvoid test_widget();\n'
        assert self._detect(src, exe=(), test=("unit_test.hpp",)) == MarkerType.TEST

    def test_spliced_string_with_hash_line_does_not_classify(self):
        # A backslash-spliced ordinary string whose continuation line starts
        # with '#' is still string data on a non-directive logical line.
        src = 'const char* usage = "\\\n# usage: main(args)";\nvoid h();\n'
        assert self._detect(src) == MarkerType.NONE

    def test_crlf_backslash_splice(self):
        # CRLF file with a spliced // comment: both halves of the CRLF pair
        # are part of the splice; main() on the next physical line is masked.
        src = "// note \\\r\nint main() { return 0; }\r\n"
        assert self._detect(src) == MarkerType.NONE


class TestAnalyzeFileMarkerCommentAware:
    """End-to-end: analyze_file must not classify commented markers (production path)."""

    def _analyze(self, tmp_path, content):
        from compiletools.global_hash_registry import get_file_hash

        filepath = tmp_path / "candidate.cpp"
        filepath.write_text(content)
        ctx = BuildContext()
        args = SimpleNamespace(
            max_read_size=0,
            verbose=0,
            exemarkers=["main(", "main ("],
            testmarkers=["doctest.h"],
            librarymarkers=[],
            use_mmap=True,
            force_mmap=False,
            suppress_fd_warnings=True,
            suppress_filesystem_warnings=True,
        )
        set_analyzer_args(args, ctx)
        content_hash = get_file_hash(str(filepath), ctx)
        return analyze_file(content_hash, ctx)

    def test_doctest_generated_main_comment_not_exe(self, tmp_path):
        result = self._analyze(
            tmp_path,
            "// Entry point: main() is generated by DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN\n"
            '#include "doctest/doctest.h"\n'
            'TEST_CASE("widget") {}\n',
        )
        assert result.marker_type == MarkerType.TEST

    def test_real_main_still_exe(self, tmp_path):
        result = self._analyze(tmp_path, "int main(int argc, char** argv) { return 0; }\n")
        assert result.marker_type == MarkerType.EXE


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
