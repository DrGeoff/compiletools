import os
import re
import sys
from textwrap import dedent
from unittest.mock import patch

import pytest
import stringzilla as sz

# Add the parent directory to sys.path so we can import ct modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective
from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing
from compiletools.simple_preprocessor import _RE_INTEGER_SUFFIXES, SimplePreprocessor
from compiletools.stringzilla_utils import is_alnum_or_underscore_sz


@pytest.fixture(autouse=True)
def _stub_filepath_by_hash(monkeypatch):
    """Stub get_filepath_by_hash for every test so SimplePreprocessor's
    reverse-lookup of the synthesized content hash doesn't hit the global
    registry (which is empty in unit tests)."""
    monkeypatch.setattr(
        "compiletools.global_hash_registry.get_filepath_by_hash",
        lambda *_a, **_kw: "<test-file>",
    )


def _make_file_analysis_result(text):
    """Build a FileAnalysisResult from raw source text (test helper)."""
    lines = text.split("\n")

    line_byte_offsets = []
    offset = 0
    for line in lines:
        line_byte_offsets.append(offset)
        offset += len(line.encode("utf-8")) + 1  # +1 for \n

    directives = []
    directive_by_line = {}
    directive_positions = {}

    for line_num, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            match = re.match(r"^\s*#\s*([a-zA-Z_]+)(?:\s+(.*))?", stripped)
            if match:
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
                directive_positions.setdefault(directive_type, []).append(line_byte_offsets[line_num])

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


