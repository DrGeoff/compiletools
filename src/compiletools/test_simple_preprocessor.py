import os
import re
import sys
from textwrap import dedent
from unittest.mock import patch

# Add the parent directory to sys.path so we can import ct modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective
from compiletools.preprocessing_cache import MacroState
from compiletools.simple_preprocessor import SimplePreprocessor


class TestSimplePreprocessor:
    """Unit tests for the SimplePreprocessor class"""

    def setup_method(self):
        """Set up test fixtures before each test method."""
        import stringzilla as sz

        from compiletools.build_context import BuildContext

        self.ctx = BuildContext()

        # Mock get_filepath_by_hash since tests don't have real files in registry
        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock_get_filepath = self.patcher.start()
        self.mock_get_filepath.return_value = "<test-file>"

        self.macros = {
            sz.Str("TEST_MACRO"): sz.Str("1"),
            sz.Str("FEATURE_A"): sz.Str("1"),
            sz.Str("VERSION"): sz.Str("3"),
            sz.Str("COUNT"): sz.Str("5"),
        }
        self.processor = SimplePreprocessor(self.macros, verbose=0)

    def teardown_method(self):
        """Clean up after each test method."""
        self.patcher.stop()

    def _create_file_analysis_result(self, text):
        """Helper to create FileAnalysisResult for testing"""
        lines = text.split("\n")

        # Create line_byte_offsets
        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode("utf-8")) + 1  # +1 for \n

        # Parse preprocessor directives
        directives = []
        directive_by_line = {}
        directive_positions = {}

        for line_num, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                # Parse directive
                match = re.match(r"^\s*#\s*([a-zA-Z_]+)(?:\s+(.*))?", stripped)
                if match:
                    directive_type = match.group(1)
                    rest = match.group(2) or ""

                    # Determine directive-specific fields
                    condition = None
                    macro_name = None
                    macro_value = None

                    if directive_type in ["if", "elif"]:
                        import stringzilla as sz

                        condition = sz.Str(rest.strip())
                    elif directive_type in ["ifdef", "ifndef"]:
                        import stringzilla as sz

                        macro_name = sz.Str(rest.strip())
                    elif directive_type == "define":
                        import stringzilla as sz

                        parts = rest.split(None, 1)
                        macro_name = sz.Str(parts[0]) if parts else sz.Str("")
                        macro_value = sz.Str(parts[1]) if len(parts) > 1 else sz.Str("1")
                        # Handle function-like macros
                        if "(" in str(macro_name):
                            macro_name = sz.Str(str(macro_name).split("(")[0])
                    elif directive_type == "undef":
                        import stringzilla as sz

                        macro_name = sz.Str(rest.strip())

                    directive = PreprocessorDirective(
                        line_num=line_num,
                        byte_pos=line_byte_offsets[line_num],
                        directive_type=directive_type,
                        continuation_lines=0,
                        condition=condition,
                        macro_name=macro_name,
                        macro_value=macro_value,
                    )

                    directives.append(directive)
                    directive_by_line[line_num] = directive

                    # Track positions by type for compatibility
                    if directive_type not in directive_positions:
                        directive_positions[directive_type] = []
                    directive_positions[directive_type].append(line_byte_offsets[line_num])

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
            includes=[],
            defines=[],
            magic_flags=[],
        )

    def test_expression_evaluation_basic_sz(self):
        """Test basic expression evaluation with StringZilla"""
        import stringzilla as sz

        # Test simple numeric expressions
        assert self.processor._evaluate_expression_sz(sz.Str("1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1 + 1")) == 2

    def test_expression_evaluation_comparisons_sz(self):
        """Test comparison operators with StringZilla"""
        import stringzilla as sz

        # Test == operator
        assert self.processor._evaluate_expression_sz(sz.Str("1 == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 == 0")) == 0

        # Test != operator
        assert self.processor._evaluate_expression_sz(sz.Str("1 != 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 != 1")) == 0

        # Test > operator
        assert self.processor._evaluate_expression_sz(sz.Str("2 > 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 > 2")) == 0

    def test_expression_evaluation_logical_sz(self):
        """Test logical operators with StringZilla"""
        import stringzilla as sz

        # Test && operator
        assert self.processor._evaluate_expression_sz(sz.Str("1 && 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 && 0")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("0 && 1")) == 0

        # Test || operator
        assert self.processor._evaluate_expression_sz(sz.Str("1 || 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0 || 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0 || 0")) == 0

    def test_expression_evaluation_complex_sz(self):
        """Test complex expressions combining operators with StringZilla"""
        import stringzilla as sz

        # Test combinations
        assert self.processor._evaluate_expression_sz(sz.Str("1 != 0 && 2 > 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 == 0 || 2 == 2")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(1 + 1) == 2")) == 1

    def test_macro_expansion_sz(self):
        """Test macro expansion in expressions with StringZilla"""
        import stringzilla as sz

        # Test simple macro expansion
        assert self.processor._evaluate_expression_sz(sz.Str("TEST_MACRO")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION")) == 3

        # Test macro in comparisons
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION == 3")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION != 2")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("COUNT > 3")) == 1

    def test_defined_expressions_sz(self):
        """Test defined() expressions with StringZilla"""
        import stringzilla as sz

        # Test defined() function
        assert self.processor._evaluate_expression_sz(sz.Str("defined(TEST_MACRO)")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined(UNDEFINED_MACRO)")) == 0

        # Test defined() in complex expressions
        assert self.processor._evaluate_expression_sz(sz.Str("defined(TEST_MACRO) && TEST_MACRO == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined(VERSION) && VERSION > 2")) == 1

    def test_numeric_literal_parsing_sz(self):
        """Test hex, binary, and octal numeric literals in expressions with StringZilla"""
        import stringzilla as sz

        assert self.processor._evaluate_expression_sz(sz.Str("0x10 == 16")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0b1010 == 10")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("010 == 8")) == 1  # octal
        assert self.processor._evaluate_expression_sz(sz.Str("0 == 0")) == 1

    def test_bitwise_operators_sz(self):
        """Test bitwise and shift operators in expressions with StringZilla"""
        import stringzilla as sz

        assert self.processor._evaluate_expression_sz(sz.Str("1 & 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 | 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 ^ 1")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("~0 == -1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(1 << 3) == 8")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(8 >> 2) == 2")) == 1

    def test_recursive_macro_expansion_sz(self):
        """Test recursive macro expansion functionality with StringZilla"""
        import stringzilla as sz

        # Test simple case
        result = self.processor._recursive_expand_macros_sz(sz.Str("VERSION"))
        assert result == "3"

        # Test recursive expansion
        processor_with_recursive = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("C"), sz.Str("C"): sz.Str("42")}, verbose=0
        )

        result = processor_with_recursive._recursive_expand_macros_sz(sz.Str("A"))
        assert result == "42"

        # Test max iterations protection (prevent infinite loops)
        processor_with_loop = SimplePreprocessor({sz.Str("X"): sz.Str("Y"), sz.Str("Y"): sz.Str("X")}, verbose=0)

        result = processor_with_loop._recursive_expand_macros_sz(sz.Str("X"), max_iterations=5)
        # Should stop after max_iterations and return last value
        assert result in ["X", "Y"]  # Could be either depending on iteration count

    def test_recursive_expansion_warns_on_truncation(self, capsys):
        """I-3 regression: hitting max_iterations on a still-mutating
        expression must emit a warning at verbose>=1, not silently return
        a truncated result. Pathological recursive macros otherwise hide
        broken user definitions."""
        import stringzilla as sz

        # A <-> B forms a 2-cycle that never converges
        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")},
            verbose=1,
        )
        processor._recursive_expand_macros_sz(sz.Str("A"), max_iterations=4)
        captured = capsys.readouterr()
        assert "max_iterations" in captured.out, f"Expected truncation warning in output, got: {captured.out!r}"
        assert "recursive macro" in captured.out

    def test_recursive_expansion_no_warn_when_converged(self, capsys):
        """Convergence within the iteration cap must NOT emit a warning."""
        import stringzilla as sz

        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("C"), sz.Str("C"): sz.Str("42")},
            verbose=9,
        )
        result = processor._recursive_expand_macros_sz(sz.Str("A"))
        captured = capsys.readouterr()
        assert result == "42"
        assert "max_iterations" not in captured.out

    def test_recursive_expansion_truncation_silent_when_quiet(self, capsys):
        """At verbose=0 the truncation warning is suppressed."""
        import stringzilla as sz

        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")},
            verbose=0,
        )
        processor._recursive_expand_macros_sz(sz.Str("A"), max_iterations=4)
        captured = capsys.readouterr()
        assert "max_iterations" not in captured.out

    def test_comment_stripping_sz(self):
        """Test C/C++ style comment stripping from StringZilla expressions"""
        import stringzilla as sz

        # Test basic line comment stripping
        result = self.processor._strip_comments_sz(sz.Str("1 + 1 // this is a comment"))
        assert result == "1 + 1"

        # Test line comment at beginning
        result = self.processor._strip_comments_sz(sz.Str("// comment only"))
        assert result == ""

        # Test block comment stripping
        result = self.processor._strip_comments_sz(sz.Str("1 + /* block */ 1"))
        assert result == "1 + 1"

        # Test expression without comments
        result = self.processor._strip_comments_sz(sz.Str("1 + 1"))
        assert result == "1 + 1"

    def test_conditional_compilation_ifdef(self):
        """Test #ifdef handling"""
        text = dedent("""
            #ifdef TEST_MACRO
            #include "test.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 contains '#include "test.h"'
        assert 1 in active_lines

    def test_conditional_compilation_ifndef(self):
        """Test #ifndef handling"""
        text = dedent("""
            #ifndef UNDEFINED_MACRO
            #include "test.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 contains '#include "test.h"'
        assert 1 in active_lines

    def test_conditional_compilation_if_simple(self):
        """Test simple #if handling"""
        text = dedent("""
            #if VERSION == 3
            #include "version3.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 contains '#include "version3.h"'
        assert 1 in active_lines

    def test_conditional_compilation_if_complex(self):
        """Test complex #if expressions"""
        text = dedent("""
            #if defined(VERSION) && VERSION > 2
            #include "advanced.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 contains '#include "advanced.h"'
        assert 1 in active_lines

    def test_conditional_compilation_if_with_not_equal(self):
        """Test #if with != operator (the problematic case)"""
        text = dedent("""
            #if COUNT != 0
            #include "nonzero.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 contains '#include "nonzero.h"'
        assert 1 in active_lines

    def test_conditional_compilation_nested(self):
        """Test nested conditional compilation"""
        text = dedent("""
            #ifdef TEST_MACRO
                #if VERSION >= 3
                    #include "test_v3.h"
                #endif
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 2 contains '#include "test_v3.h"'
        assert 2 in active_lines

    def test_conditional_compilation_else(self):
        """Test #else handling"""
        text = dedent("""
            #ifdef UNDEFINED_MACRO
            #include "undefined.h"
            #else
            #include "defined.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 3 contains '#include "defined.h"', line 1 should not be active
        assert 3 in active_lines
        assert 1 not in active_lines

    def test_conditional_compilation_elif(self):
        """Test #elif handling"""
        text = dedent("""
            #if VERSION == 1
            #include "version1.h"
            #elif VERSION == 2
            #include "version2.h"
            #elif VERSION == 3
            #include "version3.h"
            #else
            #include "default.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 5 contains '#include "version3.h"', others should not be active
        assert 5 in active_lines
        assert 1 not in active_lines
        assert 3 not in active_lines
        assert 7 not in active_lines

    def test_macro_define_and_use(self):
        """Test #define and subsequent use"""
        text = dedent("""
            #define NEW_MACRO 42
            #if NEW_MACRO == 42
            #include "forty_two.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 0 contains #define, line 2 contains '#include "forty_two.h"'
        assert 0 in active_lines
        assert 2 in active_lines

    def test_macro_undef(self):
        """Test #undef functionality"""
        text = dedent("""
            #ifdef TEST_MACRO
            #include "before_undef.h"
            #endif
            #undef TEST_MACRO
            #ifdef TEST_MACRO
            #include "after_undef.h"
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 should be active, line 3 has #undef, line 5 should not be active
        assert 1 in active_lines
        assert 3 in active_lines  # #undef directive
        assert 5 not in active_lines

    def test_failing_scenario_use_epoll(self):
        """Test the exact scenario that's failing in the nested macros test"""
        import stringzilla as sz

        # Set up macros exactly as in the failing test
        failing_macros = {
            sz.Str("BUILD_CONFIG"): sz.Str("2"),
            sz.Str("__linux__"): sz.Str("1"),
            sz.Str("USE_EPOLL"): sz.Str("1"),
            sz.Str("ENABLE_THREADING"): sz.Str("1"),
            sz.Str("THREAD_COUNT"): sz.Str("4"),
            sz.Str("NUMA_SUPPORT"): sz.Str("1"),
        }
        processor = SimplePreprocessor(failing_macros, verbose=0)

        # Test the exact problematic condition
        text = dedent("""
            #if defined(USE_EPOLL) && USE_EPOLL != 0
                #ifdef ENABLE_THREADING
                    #if defined(THREAD_COUNT) && THREAD_COUNT > 1
                        #include "linux_epoll_threading.hpp"
                        #ifdef NUMA_SUPPORT
                            #if NUMA_SUPPORT == 1
                                #include "numa_threading.hpp"
                            #endif
                        #endif
                    #endif
                #endif
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)

        # These should be included (lines 3 and 6)
        assert 3 in active_lines  # #include "linux_epoll_threading.hpp"
        assert 6 in active_lines  # #include "numa_threading.hpp"

    def test_platform_macros(self):
        """Test platform-specific macro initialization via compiler_macros"""
        # Since our simplified compiler_macros only queries the compiler,
        # and doesn't add platform macros without a compiler,
        # we'll test both with and without a compiler
        # Test 1: Without compiler (empty path)
        import stringzilla as sz

        import compiletools.compiler_macros

        macros_empty_raw = compiletools.compiler_macros.get_compiler_macros("", verbose=0)
        macros_empty = {sz.Str(k): sz.Str(v) for k, v in macros_empty_raw.items()}
        processor_empty = SimplePreprocessor(macros_empty, verbose=0)
        # Should work with empty macros
        assert processor_empty.macros == macros_empty

        # Test 2: With mocked compiler response
        from unittest.mock import MagicMock, patch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "#define __linux__ 1\n#define __GNUC__ 11\n#define __x86_64__ 1"

        with patch("subprocess.run", return_value=mock_result):
            # Clear cache to ensure fresh call
            compiletools.compiler_macros.clear_cache()
            macros_raw = compiletools.compiler_macros.get_compiler_macros("gcc", verbose=0)
            macros = {sz.Str(k): sz.Str(v) for k, v in macros_raw.items()}
            processor = SimplePreprocessor(macros, verbose=0)

            # Verify the mocked macros are present
            import stringzilla as sz

            assert sz.Str("__linux__") in processor.macros
            assert processor.macros[sz.Str("__linux__")] == sz.Str("1")
            assert sz.Str("__GNUC__") in processor.macros
            assert processor.macros[sz.Str("__GNUC__")] == sz.Str("11")

    def test_if_with_comments(self):
        """Test #if directive with C++ style comments"""
        text = dedent("""
            #if 1 // this should be true
                included_line
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        assert 1 in active_lines

    def test_block_comment_stripping(self):
        """Test that block comments do not break expression parsing"""
        text = dedent("""
            #if /* block */ 1 /* more */
            ok
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        assert 1 in active_lines


class TestExpandHasFunctions:
    """Tests for __has_* preprocessor function expansion (Cycles 4-5)."""

    def setup_method(self):
        import stringzilla as sz

        from compiletools.build_context import BuildContext

        self.ctx = BuildContext()

        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock_get_filepath = self.patcher.start()
        self.mock_get_filepath.return_value = "<test-file>"

        self.macros = {sz.Str("TEST_MACRO"): sz.Str("1")}

    def teardown_method(self):
        self.patcher.stop()

    def test_basic_has_include_expands_to_1(self):
        """__has_include(<iostream>) should expand to '1' when compiler says true."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = processor._expand_has_functions_sz(sz.Str("__has_include(<iostream>)"))
            assert str(result) == "1"

    def test_basic_has_include_expands_to_0(self):
        """__has_include(<nonexistent.h>) should expand to '0' when compiler says false."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            result = processor._expand_has_functions_sz(sz.Str("__has_include(<nonexistent.h>)"))
            assert str(result) == "0"

    def test_mixed_multiple_has_include(self):
        """Both __has_include calls should be expanded in a compound expression."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        def mock_query(compiler, call_str, cppflags="", verbose=0):
            if "<a>" in call_str:
                return 1
            if "<b>" in call_str:
                return 0
            return 0

        with patch("compiletools.compiler_macros.query_has_function", side_effect=mock_query):
            result = processor._expand_has_functions_sz(sz.Str("__has_include(<a>) && __has_include(<b>)"))
            assert str(result) == "1 && 0"

    def test_quoted_header(self):
        """__has_include("local.h") should preserve the quoted argument."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = processor._expand_has_functions_sz(sz.Str('__has_include("local.h")'))
            assert str(result) == "1"
            # Verify the full call was passed to the compiler
            mock_query.assert_called_once_with("gcc", '__has_include("local.h")', "", 0)

    def test_has_builtin(self):
        """__has_builtin(__builtin_expect) should work for non-include __has_* functions."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = processor._expand_has_functions_sz(sz.Str("__has_builtin(__builtin_expect)"))
            assert str(result) == "1"

    def test_no_compiler_evaluates_to_0(self):
        """With no compiler_path, __has_* calls should evaluate to 0 (backward compat)."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="")

        result = processor._expand_has_functions_sz(sz.Str("__has_include(<iostream>)"))
        assert str(result) == "0"

    def test_not_a_function_call_left_unchanged(self):
        """Identifiers starting with __has_ but without parens should be left unchanged."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        result = processor._expand_has_functions_sz(sz.Str("__has_value"))
        assert str(result) == "__has_value"

    def test_has_in_larger_identifier_left_unchanged(self):
        """__has_ as part of a larger identifier (preceded by alpha/underscore) left unchanged."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        result = processor._expand_has_functions_sz(sz.Str("my__has_include(<x>)"))
        # 'my' prefix means it's part of another identifier
        assert "__has_include" in str(result)

    # Cycle 6: Integration through _evaluate_expression_sz()

    def test_evaluate_expression_with_has_include_and_defined(self):
        """__has_include and defined() should both work in a single expression."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = processor._evaluate_expression_sz(sz.Str("__has_include(<iostream>) && defined(TEST_MACRO)"))
            assert result == 1

    def test_evaluate_expression_has_include_false(self):
        """When __has_include is false, expression should evaluate to 0."""
        import stringzilla as sz

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            result = processor._evaluate_expression_sz(sz.Str("__has_include(<nonexistent.h>)"))
            assert result == 0

    # Cycle 7: End-to-end through process_structured()

    def test_process_structured_has_include_true(self):
        """#if __has_include(<iostream>) should include content when compiler says true."""
        from textwrap import dedent

        text = dedent("""\
            #if __has_include(<iostream>)
            #include <special.h>
            #endif""")

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        file_result = self._create_file_analysis_result(text)

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            active_lines = processor.process_structured(file_result, self.ctx)
            # Line 1 (0-based) is "#include <special.h>" — should be active
            assert 1 in active_lines

    def test_process_structured_has_include_false(self):
        """#if __has_include(<nonexistent>) should exclude content when compiler says false."""
        from textwrap import dedent

        text = dedent("""\
            #if __has_include(<nonexistent.h>)
            #include <special.h>
            #endif""")

        processor = SimplePreprocessor(self.macros, compiler_path="gcc")

        file_result = self._create_file_analysis_result(text)

        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            active_lines = processor.process_structured(file_result, self.ctx)
            # Line 1 should NOT be active
            assert 1 not in active_lines

    # Cycle 8: Threading through get_or_compute_preprocessing()

    def test_get_or_compute_preprocessing_with_compiler(self):
        """get_or_compute_preprocessing should read compiler_path from MacroState."""
        import stringzilla as sz

        from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing

        text = "#if __has_include(<iostream>)\n#include <special.h>\n#endif"
        file_result = self._create_file_analysis_result(text)

        core = {sz.Str("__GNUC__"): sz.Str("11")}
        macros = MacroState(core, compiler_path="gcc", cppflags="-I/usr/include")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = get_or_compute_preprocessing(file_result, macros, verbose=0, context=self.ctx)
            assert 1 in result.active_lines

    def test_get_or_compute_preprocessing_without_compiler(self):
        """Without compiler_path on MacroState, __has_include should evaluate to 0."""
        import stringzilla as sz

        from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing

        text = "#if __has_include(<iostream>)\n#include <special.h>\n#endif"
        file_result = self._create_file_analysis_result(text)

        core = {sz.Str("__GNUC__"): sz.Str("11")}
        macros = MacroState(core)

        result = get_or_compute_preprocessing(file_result, macros, verbose=0, context=self.ctx)
        assert 1 not in result.active_lines

    def _create_file_analysis_result(self, text):
        """Helper to create FileAnalysisResult for testing."""
        import re

        from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective

        lines = text.split("\n")

        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode("utf-8")) + 1

        directives = []
        directive_by_line = {}
        directive_positions = {}

        for line_num, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                match = re.match(r"^\s*#\s*([a-zA-Z_]+)(?:\s+(.*))?", stripped)
                if match:
                    import stringzilla as sz

                    directive_type = match.group(1)
                    rest = match.group(2) or ""

                    condition = None
                    macro_name = None
                    macro_value = None

                    if directive_type in ["if", "elif"]:
                        condition = sz.Str(rest.strip())
                    elif directive_type in ["ifdef", "ifndef"]:
                        macro_name = sz.Str(rest.strip())
                    elif directive_type == "define":
                        parts = rest.split(None, 1)
                        macro_name = sz.Str(parts[0]) if parts else sz.Str("")
                        macro_value = sz.Str(parts[1]) if len(parts) > 1 else sz.Str("1")
                        if "(" in str(macro_name):
                            macro_name = sz.Str(str(macro_name).split("(")[0])
                    elif directive_type == "undef":
                        macro_name = sz.Str(rest.strip())

                    directive = PreprocessorDirective(
                        line_num=line_num,
                        byte_pos=line_byte_offsets[line_num],
                        directive_type=directive_type,
                        continuation_lines=0,
                        condition=condition,
                        macro_name=macro_name,
                        macro_value=macro_value,
                    )

                    directives.append(directive)
                    directive_by_line[line_num] = directive

                    if directive_type not in directive_positions:
                        directive_positions[directive_type] = []
                    directive_positions[directive_type].append(line_byte_offsets[line_num])

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
            includes=[],
            defines=[],
            magic_flags=[],
        )


class TestSimplePreprocessorEdgeCases:
    """Tests for uncovered edge cases in SimplePreprocessor."""

    def setup_method(self):
        import stringzilla as sz

        from compiletools.build_context import BuildContext

        self.ctx = BuildContext()

        self.patcher = patch("compiletools.global_hash_registry.get_filepath_by_hash")
        self.mock_get_filepath = self.patcher.start()
        self.mock_get_filepath.return_value = "<test-file>"

        self.macros = {
            sz.Str("DEFINED_MACRO"): sz.Str("1"),
            sz.Str("VERSION"): sz.Str("3"),
        }
        self.processor = SimplePreprocessor(self.macros, verbose=0)

    def teardown_method(self):
        self.patcher.stop()

    def _create_file_analysis_result(self, text):
        """Reuse helper from TestSimplePreprocessor."""
        return TestSimplePreprocessor._create_file_analysis_result(None, text)

    def test_unclosed_block_comment(self):
        """Unclosed /* comment should skip the rest of the expression."""
        import stringzilla as sz

        result = self.processor._strip_comments_sz(sz.Str("1 + /* unclosed"))
        assert "unclosed" not in str(result)
        assert "1 +" in str(result)

    def test_defined_space_form(self):
        """'defined MACRO' (without parens) should work."""
        import stringzilla as sz

        result = self.processor._expand_defined_sz(sz.Str("defined DEFINED_MACRO"))
        assert str(result) == "1"

        result = self.processor._expand_defined_sz(sz.Str("defined NONEXISTENT"))
        assert str(result) == "0"

    def test_defined_space_form_in_expression(self):
        """'defined MACRO' should evaluate correctly in full expression."""
        import stringzilla as sz

        result = self.processor._evaluate_expression_sz(sz.Str("defined DEFINED_MACRO && 1"))
        assert result == 1

    def test_defined_as_part_of_identifier_prefix(self):
        """'defined' preceded by alpha should not be treated as keyword."""
        import stringzilla as sz

        # 'predefined' contains 'defined' but shouldn't be treated as keyword
        result = self.processor._expand_defined_sz(sz.Str("predefined"))
        assert str(result) == "predefined"

    def test_defined_as_part_of_identifier_suffix(self):
        """'defined' followed by alpha (no space/paren) should not be treated as keyword."""
        import stringzilla as sz

        result = self.processor._expand_defined_sz(sz.Str("definedX"))
        assert "definedX" in str(result)

    def test_defined_at_end_of_string(self):
        """'defined' at end with no macro after it."""
        import stringzilla as sz

        result = self.processor._expand_defined_sz(sz.Str("defined"))
        assert "defined" in str(result)

    def test_defined_with_whitespace_only_after(self):
        """'defined  ' with only whitespace after."""
        import stringzilla as sz

        result = self.processor._expand_defined_sz(sz.Str("defined   "))
        # Should not crash, keeps original text
        assert "defined" in str(result)

    def test_safe_eval_unsafe_expression(self):
        """_safe_eval should raise ValueError for unsafe expressions."""
        import pytest

        with pytest.raises(ValueError, match="Unsafe expression"):
            self.processor._safe_eval("__import__('os')")

    def test_safe_eval_failure_returns_0(self):
        """_safe_eval should return 0 when eval fails on a safe-looking expression."""
        # Expression that matches the regex but fails at eval time
        result = self.processor._safe_eval("1 2")
        assert result == 0

    def test_verbose_debug_output(self, capsys):
        """Verbose mode prints debug info for directive handling."""
        import stringzilla as sz

        verbose_proc = SimplePreprocessor({sz.Str("X"): sz.Str("1")}, verbose=9)
        text = dedent("""
            #ifdef X
            line
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "#ifdef" in out
        assert "#endif" in out

    def test_verbose_define_undef(self, capsys):
        """Verbose mode prints debug for #define and #undef."""

        verbose_proc = SimplePreprocessor({}, verbose=9)
        text = dedent("""
            #define FOO 42
            #undef FOO
        """).strip()
        file_result = self._create_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "defined macro FOO" in out
        assert "undefined macro FOO" in out

    def test_verbose_ifndef(self, capsys):
        """Verbose mode prints debug for #ifndef."""

        verbose_proc = SimplePreprocessor({}, verbose=9)
        text = dedent("""
            #ifndef GUARD
            line
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "#ifndef" in out

    def test_verbose_if_elif_else(self, capsys):
        """Verbose mode prints debug for #if, #elif, #else."""
        import stringzilla as sz

        verbose_proc = SimplePreprocessor({sz.Str("V"): sz.Str("2")}, verbose=9)
        text = dedent("""
            #if V == 1
            a
            #elif V == 2
            b
            #else
            c
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "#if" in out
        assert "#elif" in out
        assert "#else" in out

    def test_if_evaluation_failure_assumes_false(self):
        """#if with unparseable expression should assume false."""

        processor = SimplePreprocessor({}, verbose=0)
        text = dedent("""
            #if __has_cpp_attribute(nodiscard)
            included
            #endif
        """).strip()
        # __has_cpp_attribute isn't a known function, will fail to eval
        file_result = self._create_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 1 not in active_lines  # 'included' line should not be active

    def test_elif_evaluation_failure_assumes_false(self):
        """#elif with unparseable expression should assume false."""

        processor = SimplePreprocessor({}, verbose=0)
        text = dedent("""
            #if 0
            a
            #elif some_invalid_expr()
            b
            #else
            c
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 3 not in active_lines  # 'b' should not be active
        assert 5 in active_lines  # 'c' should be active

    def test_if_no_condition(self):
        """#if with no condition should assume false."""
        from compiletools.file_analyzer import PreprocessorDirective

        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="if",
            continuation_lines=0,
            condition=None,
            macro_name=None,
            macro_value=None,
        )
        condition_stack = [(True, False, False)]
        self.processor._handle_if_structured(directive, condition_stack)
        assert condition_stack[-1][0] is False

    def test_elif_no_condition(self):
        """#elif with no condition should assume false."""
        from compiletools.file_analyzer import PreprocessorDirective

        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="elif",
            continuation_lines=0,
            condition=None,
            macro_name=None,
            macro_value=None,
        )
        condition_stack = [(True, False, False), (False, False, False)]
        self.processor._handle_elif_structured(directive, condition_stack)
        assert condition_stack[-1][0] is False

    def test_continuation_lines(self):
        """Directives with continuation_lines should include continuation in active_lines."""
        import stringzilla as sz

        from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective

        # Simulate a #define with a continuation line
        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="define",
            continuation_lines=1,
            macro_name=sz.Str("MULTI"),
            macro_value=sz.Str("line1 line2"),
            condition=None,
        )
        file_result = FileAnalysisResult(
            line_count=3,
            line_byte_offsets=[0, 20, 40],
            include_positions=[],
            magic_positions=[],
            directive_positions={"define": [0]},
            directives=[directive],
            directive_by_line={0: directive},
            bytes_analyzed=60,
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[],
        )
        active_lines = self.processor.process_structured(file_result, self.ctx)
        assert 0 in active_lines  # directive line
        assert 1 in active_lines  # continuation line
        assert 2 in active_lines  # regular line

    def test_include_guard_skipped(self):
        """Include guard macro should not be added to macro state."""
        import stringzilla as sz

        processor = SimplePreprocessor({}, verbose=9)
        text = dedent("""
            #ifndef MY_HEADER_H
            #define MY_HEADER_H
            content
            #endif
        """).strip()
        file_result = self._create_file_analysis_result(text)
        file_result.include_guard = sz.Str("MY_HEADER_H")
        processor.process_structured(file_result, self.ctx)
        assert sz.Str("MY_HEADER_H") not in processor.macros

    def test_unknown_directive_verbose(self, capsys):
        """Unknown directive with verbose >= 8 prints debug."""

        from compiletools.file_analyzer import PreprocessorDirective

        verbose_proc = SimplePreprocessor({}, verbose=8)
        directive = PreprocessorDirective(
            line_num=0,
            byte_pos=0,
            directive_type="pragma",
            continuation_lines=0,
            condition=None,
            macro_name=None,
            macro_value=None,
        )
        condition_stack = [(True, False, False)]
        result = verbose_proc._handle_directive_structured(directive, condition_stack, 1)
        assert result is False
        out = capsys.readouterr().out
        assert "Ignoring unknown directive" in out

    def test_block_comment_with_content_after(self):
        """Block comment followed by expression should work."""
        import stringzilla as sz

        result = self.processor._strip_comments_sz(sz.Str("/* comment */ 42"))
        assert "42" in str(result)
        assert "comment" not in str(result)

    def test_multiple_block_comments(self):
        """Multiple block comments in one expression."""
        import stringzilla as sz

        result = self.processor._strip_comments_sz(sz.Str("1 /* a */ + /* b */ 2"))
        assert str(result) == "1 + 2"

    def test_empty_block_comment_result(self):
        """Block comment that leaves nothing."""
        import stringzilla as sz

        result = self.processor._strip_comments_sz(sz.Str("/* everything is a comment */"))
        assert str(result) == ""


class TestPrintPreprocessorStats:
    """Test the print_preprocessor_stats diagnostic function."""

    def test_print_stats(self, capsys):
        from compiletools.simple_preprocessor import _stats, print_preprocessor_stats

        # Save and restore stats
        old_count = _stats["call_count"]
        _stats["call_count"] = 42
        try:
            print_preprocessor_stats()
            out = capsys.readouterr().out
            assert "42" in out
            assert "SimplePreprocessor" in out
        finally:
            _stats["call_count"] = old_count


class TestMacroHashConsistency:
    """Unit tests for macro hash computation consistency (Phase 0)"""

    def test_hash_determinism(self):
        """Verify same macro state always produces same hash."""
        import stringzilla as sz

        core = {}
        variable = {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("value"), sz.Str("BAZ"): sz.Str("0x100")}
        macros = MacroState(core, variable)

        hash1 = macros.get_hash()
        hash2 = macros.get_hash()

        assert hash1 == hash2, "Same macro state should produce same hash"
        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"

    def test_hash_ordering_independence(self):
        """Verify hash is same regardless of insertion order."""
        import stringzilla as sz

        core = {}
        # Create dicts with different insertion orders
        variable1 = {sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("2"), sz.Str("C"): sz.Str("3")}

        variable2 = {sz.Str("C"): sz.Str("3"), sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("2")}

        macros1 = MacroState(core, variable1)
        macros2 = MacroState(core, variable2)

        hash1 = macros1.get_hash()
        hash2 = macros2.get_hash()

        assert hash1 == hash2, "Hash should be independent of insertion order"

    def test_hash_sensitivity_to_changes(self):
        """Verify different macro states produce different hashes."""
        import stringzilla as sz

        core = {}
        macros1 = MacroState(core, {sz.Str("FOO"): sz.Str("1")})
        macros2 = MacroState(core, {sz.Str("FOO"): sz.Str("2")})  # Different value
        macros3 = MacroState(core, {sz.Str("BAR"): sz.Str("1")})  # Different key
        macros4 = MacroState(core, {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("2")})  # Additional key

        hash1 = macros1.get_hash()
        hash2 = macros2.get_hash()
        hash3 = macros3.get_hash()
        hash4 = macros4.get_hash()

        assert hash1 != hash2, "Different macro values should produce different hashes"
        assert hash1 != hash3, "Different macro keys should produce different hashes"
        assert hash1 != hash4, "Additional macros should produce different hash"
        assert hash2 != hash3, "All different states should have unique hashes"
        assert hash2 != hash4, "All different states should have unique hashes"
        assert hash3 != hash4, "All different states should have unique hashes"

    def test_hash_empty_macro_state(self):
        """Verify empty macro state has consistent hash."""

        empty1 = MacroState({}, {})
        empty2 = MacroState({}, {})

        hash1 = empty1.get_hash()
        hash2 = empty2.get_hash()

        assert hash1 == hash2, "Empty macro states should have same hash"
        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"

    def test_hash_with_special_characters(self):
        """Verify hash handles special characters in macro values."""
        import stringzilla as sz

        core = {}
        macros1 = MacroState(
            core, {sz.Str("PATH"): sz.Str("/usr/local/include"), sz.Str("FLAGS"): sz.Str("-O2 -g -Wall")}
        )

        macros2 = MacroState(
            core,
            {
                sz.Str("PATH"): sz.Str("/usr/local/include"),
                sz.Str("FLAGS"): sz.Str("-O3 -g -Wall"),  # Different flag
            },
        )

        hash1 = macros1.get_hash()
        hash2 = macros2.get_hash()

        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"
        assert hash1 != hash2, "Different values with special chars should have different hashes"

    def test_hash_cross_module_consistency(self):
        """Verify hash computation is consistent and accessible."""
        import stringzilla as sz

        core = {}
        variable = {sz.Str("LINUX"): sz.Str("1"), sz.Str("DEBUG"): sz.Str("1"), sz.Str("VERSION"): sz.Str("100")}
        macros = MacroState(core, variable)

        # Hash computation (used by magicflags for convergence detection)
        hash_result = macros.get_hash()

        # Verify hash type is stable string (hex digest of 64-bit hash)
        assert isinstance(hash_result, str), "Hash should be a hex digest string"
        assert len(hash_result) == 16, "Hash should be 64-bit (16 hex chars)"

        # Verify it's deterministic
        hash_again = macros.get_hash()
        assert hash_result == hash_again, "Hash should be deterministic"


class TestMacroStateBuildContextHash:
    """Tests for MacroState hashing of build context (cflags, cxxflags, cppflags, compiler_path).

    These tests verify the fix for the content-addressable hash collision bug where
    objects compiled with different flags (e.g. -O0 vs -O2, or different -I paths)
    were incorrectly reused because the hash omitted non-macro compile flags.
    """

    def test_macro_state_hash_differs_with_different_cflags(self):
        """Object hash must change when compile flags change (e.g., -O0 vs -O2)."""
        import stringzilla as sz

        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="-O0", cxxflags="")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="-O2", cxxflags="")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_cxxflags(self):
        """Object hash must change when C++ standard changes."""
        import stringzilla as sz

        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="", cxxflags="-std=c++17")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="", cxxflags="-std=c++20")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_cppflags(self):
        """Object hash must change when include paths change (e.g., different library version)."""
        import stringzilla as sz

        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="-I/opt/libfoo/v1/include")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="-I/opt/libfoo/v2/include")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_compiler(self):
        """Object hash must change when compiler changes."""
        import stringzilla as sz

        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++")
        ms2 = MacroState(core, {}, compiler_path="clang++")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_without_core_ignores_build_context(self):
        """Preprocessing cache key (include_core=False) must NOT be affected by build flags."""
        import stringzilla as sz

        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="-I/a", cflags="-O0", cxxflags="-std=c++17")
        ms2 = MacroState(core, {}, compiler_path="clang++", cppflags="-I/b", cflags="-O2", cxxflags="-std=c++20")
        assert ms1.get_hash(include_core=False) == ms2.get_hash(include_core=False)

    def test_with_updates_propagates_build_context(self):
        """with_updates must carry cflags/cxxflags to the new MacroState."""
        import stringzilla as sz

        core = {sz.Str("X"): sz.Str("1")}
        ms = MacroState(core, {}, compiler_path="g++", cppflags="-I/foo", cflags="-O2", cxxflags="-std=c++17")
        ms2 = ms.with_updates({sz.Str("Y"): sz.Str("2")})
        assert ms2.cflags == "-O2"
        assert ms2.cxxflags == "-std=c++17"
        assert ms2.compiler_path == "g++"
        assert ms2.cppflags == "-I/foo"

    def test_without_keys_propagates_build_context(self):
        """without_keys must carry cflags/cxxflags to the new MacroState."""
        import stringzilla as sz

        core = {sz.Str("X"): sz.Str("1")}
        var = {sz.Str("Y"): sz.Str("2")}
        ms = MacroState(core, var, compiler_path="g++", cppflags="-I/foo", cflags="-O2", cxxflags="-std=c++17")
        ms2 = ms.without_keys([sz.Str("Y")])
        assert ms2.cflags == "-O2"
        assert ms2.cxxflags == "-std=c++17"