class TestSimplePreprocessor:
    """Unit tests for the SimplePreprocessor class"""

    def setup_method(self):
        """Set up test fixtures before each test method."""
        self.ctx = BuildContext()
        self.macros = {
            sz.Str("TEST_MACRO"): sz.Str("1"),
            sz.Str("FEATURE_A"): sz.Str("1"),
            sz.Str("VERSION"): sz.Str("3"),
            sz.Str("COUNT"): sz.Str("5"),
        }
        self.processor = SimplePreprocessor(self.macros, verbose=0)

    def test_expression_evaluation_basic_sz(self):
        """Test basic expression evaluation with StringZilla"""
        # Test simple numeric expressions
        assert self.processor._evaluate_expression_sz(sz.Str("1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1 + 1")) == 2

    def test_expression_evaluation_comparisons_sz(self):
        """Test comparison operators with StringZilla"""
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
        # Test combinations
        assert self.processor._evaluate_expression_sz(sz.Str("1 != 0 && 2 > 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 == 0 || 2 == 2")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(1 + 1) == 2")) == 1

    def test_macro_expansion_sz(self):
        """Test macro expansion in expressions with StringZilla"""
        # Test simple macro expansion
        assert self.processor._evaluate_expression_sz(sz.Str("TEST_MACRO")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION")) == 3

        # Test macro in comparisons
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION == 3")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("VERSION != 2")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("COUNT > 3")) == 1

    def test_defined_expressions_sz(self):
        """Test defined() expressions with StringZilla"""
        # Test defined() function
        assert self.processor._evaluate_expression_sz(sz.Str("defined(TEST_MACRO)")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined(UNDEFINED_MACRO)")) == 0

        # Test defined() in complex expressions
        assert self.processor._evaluate_expression_sz(sz.Str("defined(TEST_MACRO) && TEST_MACRO == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined(VERSION) && VERSION > 2")) == 1

    def test_digit_adjacent_defined_not_treated_as_operator_sz(self):
        """A6: a digit immediately before 'defined' makes it part of a larger
        pp-token (e.g. '1defined'), so the 'defined' operator must NOT fire.

        The identifier-continuation class includes digits, so the left-boundary
        check rejects 'defined' as an operator here. The malformed token degrades:
        '1defined(FOO)' is left literally unexpanded by _expand_defined_sz (NOT
        rewritten to 1/0), and the whole expression then evaluates to 0 (the
        inactive result the degraded/unrecognized path produces) regardless of
        whether FOO is defined.
        """
        # FOO defined: still must not be recognized as the defined() operator.
        assert str(self.processor._expand_defined_sz(sz.Str("1defined(TEST_MACRO)"))) == "1defined(TEST_MACRO)"
        assert self.processor._evaluate_expression_sz(sz.Str("1defined(TEST_MACRO)")) == 0
        # FOO undefined: same degraded result.
        assert (
            str(self.processor._expand_defined_sz(sz.Str("1defined(UNDEFINED_MACRO)"))) == "1defined(UNDEFINED_MACRO)"
        )
        assert self.processor._evaluate_expression_sz(sz.Str("1defined(UNDEFINED_MACRO)")) == 0

    def test_digit_right_adjacent_defined_not_treated_as_operator_sz(self):
        """A6: a digit immediately AFTER 'defined' makes it part of a larger
        pp-token (e.g. 'defined1'), so the 'defined' operator must NOT fire.

        Symmetric with the left-boundary case (`1defined`). The right-boundary
        guard in `_expand_defined_sz` keys on `is_alnum_or_underscore_sz`, whose
        A6 fix added digits to the identifier-continuation class; this test pins
        that the digit IS in that class, so `defined1` is recognized as one token
        and the operator is rejected. Asserting the predicate directly is what
        makes this fail pre-fix: the downstream macro-name extractor independently
        rejects a digit-led name, so `_expand_defined_sz`'s output coincides on
        either side of the fix — only the boundary predicate observably changes.
        The pass-through assertion documents the resulting end-to-end behavior.
        """
        assert is_alnum_or_underscore_sz(sz.Str("defined1(TEST_MACRO)"), len("defined")) is True
        assert str(self.processor._expand_defined_sz(sz.Str("defined1(TEST_MACRO)"))) == "defined1(TEST_MACRO)"

    def test_defined_still_fires_when_digit_is_operator_separated_sz(self):
        """A6 regression: a digit separated from 'defined' by an operator/space is
        operator-adjacency, NOT identifier-adjacency, so 'defined' STILL fires.

        Also covers the normal parenthesized and space forms.
        """
        # Normal forms unaffected.
        assert self.processor._evaluate_expression_sz(sz.Str("defined(TEST_MACRO)")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined TEST_MACRO")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("defined(UNDEFINED_MACRO)")) == 0
        # Digit separated by an operator: defined() still evaluates, 1 + 1 == 2.
        assert self.processor._evaluate_expression_sz(sz.Str("1 + defined(TEST_MACRO)")) == 2

    def test_numeric_literal_parsing_sz(self):
        """Test hex, binary, and octal numeric literals in expressions with StringZilla"""
        assert self.processor._evaluate_expression_sz(sz.Str("0x10 == 16")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0b1010 == 10")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("010 == 8")) == 1  # octal
        assert self.processor._evaluate_expression_sz(sz.Str("0 == 0")) == 1

    def test_integer_suffixes_on_nondecimal_literals_sz(self):
        """C integer suffixes (U/L/UL/LL/ULL, any order) must strip on hex/bin/oct literals (A8).

        The A8 bug was specific to HEX: the old regex ``(\\d+)[LlUu]+\\b`` only matched
        decimal digit runs, so ``\\d+`` couldn't span the hex letters and a suffixed hex
        literal like ``0xFFUL`` survived unstripped, reaching the tokenizer and raising
        ValueError. Binary/octal/decimal suffixed literals already worked under the old
        regex (``\\d+`` matched the digit run after the ``0b``/``0`` prefix); they are
        retained here as defense-in-depth coverage against future regex changes. The
        literal value itself must be preserved.
        """
        # Hex with suffixes (the A8 break: previously raised ValueError)
        assert self.processor._evaluate_expression_sz(sz.Str("0xFFUL == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0xFFu == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0xFFl == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0XffU == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0xABCUL == 2748")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0xDEADBEEFull == 3735928559")) == 1
        # Binary with suffixes (defense-in-depth: already worked pre-A8)
        assert self.processor._evaluate_expression_sz(sz.Str("0b1010U == 10")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0b1010UL == 10")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0b1010LLU == 10")) == 1
        # Octal (C bare-leading-zero) with suffixes (defense-in-depth: already worked pre-A8)
        assert self.processor._evaluate_expression_sz(sz.Str("0777L == 511")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0777UL == 511")) == 1
        # Decimal control still works
        assert self.processor._evaluate_expression_sz(sz.Str("42UL == 42")) == 1
        # Suffix-bearing literals participate in arithmetic / branch activation
        assert self.processor._evaluate_expression_sz(sz.Str("0x10UL + 0b1U == 17")) == 1

    def test_valid_integer_suffix_forms_still_evaluate_sz(self):
        """Every valid C integer-suffix form strips to its bare value (A5 no-regression).

        Valid forms are ``U?(L|LL)?`` / ``(L|LL)U?`` with at most one ``u`` and a
        matching-case long part — in either order, any ``u``-case. The A5 fix
        constrained the suffix alternation to exactly these forms; this guards
        that none of them regressed to ValueError/0.
        """
        # Unsigned-only, both cases
        assert self.processor._evaluate_expression_sz(sz.Str("1u == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1U == 1")) == 1
        # Long-only, both cases and lengths
        assert self.processor._evaluate_expression_sz(sz.Str("1l == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1L == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1ll == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1LL == 1")) == 1
        # Unsigned-then-long, both orders/cases
        assert self.processor._evaluate_expression_sz(sz.Str("1ul == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1UL == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1lu == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1LU == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1ull == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1ULL == 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("42LLU == 42")) == 1
        # Non-decimal bodies keep their suffixes stripped under the new alternation
        assert self.processor._evaluate_expression_sz(sz.Str("0xFFu == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0xFFUL == 255")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0b101L == 5")) == 1
        # Direct regex check: the suffix is removed, leaving the bare body
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1ULL") == "1"
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "0xFFu") == "0xFF"

    def test_invalid_integer_suffix_runs_not_silently_accepted_sz(self):
        """Invalid suffix runs must NOT be stripped to the bare valid value (A5 fix).

        The old ``[LlUu]+`` alternation matched ANY run of suffix letters, so
        malformed literals like ``1UU``/``1LLL``/``1lL`` were silently rewritten to
        ``1`` and evaluated truthy. C permits only ``U?(L|LL)?`` / ``(L|LL)U?`` with
        at most one ``u`` and a matching-case long part. The constrained regex no
        longer matches these runs, so the literal survives to the parser, which
        rejects it ("trailing tokens" SyntaxError) and ``_safe_eval`` degrades the
        directive to 0 (inactive) rather than accepting the bare integer.
        """
        # Doubled unsigned, triple/over-long, and mixed-case long parts: all invalid C.
        assert self.processor._evaluate_expression_sz(sz.Str("1UU")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1ULUL")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1LLL")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1lL")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1Ll")) == 0
        # In a comparison the bogus literal must not satisfy "== bare value".
        assert self.processor._evaluate_expression_sz(sz.Str("1UU == 1")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1LLL == 1")) == 0
        # Direct regex check: invalid runs leave the literal untouched (no strip).
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1UU") == "1UU"
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1ULUL") == "1ULUL"
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1LLL") == "1LLL"
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1lL") == "1lL"
        assert _RE_INTEGER_SUFFIXES.sub(r"\1", "1Ll") == "1Ll"

    def test_bitwise_operators_sz(self):
        """Test bitwise and shift operators in expressions with StringZilla"""
        assert self.processor._evaluate_expression_sz(sz.Str("1 & 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 | 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 ^ 1")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("~0 == -1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(1 << 3) == 8")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(8 >> 2) == 2")) == 1

    def test_expression_evaluation_uses_c_integer_division_sz(self):
        """Preprocessor division is integer division, not Python float division."""
        assert self.processor._evaluate_expression_sz(sz.Str("4 / 2")) == 2
        assert self.processor._evaluate_expression_sz(sz.Str("5 / 2 == 2")) == 1

    def test_expression_evaluation_uses_c_truncated_modulo_sz(self):
        """C99 truncates modulo toward zero; Python floor-mods toward -inf.

        The eval()-based predecessor returned Python's floor-mod (e.g.
        ``-7 % 2 == 1``); the C-precedence parser truncates so ``-7 % 2 == -1``.
        """
        assert self.processor._evaluate_expression_sz(sz.Str("(-7) % 2")) == -1
        assert self.processor._evaluate_expression_sz(sz.Str("7 % (-2)")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("(-7) / 2")) == -3
        assert self.processor._evaluate_expression_sz(sz.Str("(-7) % 2 == -1")) == 1

    def test_expression_evaluation_uses_c_bitwise_precedence_sz(self):
        """C equality binds tighter than bitwise AND/OR/XOR in #if expressions."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 & 2 == 0")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("1 | 0 == 0")) == 1

    def test_recursive_macro_expansion_sz(self):
        """Test recursive macro expansion functionality with StringZilla"""
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
        """Regression: hitting max_iterations on a still-mutating
        expression must emit a warning at verbose>=1, not silently return
        a truncated result. Pathological recursive macros otherwise hide
        broken user definitions."""
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
        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")},
            verbose=0,
        )
        processor._recursive_expand_macros_sz(sz.Str("A"), max_iterations=4)
        captured = capsys.readouterr()
        assert "max_iterations" not in captured.out

    def test_comment_stripping_sz(self):
        """Test C/C++ style comment stripping from StringZilla expressions"""
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Line 1 should be active, line 3 has #undef, line 5 should not be active
        assert 1 in active_lines
        assert 3 in active_lines  # #undef directive
        assert 5 not in active_lines

    def test_failing_scenario_use_epoll(self):
        """Test the exact scenario that's failing in the nested macros test"""
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        assert 1 in active_lines

    def test_block_comment_stripping(self):
        """Test that block comments do not break expression parsing"""
        text = dedent("""
            #if /* block */ 1 /* more */
            ok
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        assert 1 in active_lines


class TestExpandHasFunctions:
    """Tests for __has_* preprocessor function expansion (Cycles 4-5)."""

    def setup_method(self):
        self.ctx = BuildContext()
        self.macros = {sz.Str("TEST_MACRO"): sz.Str("1")}
        # Default processor for the 11 gcc-compiler tests. test_no_compiler_
        # evaluates_to_0 builds its own with compiler_path="" inline.
        self.processor = SimplePreprocessor(self.macros, compiler_path="gcc")

    def test_basic_has_include_expands_to_1(self):
        """__has_include(<iostream>) should expand to '1' when compiler says true."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include(<iostream>)"))
            assert str(result) == "1"

    def test_basic_has_include_expands_to_0(self):
        """__has_include(<nonexistent.h>) should expand to '0' when compiler says false."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include(<nonexistent.h>)"))
            assert str(result) == "0"

    def test_mixed_multiple_has_include(self):
        """Both __has_include calls should be expanded in a compound expression."""

        def mock_query(compiler, call_str, cppflags="", verbose=0):
            if "<a>" in call_str:
                return 1
            if "<b>" in call_str:
                return 0
            return 0

        with patch("compiletools.compiler_macros.query_has_function", side_effect=mock_query):
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include(<a>) && __has_include(<b>)"))
            assert str(result) == "1 && 0"

    def test_quoted_header(self):
        """__has_include("local.h") should preserve the quoted argument."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = self.processor._expand_has_functions_sz(sz.Str('__has_include("local.h")'))
            assert str(result) == "1"
            # Verify the full call was passed to the compiler
            mock_query.assert_called_once_with("gcc", '__has_include("local.h")', "", 0)

    def test_has_builtin(self):
        """__has_builtin(__builtin_expect) should work for non-include __has_* functions."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = self.processor._expand_has_functions_sz(sz.Str("__has_builtin(__builtin_expect)"))
            assert str(result) == "1"

    def test_object_macro_operand_expanded_before_has_check(self):
        """A18: an object-macro used as a __has_* operand is expanded before the
        has-check consumes it.

        Per the C standard the operand of __has_include / __has_attribute /
        __has_builtin etc. is macro-expanded. The compiler probe runs on a fresh
        stdin TU that knows nothing of our #defines, so the simple preprocessor
        must expand the operand itself before handing the call string to the
        compiler.
        """
        processor = SimplePreprocessor({sz.Str("HEADER"): sz.Str("<foo.h>")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = processor._evaluate_expression_sz(sz.Str("__has_include(HEADER)"))
            assert result == 1
            # The operand must reach the compiler already expanded to <foo.h>,
            # NOT as the literal token HEADER.
            mock_query.assert_called_once_with("gcc", "__has_include(<foo.h>)", "", 0)

    def test_object_macro_operand_expanded_for_has_attribute(self):
        """A18: a macro operand of __has_attribute is expanded before the has-check."""
        processor = SimplePreprocessor({sz.Str("ATTR"): sz.Str("nodiscard")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            processor._evaluate_expression_sz(sz.Str("__has_attribute(ATTR)"))
            mock_query.assert_called_once_with("gcc", "__has_attribute(nodiscard)", "", 0)

    def test_chained_object_macro_operand_fully_expanded(self):
        """A18: a chained macro operand (A -> B -> nodiscard) resolves to a fixed point."""
        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("nodiscard")}, compiler_path="gcc"
        )
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            processor._evaluate_expression_sz(sz.Str("__has_attribute(A)"))
            mock_query.assert_called_once_with("gcc", "__has_attribute(nodiscard)", "", 0)

    def test_quoted_literal_header_operand_not_macro_expanded(self):
        """A1: a quoted header-name operand ("foo.h") is NOT macro-expanded.

        Per C23 6.10.1 the header-name form of a __has_include operand is taken
        literally, not subject to macro expansion. With ``foo`` #defined as
        ``bar``, ``__has_include("foo.h")`` must probe ``"foo.h"`` -- NOT
        ``"bar.h"`` (the A18 bug expanded it unconditionally).
        """
        processor = SimplePreprocessor({sz.Str("foo"): sz.Str("bar")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            processor._expand_has_functions_sz(sz.Str('__has_include("foo.h")'))
            mock_query.assert_called_once_with("gcc", '__has_include("foo.h")', "", 0)

    def test_angle_literal_header_operand_not_macro_expanded(self):
        """A1: an angle-bracket header-name operand (<foo.h>) is NOT macro-expanded.

        With ``foo`` #defined as ``bar``, ``__has_include(<foo.h>)`` must probe
        ``<foo.h>`` -- NOT ``<bar.h>``.
        """
        processor = SimplePreprocessor({sz.Str("foo"): sz.Str("bar")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            processor._expand_has_functions_sz(sz.Str("__has_include(<foo.h>)"))
            mock_query.assert_called_once_with("gcc", "__has_include(<foo.h>)", "", 0)

    def test_literal_header_operand_with_surrounding_whitespace_not_expanded(self):
        """A1: leading whitespace before a header-name operand is stripped before
        the literal-form check, so ``__has_include( "foo.h" )`` is still treated
        as a (non-expanded) header name.
        """
        processor = SimplePreprocessor({sz.Str("foo"): sz.Str("bar")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            processor._expand_has_functions_sz(sz.Str('__has_include( "foo.h" )'))
            (_, call_str, _, _) = mock_query.call_args.args
            assert '"foo.h"' in call_str
            assert '"bar.h"' not in call_str

    def test_defined_operand_not_expanded_with_has_check_present(self):
        """A18 must not regress defined(): defined()'s operand stays unexpanded.

        FOO is a defined macro whose body is BAR (BAR itself is undefined).
        defined(FOO) checks the NAME FOO -> 1; it must NOT become defined(BAR) -> 0.
        """
        processor = SimplePreprocessor({sz.Str("FOO"): sz.Str("BAR")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            assert processor._evaluate_expression_sz(sz.Str("defined(FOO)")) == 1

    def test_non_ascii_byte_in_operand_does_not_crash(self):
        """A9: a non-ASCII byte inside the __has_* operand must not crash the scan.

        The paren-matching loop walks the operand byte-by-byte. Indexing a
        ``sz.Str`` with a bare integer decodes that single byte as UTF-8 in
        isolation, so the leading byte of any multi-byte sequence (here the
        UTF-8 ``ö``) raised ``UnicodeDecodeError``. Slice indexing returns a
        1-char ``Str`` (a replacement char for a lone continuation byte) and
        keeps the scan intact.
        """
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            # Must not raise; the call is recognised and expanded to '1'.
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include(<föö.h>)"))
            assert str(result) == "1"

    def test_non_ascii_byte_where_paren_expected_does_not_crash(self):
        """A9: a non-ASCII byte where the opening paren is expected must not crash.

        After the function name + whitespace skip, the scanner peeks the next
        byte to decide whether this is a call. A multi-byte char there formerly
        raised ``UnicodeDecodeError``; now it is correctly treated as 'not a
        call' and the identifier is left unchanged.
        """
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include é"))
            # Not a function call (no paren) -> name passed through verbatim.
            assert "__has_include" in str(result)

    def test_non_ascii_byte_before_has_does_not_crash(self):
        """A9 (third site): a non-ASCII byte immediately before ``__has_`` must
        not crash the standalone-identifier check.

        When ``has_pos > 0`` the scanner peeks the preceding byte to decide
        whether ``__has_`` is a fresh identifier or the tail of a larger one
        (``my__has_include``). That peek goes through
        ``is_alpha_or_underscore_sz(expr_sz, has_pos - 1)``, which formerly
        indexed the ``sz.Str`` with a bare integer and raised
        ``UnicodeDecodeError`` when the preceding byte was a UTF-8 lead /
        continuation byte (here the trailing byte of ``é``). A UTF-8 byte is
        not an identifier char, so ``__has_`` is correctly a fresh identifier
        and the call is recognised and expanded.
        """
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            # Must not raise; the preceding byte is non-identifier, so the
            # __has_include is a standalone call and expands to '1'.
            result = self.processor._expand_has_functions_sz(sz.Str("é__has_include(<x.h>)"))
            assert str(result).endswith("1")

    def test_digit_adjacent_has_include_does_not_probe(self):
        """A6: a digit immediately before ``__has_`` makes it part of a larger
        pp-token (``FOO1__has_include``), so the ``__has_include`` operator must
        NOT fire and no compiler probe must be issued.

        The left-boundary check uses the identifier-continuation class (letters,
        digits, underscore); a digit there means ``__has_`` is the tail of a
        larger identifier and the call string is passed through verbatim.
        """
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = self.processor._expand_has_functions_sz(sz.Str("FOO1__has_include(<x.h>)"))
            # Passed through unchanged; no probe issued.
            assert str(result) == "FOO1__has_include(<x.h>)"
            mock_query.assert_not_called()

    def test_standalone_has_include_still_probes(self):
        """A6 regression: a standalone ``__has_include`` (no identifier char
        adjacent) still fires and probes the compiler exactly once."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = self.processor._expand_has_functions_sz(sz.Str("__has_include(<x.h>)"))
            assert str(result) == "1"
            mock_query.assert_called_once_with("gcc", "__has_include(<x.h>)", "", 0)

    def test_same_identifier_in_defined_and_has_check_resolves_each_correctly(self):
        """A18 cross-operand: when ONE identifier feeds BOTH defined() and a
        __has_* in the same expression, the two operands have opposite expansion
        contracts and both must hold simultaneously.

        For ``defined(COND) && __has_attribute(COND)`` with COND defined as
        nodiscard:
          - defined(COND) must see COND UNEXPANDED (it tests whether COND is a
            defined macro NAME) -> 1, because COND is in the macro table.
          - __has_attribute(COND) must see COND EXPANDED to nodiscard before the
            probe (the compiler TU knows nothing of our #defines) -> the call
            string handed to the compiler is __has_attribute(nodiscard).

        This only passes because _expand_macros_sz consumes defined() (against
        the raw name, via _expand_defined_sz) and the __has_* call (whose operand
        is expanded internally) BEFORE the general _expand_object_macros_sz pass
        rewrites any remaining bare identifiers. If object-macro expansion ran
        FIRST, the bare COND outside the has-call would become nodiscard, so
        defined() would see defined(nodiscard) -> 0, the && would short-circuit,
        and the overall result would be 0. (Verified: moving _expand_object_macros_sz
        to the front of _expand_macros_sz makes this test fail with 0 == 1.)
        """
        processor = SimplePreprocessor({sz.Str("COND"): sz.Str("nodiscard")}, compiler_path="gcc")
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as mock_query:
            result = processor._evaluate_expression_sz(sz.Str("defined(COND) && __has_attribute(COND)"))
            # Both operands resolved correctly: defined(COND) -> 1 (name was
            # defined) AND __has_attribute -> 1, so the && is true.
            assert result == 1
            # The has-check saw the EXPANDED operand; defined() saw the raw name
            # (otherwise defined(nodiscard) would have short-circuited to 0 and
            # the has probe would never have run / been called with COND).
            mock_query.assert_called_once_with("gcc", "__has_attribute(nodiscard)", "", 0)

    def test_cyclic_has_operand_warns_at_verbose_1(self, capsys):
        """A18 follow-up: a cyclic object-macro pair used as a __has_* operand
        truncates at max_iterations and warns at verbose >= 1 (parity with
        _recursive_expand_macros_sz), instead of silently handing a half-expanded
        token to the probe.
        """
        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")},
            compiler_path="gcc",
            verbose=1,
        )
        result = processor._expand_object_macros_recursive_sz(sz.Str("A"))
        # Cyclic pair never converges; the result is one of the two tokens.
        assert str(result) in ("A", "B")
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "_expand_object_macros_recursive_sz" in captured.out
        assert "max_iterations" in captured.out

    def test_cyclic_has_operand_silent_at_verbose_0(self, capsys):
        """The cyclic-operand warning is gated on verbose >= 1 (default off)."""
        processor = SimplePreprocessor(
            {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")},
            compiler_path="gcc",
            verbose=0,
        )
        processor._expand_object_macros_recursive_sz(sz.Str("A"))
        assert "WARNING" not in capsys.readouterr().out

    def test_no_compiler_evaluates_to_0(self):
        """With no compiler_path, __has_* calls should evaluate to 0 (backward compat)."""
        processor = SimplePreprocessor(self.macros, compiler_path="")

        result = processor._expand_has_functions_sz(sz.Str("__has_include(<iostream>)"))
        assert str(result) == "0"

    def test_not_a_function_call_left_unchanged(self):
        """Identifiers starting with __has_ but without parens should be left unchanged."""
        result = self.processor._expand_has_functions_sz(sz.Str("__has_value"))
        assert str(result) == "__has_value"

    def test_has_in_larger_identifier_left_unchanged(self):
        """__has_ as part of a larger identifier (preceded by alpha/underscore) left unchanged."""
        result = self.processor._expand_has_functions_sz(sz.Str("my__has_include(<x>)"))
        # 'my' prefix means it's part of another identifier
        assert "__has_include" in str(result)

    # Cycle 6: Integration through _evaluate_expression_sz()

    def test_evaluate_expression_with_has_include_and_defined(self):
        """__has_include and defined() should both work in a single expression."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = self.processor._evaluate_expression_sz(sz.Str("__has_include(<iostream>) && defined(TEST_MACRO)"))
            assert result == 1

    def test_evaluate_expression_has_include_false(self):
        """When __has_include is false, expression should evaluate to 0."""
        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            result = self.processor._evaluate_expression_sz(sz.Str("__has_include(<nonexistent.h>)"))
            assert result == 0

    # Cycle 7: End-to-end through process_structured()

    def test_process_structured_has_include_true(self):
        """#if __has_include(<iostream>) should include content when compiler says true."""
        text = dedent("""\
            #if __has_include(<iostream>)
            #include <special.h>
            #endif""")

        file_result = _make_file_analysis_result(text)

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            active_lines = self.processor.process_structured(file_result, self.ctx)
            # Line 1 (0-based) is "#include <special.h>" — should be active
            assert 1 in active_lines

    def test_process_structured_has_include_false(self):
        """#if __has_include(<nonexistent>) should exclude content when compiler says false."""
        text = dedent("""\
            #if __has_include(<nonexistent.h>)
            #include <special.h>
            #endif""")

        file_result = _make_file_analysis_result(text)

        with patch("compiletools.compiler_macros.query_has_function", return_value=0):
            active_lines = self.processor.process_structured(file_result, self.ctx)
            # Line 1 should NOT be active
            assert 1 not in active_lines

    # Cycle 8: Threading through get_or_compute_preprocessing()

    def test_get_or_compute_preprocessing_with_compiler(self):
        """get_or_compute_preprocessing should read compiler_path from MacroState."""
        text = "#if __has_include(<iostream>)\n#include <special.h>\n#endif"
        file_result = _make_file_analysis_result(text)

        core = {sz.Str("__GNUC__"): sz.Str("11")}
        macros = MacroState(core, compiler_path="gcc", cppflags="-I/usr/include", anchor_root="")

        with patch("compiletools.compiler_macros.query_has_function", return_value=1):
            result = get_or_compute_preprocessing(file_result, macros, verbose=0, context=self.ctx)
            assert 1 in result.active_lines

    def test_get_or_compute_preprocessing_without_compiler(self):
        """Without compiler_path on MacroState, __has_include should evaluate to 0."""
        text = "#if __has_include(<iostream>)\n#include <special.h>\n#endif"
        file_result = _make_file_analysis_result(text)

        core = {sz.Str("__GNUC__"): sz.Str("11")}
        macros = MacroState(core, anchor_root="")

        result = get_or_compute_preprocessing(file_result, macros, verbose=0, context=self.ctx)
        assert 1 not in result.active_lines

    # Cycle 9 (Finding A1): dead-branch #if/#elif must NOT evaluate the
    # controlling expression, because _evaluate_expression_sz issues a real
    # compiler probe (query_has_function) for __has_include / __has_*.

    def _spy_evaluate(self):
        """Wrap _evaluate_expression_sz to record every expression it sees."""
        seen = []
        real = self.processor._evaluate_expression_sz

        def spy(expr_sz):
            seen.append(str(expr_sz))
            return real(expr_sz)

        self.processor._evaluate_expression_sz = spy  # type: ignore[method-assign]
        return seen

    def test_dead_if_branch_does_not_evaluate_nested_if(self):
        """A #if nested inside a dead #if 0 must not evaluate its expression
        (no spurious __has_include compiler probe for an unreachable branch)."""
        text = dedent("""\
            #if 0
            #if __has_include(<should_not_be_probed.h>)
            #include <unreachable.h>
            #endif
            #endif""")
        file_result = _make_file_analysis_result(text)
        seen = self._spy_evaluate()

        # query_has_function would be the real side effect; assert it is never
        # called for the dead inner branch.
        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as probe:
            active_lines = self.processor.process_structured(file_result, self.ctx)

        assert probe.call_count == 0
        assert not any("should_not_be_probed" in e for e in seen)
        # The outer #if 0 is dead, so nothing inside is active.
        assert 2 not in active_lines

    def test_dead_elif_branch_does_not_evaluate_nested_if(self):
        """A #if nested inside a dead #elif branch must not evaluate its
        expression."""
        text = dedent("""\
            #if 1
            #include <taken.h>
            #elif 1
            #if __has_include(<should_not_be_probed.h>)
            #include <unreachable.h>
            #endif
            #endif""")
        file_result = _make_file_analysis_result(text)
        seen = self._spy_evaluate()

        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as probe:
            active_lines = self.processor.process_structured(file_result, self.ctx)

        assert probe.call_count == 0
        assert not any("should_not_be_probed" in e for e in seen)
        assert 1 in active_lines  # <taken.h>
        assert 4 not in active_lines  # <unreachable.h>

    def test_elif_inside_dead_outer_does_not_evaluate(self):
        """An #elif whose parent branch is dead must not evaluate its
        controlling expression either."""
        text = dedent("""\
            #if 0
            #if 0
            #include <a.h>
            #elif __has_include(<should_not_be_probed.h>)
            #include <b.h>
            #endif
            #endif""")
        file_result = _make_file_analysis_result(text)
        seen = self._spy_evaluate()

        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as probe:
            active_lines = self.processor.process_structured(file_result, self.ctx)

        assert probe.call_count == 0
        assert not any("should_not_be_probed" in e for e in seen)
        assert 2 not in active_lines
        assert 4 not in active_lines

    def test_dead_branch_skip_preserves_active_lines(self):
        """Skipping dead-branch eval must not change which lines are active for
        a representative battery of nested conditionals, elif chains, and
        else."""
        # Nested true inside true.
        text1 = dedent("""\
            #if 1
            #if 1
            #include <aa.h>
            #else
            #include <ab.h>
            #endif
            #else
            #include <b.h>
            #endif""")
        active = self.processor.process_structured(_make_file_analysis_result(text1), self.ctx)
        assert 2 in active and 4 not in active and 7 not in active

        # elif after a dead #if: the elif is the parent-active branch and must
        # still be evaluated and taken.
        self.processor = SimplePreprocessor(self.macros, verbose=0)
        text2 = dedent("""\
            #if 0
            #include <x.h>
            #elif 1
            #include <y.h>
            #else
            #include <z.h>
            #endif""")
        active = self.processor.process_structured(_make_file_analysis_result(text2), self.ctx)
        assert 1 not in active and 3 in active and 5 not in active

        # #else after a fully-dead chain becomes active.
        self.processor = SimplePreprocessor(self.macros, verbose=0)
        text3 = dedent("""\
            #if 0
            #include <x.h>
            #elif 0
            #include <y.h>
            #else
            #include <z.h>
            #endif""")
        active = self.processor.process_structured(_make_file_analysis_result(text3), self.ctx)
        assert 1 not in active and 3 not in active and 5 in active

    def test_live_nested_if_still_evaluates_has_probe_exactly_once(self):
        """N1 (A1 positive control): A1 must not OVER-suppress a LIVE branch.

        The A1 dead-branch tests only assert the probe is NOT called for
        unreachable branches. This complement proves a __has_include inside a
        LIVE nested #if (outer #if 1 active) IS still evaluated — exactly once,
        not zero times (over-suppressed) and not twice (re-evaluated)."""
        text = dedent("""\
            #if 1
            #if __has_include(<live_probe.h>)
            #include <found.h>
            #endif
            #endif""")
        file_result = _make_file_analysis_result(text)
        seen = self._spy_evaluate()

        with patch("compiletools.compiler_macros.query_has_function", return_value=1) as probe:
            active_lines = self.processor.process_structured(file_result, self.ctx)

        # The live inner #if must be evaluated exactly once (probe fired once).
        assert probe.call_count == 1
        assert any("live_probe" in e for e in seen)
        # Probe returned 1 -> the inner branch is active -> <found.h> is included.
        assert 2 in active_lines


class TestSimplePreprocessorEdgeCases:
    """Tests for uncovered edge cases in SimplePreprocessor."""

    def setup_method(self):
        self.ctx = BuildContext()
        self.macros = {
            sz.Str("DEFINED_MACRO"): sz.Str("1"),
            sz.Str("VERSION"): sz.Str("3"),
        }
        self.processor = SimplePreprocessor(self.macros, verbose=0)

    def test_unclosed_block_comment(self):
        """Unclosed /* comment should skip the rest of the expression."""
        result = self.processor._strip_comments_sz(sz.Str("1 + /* unclosed"))
        assert "unclosed" not in str(result)
        assert "1 +" in str(result)

    def test_defined_space_form(self):
        """'defined MACRO' (without parens) should work."""
        result = self.processor._expand_defined_sz(sz.Str("defined DEFINED_MACRO"))
        assert str(result) == "1"

        result = self.processor._expand_defined_sz(sz.Str("defined NONEXISTENT"))
        assert str(result) == "0"

    def test_defined_space_form_in_expression(self):
        """'defined MACRO' should evaluate correctly in full expression."""
        result = self.processor._evaluate_expression_sz(sz.Str("defined DEFINED_MACRO && 1"))
        assert result == 1

    def test_defined_as_part_of_identifier_prefix(self):
        """'defined' preceded by alpha should not be treated as keyword."""
        # 'predefined' contains 'defined' but shouldn't be treated as keyword
        result = self.processor._expand_defined_sz(sz.Str("predefined"))
        assert str(result) == "predefined"

    def test_defined_as_part_of_identifier_suffix(self):
        """'defined' followed by alpha (no space/paren) should not be treated as keyword."""
        result = self.processor._expand_defined_sz(sz.Str("definedX"))
        assert "definedX" in str(result)

    def test_defined_at_end_of_string(self):
        """'defined' at end with no macro after it."""
        result = self.processor._expand_defined_sz(sz.Str("defined"))
        assert "defined" in str(result)

    def test_defined_with_whitespace_only_after(self):
        """'defined  ' with only whitespace after."""
        result = self.processor._expand_defined_sz(sz.Str("defined   "))
        # Should not crash, keeps original text
        assert "defined" in str(result)

    def test_defined_unterminated_paren_not_rewritten(self):
        """A14: 'defined(MACRO' with no closing paren must NOT become '1(MACRO'/'0(MACRO'.

        The unterminated parenthesized form is unparseable; it must fall through
        to the "keep as is" path (leaving 'defined' intact) rather than emitting
        a corrupt rewrite. No crash, no infinite loop.
        """
        # Defined macro: must not become '1(DEFINED_MACRO'
        result = str(self.processor._expand_defined_sz(sz.Str("defined(DEFINED_MACRO")))
        assert result == "defined(DEFINED_MACRO"
        assert not result.startswith("1")
        assert "1(" not in result

        # Undefined macro: must not become '0(FOO'
        result = str(self.processor._expand_defined_sz(sz.Str("defined(FOO")))
        assert result == "defined(FOO"
        assert "0(" not in result

        # Trailing space, still no paren: must not become '0(FOO '
        result = str(self.processor._expand_defined_sz(sz.Str("defined(FOO ")))
        assert "0(" not in result and "1(" not in result
        assert "defined(FOO" in result

        # Malformed tail after a valid prefix: prefix preserved, no corrupt rewrite
        result = str(self.processor._expand_defined_sz(sz.Str("A || defined(FOO")))
        assert result.startswith("A || ")
        assert "0(" not in result and "1(" not in result
        assert "defined(FOO" in result

        # Paren immediately followed by end: already handled, keep intact
        result = str(self.processor._expand_defined_sz(sz.Str("defined(")))
        assert result == "defined("

    def test_defined_well_formed_paren_still_works(self):
        """A14 regression guard: the well-formed forms must be unaffected by the fix."""
        assert str(self.processor._expand_defined_sz(sz.Str("defined(DEFINED_MACRO)"))) == "1"
        assert str(self.processor._expand_defined_sz(sz.Str("defined(FOO)"))) == "0"
        assert str(self.processor._expand_defined_sz(sz.Str("defined DEFINED_MACRO"))) == "1"
        assert str(self.processor._expand_defined_sz(sz.Str("defined FOO"))) == "0"

    def test_non_ascii_byte_after_defined_space_form_does_not_crash(self):
        """B1: a non-ASCII byte where the operand of the SPACE form ``defined`` is
        expected must not crash the scan.

        After the ``defined`` keyword + whitespace skip, ``_expand_defined_sz``
        peeks the next byte to decide between the ``defined(MACRO)`` and
        ``defined MACRO`` forms. That peek formerly indexed the 1-byte
        ``sz.Str`` slice with a bare integer (``ch[0]``), which re-decodes the
        single raw byte as UTF-8 and raised ``UnicodeDecodeError`` on the lead
        byte of a multi-byte char (here the UTF-8 ``é``). The slice-compare
        idiom keeps the scan intact; the non-identifier byte means there is no
        parseable operand, so ``defined`` is passed through untouched.
        """
        # Must not raise. Best-effort result: 'defined' kept as-is (no operand).
        result = self.processor._expand_defined_sz(sz.Str("defined é"))
        assert "defined" in str(result)

    def test_non_ascii_byte_after_defined_paren_form_does_not_crash(self):
        """B1: a non-ASCII byte where the closing paren of the PAREN form is
        expected must not crash the scan.

        For ``defined(X é`` the scanner consumes ``(X`` then peeks for the
        closing ``)``. That peek formerly used the unsafe ``ch[0] == ")"`` form
        and raised ``UnicodeDecodeError`` on the UTF-8 ``é`` byte. With the
        slice-compare idiom the byte is correctly 'not a close paren', so the
        unterminated form falls through to the A14 keep-as-is path.
        """
        # Must not raise; unterminated paren degrades gracefully (A14 contract).
        result = self.processor._expand_defined_sz(sz.Str("defined(X é"))
        assert "defined" in str(result)
        # A14: unterminated paren must not become a corrupt '1('/'0(' rewrite.
        assert "1(" not in str(result) and "0(" not in str(result)

    def test_non_ascii_byte_in_defined_does_not_crash_end_to_end(self):
        """B1 end-to-end: a ``#if defined é`` line must not crash the scan.

        Mirrors the A9 end-to-end contract — a non-ASCII byte in the controlling
        expression must degrade the directive to false rather than propagating a
        ``UnicodeDecodeError`` out of ``_evaluate_expression_sz`` (where the
        directive-level ``except Exception`` would swallow it, silently dropping
        the whole conditional block).
        """
        # _evaluate_expression_sz must not raise on a non-ASCII defined operand.
        result = self.processor._evaluate_expression_sz(sz.Str("defined é"))
        assert result in (0, 1)

        # And the full structured scan over a #if defined é block must not crash.
        text = dedent("""\
            #if defined é
            #include <unreachable.h>
            #endif""")
        file_result = _make_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result, self.ctx)
        # Degenerate operand -> false -> body inactive; the contract is "no crash".
        assert 1 not in active_lines

    def test_defined_numeric_and_empty_operand_degrade_gracefully(self):
        """N3 (A14 regression lock): numeric / empty parenthesized operands must
        pass through untouched, never producing a corrupt ``1(``/``0(`` rewrite.

        ``defined(123)``, ``defined(1FOO)`` and ``defined()`` have no valid
        identifier operand (an identifier cannot start with a digit, and ``()``
        is empty). They must be kept verbatim rather than rewritten.
        """
        for probe in ("defined(123)", "defined(1FOO)", "defined()"):
            result = str(self.processor._expand_defined_sz(sz.Str(probe)))
            assert result == probe
            assert "1(" not in result and "0(" not in result

    def test_safe_eval_unsafe_expression(self):
        """_safe_eval should raise ValueError for unsafe expressions."""
        with pytest.raises(ValueError, match="Unsafe expression"):
            self.processor._safe_eval("__import__('os')")

    def test_safe_eval_failure_returns_0(self):
        """_safe_eval should return 0 when eval fails on a safe-looking expression."""
        # Expression that matches the regex but fails at eval time
        result = self.processor._safe_eval("1 2")
        assert result == 0

    def test_verbose_debug_output(self, capsys):
        """Verbose mode prints debug info for directive handling."""
        verbose_proc = SimplePreprocessor({sz.Str("X"): sz.Str("1")}, verbose=9)
        text = dedent("""
            #ifdef X
            line
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
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
        file_result = _make_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "#ifndef" in out

    def test_verbose_if_elif_else(self, capsys):
        """Verbose mode prints debug for #if, #elif, #else."""
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
        file_result = _make_file_analysis_result(text)
        verbose_proc.process_structured(file_result, self.ctx)
        out = capsys.readouterr().out
        assert "#if" in out
        assert "#elif" in out
        assert "#else" in out

    def test_if_evaluation_failure_assumes_false(self):
        """#if eval follows C semantics: an undefined identifier is the integer
        0 (so a bare undefined macro is false, but it still participates in
        arithmetic), while genuinely malformed/garbage expressions assume
        false."""

        processor = SimplePreprocessor({}, verbose=0)

        # (a) A valid expression whose only undefined identifier is replaced by
        #     0 -> the whole expression is false because 0 is false.
        text = dedent("""
            #if UNDEFINED_MACRO
            included
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 1 not in active_lines  # 'included' must not be active (0 -> false)

        # (a') Same undefined identifier, but now used in arithmetic: 0 + 1 == 1
        #      -> the branch IS active (this is the C-correct behavior the old
        #      "unparseable -> false" code got wrong by discarding the expr).
        text = dedent("""
            #if UNDEFINED_MACRO + 1
            included
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 1 in active_lines  # 'included' must be active (0 + 1 -> true)

        # (b) Genuinely malformed garbage (a stray, non-identifier,
        #     non-operator character) is still rejected -> false.
        text = dedent("""
            #if 1 @ 2
            included
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 1 not in active_lines  # garbage -> false

    def test_undefined_identifier_evaluates_to_zero_sz(self):
        """A2: in a controlling expression any surviving identifier is 0."""
        # A bare undefined identifier is false (== 0).
        assert self.processor._evaluate_expression_sz(sz.Str("UNDEFINED_MACRO")) == 0
        # ... but it still participates as the integer 0 in arithmetic/logic.
        assert self.processor._evaluate_expression_sz(sz.Str("UNDEFINED_MACRO + 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("UNDEFINED_MACRO == 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("!UNDEFINED_MACRO")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("FOO || BAR")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("FOO + BAR + 2")) == 2

    def test_garbage_expression_rejected_sz(self):
        """A2: a stray non-identifier, non-operator byte stays unsafe (false)."""
        # _safe_eval re-raises ValueError; #if/#elif callers turn that into
        # false. _evaluate_expression_sz propagates it for the same reason.
        for garbage in ("1 @ 2", "1 $ 2", "1 ` 2", "'a'"):
            with pytest.raises(ValueError):
                self.processor._evaluate_expression_sz(sz.Str(garbage))

    def test_short_circuit_logical_or_skips_dead_rhs_sz(self):
        """A6: ``1 || <anything>`` is 1 and must NOT evaluate the RHS, so a
        dead division-by-zero on the right side never surfaces."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 || UNDEF / 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 || 1 / 0")) == 1

    def test_short_circuit_logical_and_skips_dead_rhs_sz(self):
        """A6: ``0 && <anything>`` is 0 and must NOT evaluate the RHS."""
        assert self.processor._evaluate_expression_sz(sz.Str("0 && UNDEF / 0")) == 0
        assert self.processor._evaluate_expression_sz(sz.Str("0 && 1 / 0")) == 0

    def test_short_circuit_live_rhs_still_evaluated_sz(self):
        """A6: the RHS is still evaluated when the LHS does not short-circuit,
        and a LIVE division-by-zero still degrades to 0 (unchanged contract)."""
        assert self.processor._evaluate_expression_sz(sz.Str("0 || 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 && 1")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("1 && 0")) == 0
        # A live 1/0 in an evaluated position degrades to 0 (caught), so
        # ``0 || 1 / 0`` -> 0 || 0 -> 0.
        assert self.processor._evaluate_expression_sz(sz.Str("0 || 1 / 0")) == 0

    def test_short_circuit_dead_negative_shift_skipped_sz(self):
        """A6 follow-up: a dead-branch negative shift must not surface either.
        Python's ``<<``/``>>`` raise on a negative RHS and ``_safe_eval``
        re-raises ValueError, so the short-circuit must cover shift too."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 || (1 << -1)")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0 && (1 << -2)")) == 0
        # Dead shift in the untaken ternary branch as well.
        assert self.processor._evaluate_expression_sz(sz.Str("1 ? 1 : (1 >> -1)")) == 1

    def test_live_negative_shift_behavior_unchanged_sz(self):
        """A6 follow-up: only DEAD shifts are skipped. A LIVE negative shift is
        untouched -- it raises ValueError out of the parser exactly as before
        (``_safe_eval`` re-raises ValueError as the unsafe-expression signal)."""
        with pytest.raises(ValueError):
            self.processor._evaluate_expression_sz(sz.Str("1 << -1"))

    def test_ternary_conditional_operator_sz(self):
        """A7: ``?:`` is valid in a constant-expression; evaluate the taken
        branch only."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 ? 2 : 3")) == 2
        assert self.processor._evaluate_expression_sz(sz.Str("0 ? 2 : 3")) == 3
        assert self.processor._evaluate_expression_sz(sz.Str("1 ? 0 : 1")) == 0
        # Right-associative: 1 ? 0 : (1 ? 1 : 0) -> 0
        assert self.processor._evaluate_expression_sz(sz.Str("1 ? 0 : 1 ? 1 : 0")) == 0
        # 0 ? X : (1 ? 1 : 0) -> 1
        assert self.processor._evaluate_expression_sz(sz.Str("0 ? 9 : 1 ? 1 : 0")) == 1

    def test_ternary_short_circuits_untaken_branch_sz(self):
        """A7: only the taken branch is evaluated; a dead 1/0 must not surface."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 ? 1 : 1 / 0")) == 1
        assert self.processor._evaluate_expression_sz(sz.Str("0 ? 1 / 0 : 2")) == 2

    def test_ternary_precedence_below_logical_or_sz(self):
        """A7: ternary binds looser than ``||`` -> ``1 || 0 ? 2 : 3`` parses as
        ``(1 || 0) ? 2 : 3`` -> 2."""
        assert self.processor._evaluate_expression_sz(sz.Str("1 || 0 ? 2 : 3")) == 2
        assert self.processor._evaluate_expression_sz(sz.Str("0 || 0 ? 2 : 3")) == 3

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
        file_result = _make_file_analysis_result(text)
        active_lines = processor.process_structured(file_result, self.ctx)
        assert 3 not in active_lines  # 'b' should not be active
        assert 5 in active_lines  # 'c' should be active

    def test_if_no_condition(self):
        """#if with no condition should assume false."""
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
        processor = SimplePreprocessor({}, verbose=9)
        text = dedent("""
            #ifndef MY_HEADER_H
            #define MY_HEADER_H
            content
            #endif
        """).strip()
        file_result = _make_file_analysis_result(text)
        file_result.include_guard = sz.Str("MY_HEADER_H")
        processor.process_structured(file_result, self.ctx)
        assert sz.Str("MY_HEADER_H") not in processor.macros

    def test_unknown_directive_verbose(self, capsys):
        """Unknown directive with verbose >= 8 prints debug."""

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
        result = self.processor._strip_comments_sz(sz.Str("/* comment */ 42"))
        assert "42" in str(result)
        assert "comment" not in str(result)

    def test_multiple_block_comments(self):
        """Multiple block comments in one expression."""
        result = self.processor._strip_comments_sz(sz.Str("1 /* a */ + /* b */ 2"))
        assert str(result) == "1 + 2"

    def test_empty_block_comment_result(self):
        """Block comment that leaves nothing."""
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
        core = {}
        variable = {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("value"), sz.Str("BAZ"): sz.Str("0x100")}
        macros = MacroState(core, variable, anchor_root="")

        hash1 = macros.get_hash()
        hash2 = macros.get_hash()

        assert hash1 == hash2, "Same macro state should produce same hash"
        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"

    def test_hash_ordering_independence(self):
        """Verify hash is same regardless of insertion order."""
        core = {}
        # Create dicts with different insertion orders
        variable1 = {sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("2"), sz.Str("C"): sz.Str("3")}

        variable2 = {sz.Str("C"): sz.Str("3"), sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("2")}

        macros1 = MacroState(core, variable1, anchor_root="")
        macros2 = MacroState(core, variable2, anchor_root="")

        hash1 = macros1.get_hash()
        hash2 = macros2.get_hash()

        assert hash1 == hash2, "Hash should be independent of insertion order"

    def test_hash_sensitivity_to_changes(self):
        """Verify different macro states produce different hashes."""
        core = {}
        macros1 = MacroState(core, {sz.Str("FOO"): sz.Str("1")}, anchor_root="")
        macros2 = MacroState(core, {sz.Str("FOO"): sz.Str("2")}, anchor_root="")  # Different value
        macros3 = MacroState(core, {sz.Str("BAR"): sz.Str("1")}, anchor_root="")  # Different key
        macros4 = MacroState(
            core, {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("2")}, anchor_root=""
        )  # Additional key

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

        empty1 = MacroState({}, {}, anchor_root="")
        empty2 = MacroState({}, {}, anchor_root="")

        hash1 = empty1.get_hash()
        hash2 = empty2.get_hash()

        assert hash1 == hash2, "Empty macro states should have same hash"
        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"

    def test_hash_with_special_characters(self):
        """Verify hash handles special characters in macro values."""
        core = {}
        macros1 = MacroState(
            core,
            {sz.Str("PATH"): sz.Str("/usr/local/include"), sz.Str("FLAGS"): sz.Str("-O2 -g -Wall")},
            anchor_root="",
        )

        macros2 = MacroState(
            core,
            {
                sz.Str("PATH"): sz.Str("/usr/local/include"),
                sz.Str("FLAGS"): sz.Str("-O3 -g -Wall"),  # Different flag
            },
            anchor_root="",
        )

        hash1 = macros1.get_hash()
        hash2 = macros2.get_hash()

        assert isinstance(hash1, str), "Hash should be a hex string"
        assert len(hash1) == 16, "Hash should be 64-bit (16 hex chars)"
        assert hash1 != hash2, "Different values with special chars should have different hashes"

    def test_hash_cross_module_consistency(self):
        """Verify hash computation is consistent and accessible."""
        core = {}
        variable = {sz.Str("LINUX"): sz.Str("1"), sz.Str("DEBUG"): sz.Str("1"), sz.Str("VERSION"): sz.Str("100")}
        macros = MacroState(core, variable, anchor_root="")

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
        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="-O0", cxxflags="", anchor_root="")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="-O2", cxxflags="", anchor_root="")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_cxxflags(self):
        """Object hash must change when C++ standard changes."""
        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="", cxxflags="-std=c++17", anchor_root="")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="", cflags="", cxxflags="-std=c++20", anchor_root="")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_cppflags(self):
        """Object hash must change when include paths change (e.g., different library version)."""
        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", cppflags="-I/opt/libfoo/v1/include", anchor_root="")
        ms2 = MacroState(core, {}, compiler_path="g++", cppflags="-I/opt/libfoo/v2/include", anchor_root="")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_differs_with_different_compiler(self):
        """Object hash must change when compiler changes."""
        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(core, {}, compiler_path="g++", anchor_root="")
        ms2 = MacroState(core, {}, compiler_path="clang++", anchor_root="")
        assert ms1.get_hash(include_core=True) != ms2.get_hash(include_core=True)

    def test_macro_state_hash_without_core_ignores_build_context(self):
        """Preprocessing cache key (include_core=False) must NOT be affected by build flags."""
        core = {sz.Str("__GNUC__"): sz.Str("12")}
        ms1 = MacroState(
            core, {}, compiler_path="g++", cppflags="-I/a", cflags="-O0", cxxflags="-std=c++17", anchor_root=""
        )
        ms2 = MacroState(
            core, {}, compiler_path="clang++", cppflags="-I/b", cflags="-O2", cxxflags="-std=c++20", anchor_root=""
        )
        assert ms1.get_hash(include_core=False) == ms2.get_hash(include_core=False)

    def test_with_updates_propagates_build_context(self):
        """with_updates must carry cflags/cxxflags to the new MacroState."""
        core = {sz.Str("X"): sz.Str("1")}
        ms = MacroState(
            core, {}, compiler_path="g++", cppflags="-I/foo", cflags="-O2", cxxflags="-std=c++17", anchor_root=""
        )
        ms2 = ms.with_updates({sz.Str("Y"): sz.Str("2")})
        assert ms2.cflags == "-O2"
        assert ms2.cxxflags == "-std=c++17"
        assert ms2.compiler_path == "g++"
        assert ms2.cppflags == "-I/foo"

    def test_without_keys_propagates_build_context(self):
        """without_keys must carry cflags/cxxflags to the new MacroState."""
        core = {sz.Str("X"): sz.Str("1")}
        var = {sz.Str("Y"): sz.Str("2")}
        ms = MacroState(
            core, var, compiler_path="g++", cppflags="-I/foo", cflags="-O2", cxxflags="-std=c++17", anchor_root=""
        )
        ms2 = ms.without_keys([sz.Str("Y")])
        assert ms2.cflags == "-O2"
        assert ms2.cxxflags == "-std=c++17"


class TestResolveComputedInclude:
    """Tests for SimplePreprocessor.resolve_computed_include (A10).

    The wrapper-stripping must remove exactly ONE balanced outer (...) pair,
    not greedily peel every paren level. Greedy stripping corrupts computed
    includes whose stripped content legitimately contains inner parentheses.
    """

    def test_single_wrapper_resolves(self):
        """XSTR(FOO) -> FOO -> object-macro value. The simple, common case."""
        macros = {sz.Str("FOO"): sz.Str("linux_extra.h")}
        p = SimplePreprocessor(macros, verbose=0)
        assert p.resolve_computed_include("XSTR(FOO)") == "linux_extra.h"

    def test_quoted_include_returns_none(self):
        """A literal quoted include is not a computed include."""
        p = SimplePreprocessor({}, verbose=0)
        assert p.resolve_computed_include('"foo.h"') is None

    def test_angled_include_returns_none(self):
        """A literal angle-bracket include is not a computed include."""
        p = SimplePreprocessor({}, verbose=0)
        assert p.resolve_computed_include("<foo.h>") is None

    def test_empty_returns_none(self):
        """Empty / whitespace-only input is not a computed include."""
        p = SimplePreprocessor({}, verbose=0)
        assert p.resolve_computed_include("   ") is None

    def test_unresolvable_returns_none(self):
        """A wrapper around a non-macro identifier is unresolvable (expanded == inner)."""
        p = SimplePreprocessor({}, verbose=0)
        assert p.resolve_computed_include("XSTR(NOT_A_MACRO)") is None

    def test_balanced_strip_preserves_inner_parens(self):
        """XSTR(KEEP(name)) must strip ONLY the outer XSTR(...) wrapper.

        Greedy stripping peels both paren levels down to ``name``, silently
        discarding the inner ``KEEP(...)`` token structure and resolving to
        the bare macro value. The C-correct single-outer-pair strip leaves
        ``KEEP(name)`` intact, so after object-macro expansion of ``name``
        the result keeps the surrounding wrapper.
        """
        macros = {sz.Str("name"): sz.Str("config.h")}
        p = SimplePreprocessor(macros, verbose=0)
        # Balanced: XSTR(KEEP(name)) -> KEEP(name) -> KEEP(config.h)
        assert p.resolve_computed_include("XSTR(KEEP(name))") == "KEEP(config.h)"

    def test_unbalanced_trailing_paren_not_stripped(self):
        """JOIN(A,B)(C): the leading '(' is NOT balanced by the final ')'.

        Greedy stripping slices ``A,B)(C`` (syntactic garbage). A balanced
        strip must refuse to peel this layer because the '(' after the leading
        identifier does not enclose the entire remainder; the expression is
        passed through to expansion unchanged.
        """
        macros = {sz.Str("A"): sz.Str("aval")}
        p = SimplePreprocessor(macros, verbose=0)
        # No outer pair stripped; object-macro A expands in place.
        assert p.resolve_computed_include("JOIN(A,B)(C)") == "JOIN(aval,B)(C)"

    def test_malformed_unbalanced_does_not_crash(self):
        """An unbalanced wrapper must degrade gracefully (no exception)."""
        p = SimplePreprocessor({}, verbose=0)
        # Should not raise; returns None or a best-effort string.
        result = p.resolve_computed_include("XSTR((FOO)")
        assert result is None or isinstance(result, str)

    # N2: direct unit tests for the _strip_one_balanced_wrapper staticmethod.
    # Previously only exercised transitively through resolve_computed_include;
    # these pin its contract (return value verified against the live method).

    def test_strip_wrapper_empty_inner(self):
        """``()`` strips to the empty string: outer pair closes at the last char."""
        assert SimplePreprocessor._strip_one_balanced_wrapper("()") == ""

    def test_strip_wrapper_nested_keeps_inner(self):
        """``X((Y))`` strips ONLY the first balanced outer pair -> ``(Y)``."""
        assert SimplePreprocessor._strip_one_balanced_wrapper("X((Y))") == "(Y)"

    def test_strip_wrapper_leading_paren_not_wrapping_returns_none(self):
        """``(FOO)bar`` does not end in ')' -> None (fails the endswith guard)."""
        assert SimplePreprocessor._strip_one_balanced_wrapper("(FOO)bar") is None

    def test_strip_wrapper_two_separate_groups_returns_none(self):
        """``(A)(B)``: the first '(' is not balanced by the FINAL ')' -> None."""
        assert SimplePreprocessor._strip_one_balanced_wrapper("(A)(B)") is None

    def test_strip_wrapper_unbalanced_returns_none(self):
        """An unbalanced input (no closing ')') -> None."""
        assert SimplePreprocessor._strip_one_balanced_wrapper("(unbalanced") is None
