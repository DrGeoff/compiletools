"""Simple C preprocessor for handling conditional compilation directives."""

import contextlib
import re
import sys
from collections import Counter
from typing import TYPE_CHECKING, Any

import stringzilla as sz

from compiletools.stringzilla_utils import (
    concat_sz,
    is_alnum_or_underscore_sz,
    is_alpha_or_underscore_sz,
    strip_sz,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective


class PreprocessorExpressionError(Exception):
    """Raised for a genuine internal failure while evaluating a ``#if``/``#elif``
    controlling expression -- as opposed to a merely malformed/garbage user
    expression, which is handled gracefully as false.

    The distinction is load-bearing: the blanket ``except Exception: return 0``
    that used to guard expression evaluation could not tell a real evaluation
    failure (e.g. a ``RecursionError`` from a deeply-nested-but-VALID
    expression) apart from "the expression is false", so a true ``#if`` branch
    was silently dropped from compilation -- a silent-miscompile class bug.
    This exception is deliberately NOT swallowed by the false-degrading paths so
    such failures surface loudly instead of quietly excluding source.
    """


# Precompiled regex patterns for _safe_eval
_RE_BACKSLASH_WHITESPACE = re.compile(r"\\\s*")
_RE_MALFORMED_NUMBERS = re.compile(r"(\d+)\s*\(\s*(\d+)\s*\)")
# Strip C integer suffixes from a complete integer literal. The captured body
# must cover all four C literal forms, since the suffix letters differ from the
# literal's own digits:
#   - hex  0x.. / 0X..  (body digits 0-9A-Fa-f overlap the suffix letter 'L')
#   - bin  0b.. / 0B..
#   - oct/dec  bare digit run (C octal is a leading-0 digit run; normalized later)
# A naive ``(\d+)`` body only matches decimal, so ``0xFFUL`` was left unstripped
# (then rejected by the tokenizer) and ``0xFFu`` had no suffix removed.
#
# The suffix alternation is constrained to the VALID C forms only:
# ``U?(L|LL)?`` / ``(L|LL)U?`` — an optional unsigned ``u``/``U`` and an optional
# long ``l``/``L``/``ll``/``LL`` (matching case for the two-letter long), in
# either order, with at most one ``u``. A naive ``[LlUu]+`` would also strip
# *invalid* runs (``1UU``, ``1ULUL``, ``1LLL``, mixed-case ``1lL``/``1Ll``),
# silently treating them as the bare value; the constrained form leaves them
# untouched so the tokenizer rejects them (degrading the directive to 0) instead
# of accepting a malformed literal. The regex still only fires when a suffix is
# present, so a bare body is never matched.
# The ``(?![0-9A-Za-z_])`` lookahead (replacing ``\b``) ensures we strip a suffix
# only at a true literal boundary — ``0xF`` alone is never shortened to ``0x``
# because there is no U/L run after the hex body.
_RE_INTEGER_SUFFIXES = re.compile(
    r"(0[xX][0-9A-Fa-f]+|0[bB][01]+|\d+)(?:[uU](?:ll|LL|l|L)?|(?:ll|LL|l|L)[uU]?)(?![0-9A-Za-z_])"
)

# Precompiled regex patterns for _normalize_numeric_literals
_RE_HEX_LITERAL = re.compile(r"\b0[xX][0-9A-Fa-f]+\b")
_RE_BIN_LITERAL = re.compile(r"\b0[bB][01]+\b")
_RE_OCT_LITERAL = re.compile(r"\b0[0-7]+\b")

# Reserved words that should not be treated as macros
_RESERVED_WORDS = frozenset([sz.Str("and"), sz.Str("or"), sz.Str("not")])

# Byte-set fast-paths for ASCII identifier scanning in ``_expand_macros_sz``.
# Using ``sz.Str.find_first_of`` skips non-ASCII bytes (e.g. UTF-8 em-dash
# or emoji in a magic-flag value) in a single vectorized pass — a per-byte
# ``result[i]`` Python-side check would raise ``UnicodeDecodeError`` on the
# leading byte of any multi-byte sequence, since ``sz.Str.__getitem__``
# decodes each byte as UTF-8 in isolation.
_ID_START_BYTESET = "_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ID_CONT_BYTESET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"

# Dispatch table for preprocessor directives (performance optimization)
# Maps directive type to (handler_method_name, needs_directive_arg)
_DIRECTIVE_DISPATCH = {
    "define": ("_handle_define_structured", True),
    "undef": ("_handle_undef_structured", True),
    "ifdef": ("_handle_ifdef_structured", True),
    "ifndef": ("_handle_ifndef_structured", True),
    "if": ("_handle_if_structured", True),
    "elif": ("_handle_elif_structured", True),
    "else": ("_handle_else", False),
    "endif": ("_handle_endif", False),
}

# Global statistics for profiling
_stats: dict[str, Any] = {
    "call_count": 0,
    "files_processed": Counter(),
}


_CExprToken = int | str


def _split_macro_arguments(expr_sz: sz.Str, open_paren: int) -> tuple[list[sz.Str], int] | None:
    """Split a macro invocation's argument list starting at ``open_paren``.

    Returns ``(arguments, index_past_closing_paren)``, or None when the list is
    unterminated. Commas nested inside parentheses belong to the argument that
    contains them, so ``F(G(a, b), c)`` yields two arguments.
    """
    depth = 0
    args: list[sz.Str] = []
    arg_start = open_paren + 1
    pos = open_paren

    while pos < len(expr_sz):
        pos = expr_sz.find_first_of("(),", pos)
        if pos == -1:
            return None
        char = str(expr_sz[pos : pos + 1])
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append(expr_sz[arg_start:pos])
                return args, pos + 1
        elif depth == 1:
            args.append(expr_sz[arg_start:pos])
            arg_start = pos + 1
        pos += 1

    return None


def _replace_identifiers_sz(body_sz: sz.Str, replacements: dict[sz.Str, sz.Str]) -> sz.Str:
    """Replace whole-identifier occurrences of each key in ``body_sz``.

    Only complete identifiers are replaced, so a parameter named ``a`` does not
    rewrite the ``a`` inside ``max``.
    """
    result_parts: list[sz.Str] = []
    i = 0
    length = len(body_sz)

    while i < length:
        identifier_start = body_sz.find_first_of(_ID_START_BYTESET, i)
        if identifier_start == -1:
            result_parts.append(body_sz[i:])
            break
        if identifier_start > i:
            result_parts.append(body_sz[i:identifier_start])

        identifier_end = body_sz.find_first_not_of(_ID_CONT_BYTESET, identifier_start)
        if identifier_end == -1:
            identifier_end = length
        identifier = body_sz[identifier_start:identifier_end]
        result_parts.append(replacements.get(identifier, identifier))
        i = identifier_end

    return concat_sz(*result_parts) if result_parts else sz.Str("")


class _CExpressionParser:
    """Evaluate the integer subset used by C preprocessor expressions."""

    # Maximum recursive-descent depth. The parser is a precedence-climbing
    # recursive descent, so nested parentheses (and, to a lesser extent, nested
    # ternaries and stacked unary operators) drive it deeper. Each level of
    # parenthesis re-enters through both ``_parse_conditional`` and
    # ``_parse_unary``, so this bound of 300 permits ~150 levels of nesting --
    # orders of magnitude beyond any real-world ``#if`` (which rarely nests past
    # a handful) while capping actual stack usage well within a thread's C
    # stack. Exceeding it raises ``PreprocessorExpressionError`` loudly rather
    # than letting a ``RecursionError`` be silently degraded to a false ``#if``.
    _MAX_DEPTH = 300

    # Python frames consumed per unit of ``self._depth`` (the full precedence
    # chain is ~13 frames per parenthesis level, split across the two counted
    # re-entry points, i.e. ~7 frames per depth unit). 12 is a safe upper bound
    # used only to size the temporary recursion-limit headroom so the explicit
    # ``_MAX_DEPTH`` guard trips deterministically BEFORE CPython's own
    # ``RecursionError`` -- never a "blind" ``setrecursionlimit`` widening.
    _FRAMES_PER_DEPTH = 12
    _RECURSION_MARGIN = 128

    def __init__(self, tokens: list[_CExprToken]) -> None:
        self.tokens = tokens
        self.pos = 0
        self._depth = 0

    def parse(self) -> int:
        # Provision enough recursion headroom that the explicit ``_MAX_DEPTH``
        # guard is the thing that fires for a pathological expression, not
        # CPython's interpreter limit (whose swallowed RecursionError was the
        # original silent-false bug). The raise is bounded by ``_MAX_DEPTH`` and
        # restored unconditionally, so it cannot leak to unrelated code.
        old_limit = sys.getrecursionlimit()
        needed = old_limit + self._MAX_DEPTH * self._FRAMES_PER_DEPTH + self._RECURSION_MARGIN
        sys.setrecursionlimit(needed)
        try:
            value = self._parse_conditional(evaluate=True)
            if self._peek() != "EOF":
                raise SyntaxError("trailing tokens")
            return value
        except RecursionError as e:
            # Defence in depth: should be unreachable because the ``_MAX_DEPTH``
            # guard trips first, but if some other recursion path overflows,
            # surface it loudly rather than let the caller degrade it to false.
            raise PreprocessorExpressionError(f"expression too deeply nested to evaluate: {e}") from e
        finally:
            sys.setrecursionlimit(old_limit)

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > self._MAX_DEPTH:
            raise PreprocessorExpressionError(
                f"expression nesting exceeds the maximum supported depth of {self._MAX_DEPTH}"
            )

    # Short-circuit mechanism (A6/A7): every recursive method takes an
    # ``evaluate`` flag. When False the subtree is still fully *parsed* (so the
    # token stream stays aligned), but the dead arithmetic that could raise —
    # ``/`` and ``%`` (ZeroDivisionError) and ``<<``/``>>`` (negative shift
    # count) — is skipped and a placeholder 0 is returned. The result of a
    # non-evaluated subtree is discarded, so any placeholder is sound.
    # ``||`` clears it for the RHS when the LHS is already true; ``&&`` when the
    # LHS is already false; ``?:`` for the untaken branch.

    def _peek(self) -> _CExprToken:
        return self.tokens[self.pos]

    def _match(self, *ops: str) -> str | None:
        token = self._peek()
        if isinstance(token, str) and token in ops:
            self.pos += 1
            return token
        return None

    def _parse_conditional(self, evaluate: bool) -> int:
        # A7: conditional-expression := logical-or ( '?' expression ':'
        # conditional )?  — lower precedence than ``||``, right-associative.
        # Only the taken branch is evaluated; the untaken branch is parsed with
        # ``evaluate=False`` so its arithmetic never raises.
        # Depth is tracked here (the re-entry point for both parentheses, via
        # ``_parse_primary``, and ternaries) and decremented on exit so sibling
        # sub-expressions do not accumulate depth against the guard.
        self._enter()
        try:
            condition = self._parse_logical_or(evaluate)
            if not self._match("?"):
                return condition
            take_true = evaluate and condition != 0
            true_value = self._parse_conditional(evaluate=take_true)
            if not self._match(":"):
                raise SyntaxError("expected ':' in conditional expression")
            false_value = self._parse_conditional(evaluate=evaluate and condition == 0)
            return true_value if condition != 0 else false_value
        finally:
            self._depth -= 1

    def _parse_logical_or(self, evaluate: bool) -> int:
        value = self._parse_logical_and(evaluate)
        while self._match("||"):
            # A6: once the LHS is true the result is 1 and the RHS is dead.
            rhs_live = evaluate and value == 0
            rhs = self._parse_logical_and(rhs_live)
            value = 1 if value != 0 or rhs != 0 else 0
        return value

    def _parse_logical_and(self, evaluate: bool) -> int:
        value = self._parse_bitwise_or(evaluate)
        while self._match("&&"):
            # A6: once the LHS is false the result is 0 and the RHS is dead.
            rhs_live = evaluate and value != 0
            rhs = self._parse_bitwise_or(rhs_live)
            value = 1 if value != 0 and rhs != 0 else 0
        return value

    def _parse_bitwise_or(self, evaluate: bool) -> int:
        value = self._parse_bitwise_xor(evaluate)
        while self._match("|"):
            value |= self._parse_bitwise_xor(evaluate)
        return value

    def _parse_bitwise_xor(self, evaluate: bool) -> int:
        value = self._parse_bitwise_and(evaluate)
        while self._match("^"):
            value ^= self._parse_bitwise_and(evaluate)
        return value

    def _parse_bitwise_and(self, evaluate: bool) -> int:
        value = self._parse_equality(evaluate)
        while self._match("&"):
            value &= self._parse_equality(evaluate)
        return value

    def _parse_equality(self, evaluate: bool) -> int:
        value = self._parse_relational(evaluate)
        while op := self._match("==", "!="):
            rhs = self._parse_relational(evaluate)
            if op == "==":
                value = 1 if value == rhs else 0
            else:
                value = 1 if value != rhs else 0
        return value

    def _parse_relational(self, evaluate: bool) -> int:
        value = self._parse_shift(evaluate)
        while op := self._match("<", "<=", ">", ">="):
            rhs = self._parse_shift(evaluate)
            if op == "<":
                value = 1 if value < rhs else 0
            elif op == "<=":
                value = 1 if value <= rhs else 0
            elif op == ">":
                value = 1 if value > rhs else 0
            else:
                value = 1 if value >= rhs else 0
        return value

    def _parse_shift(self, evaluate: bool) -> int:
        value = self._parse_additive(evaluate)
        while op := self._match("<<", ">>"):
            rhs = self._parse_additive(evaluate)
            if not evaluate:
                # Dead subtree (A6/A7): skip the shift so a dead negative count
                # never raises ``ValueError``. The returned value is discarded.
                value = 0
            elif op == "<<":
                value <<= rhs
            else:
                value >>= rhs
        return value

    def _parse_additive(self, evaluate: bool) -> int:
        value = self._parse_multiplicative(evaluate)
        while op := self._match("+", "-"):
            rhs = self._parse_multiplicative(evaluate)
            if op == "+":
                value += rhs
            else:
                value -= rhs
        return value

    def _parse_multiplicative(self, evaluate: bool) -> int:
        value = self._parse_unary(evaluate)
        while op := self._match("*", "/", "%"):
            rhs = self._parse_unary(evaluate)
            if op == "*":
                value *= rhs
            elif not evaluate:
                # Dead subtree (A6/A7): skip the div/mod so a dead ``x / 0``
                # never raises. The returned value is discarded.
                value = 0
            elif op == "/":
                value = self._c_trunc_div(value, rhs)
            else:
                value = value - self._c_trunc_div(value, rhs) * rhs
        return value

    def _parse_unary(self, evaluate: bool) -> int:
        # Track depth here too: stacked prefix operators (``!!!...``) recurse
        # through ``_parse_unary`` without passing ``_parse_conditional``, so
        # this is the other unbounded-recursion vector.
        self._enter()
        try:
            if self._match("+"):
                return +self._parse_unary(evaluate)
            if self._match("-"):
                return -self._parse_unary(evaluate)
            if self._match("!"):
                return 0 if self._parse_unary(evaluate) else 1
            if self._match("~"):
                return ~self._parse_unary(evaluate)
            return self._parse_primary(evaluate)
        finally:
            self._depth -= 1

    def _parse_primary(self, evaluate: bool) -> int:
        token = self._peek()
        if isinstance(token, int):
            self.pos += 1
            return token
        if self._match("("):
            value = self._parse_conditional(evaluate)
            if not self._match(")"):
                raise SyntaxError("missing closing parenthesis")
            return value
        raise SyntaxError("expected integer or parenthesized expression")

    @staticmethod
    def _c_trunc_div(lhs: int, rhs: int) -> int:
        if rhs == 0:
            raise ZeroDivisionError("integer division by zero")
        quotient = abs(lhs) // abs(rhs)
        return -quotient if (lhs < 0) != (rhs < 0) else quotient


class SimplePreprocessor:
    """A simple C preprocessor for handling conditional compilation directives.

    Capabilities:
    - Handles #if/#elif/#else/#endif, #ifdef/#ifndef, #define/#undef
    - Understands defined(MACRO) and defined MACRO forms
    - Supports C-style numeric literals: hex (0x), binary (0b), octal (0...)
    - Evaluates logical (&&, ||, ! and and/or/not), comparison, bitwise (&, |, ^, ~) and shift (<<, >>) operators
    - Strips // and /* ... */ comments from expressions in directives
    - Respects inactive branches (directives only alter state when active)
    - Provides recursive macro expansion helper for advanced use
    """

    def __init__(
        self,
        defined_macros: dict[sz.Str, sz.Str],
        verbose: int = 0,
        compiler_path: str = "",
        cppflags: str = "",
        function_params: dict[sz.Str, tuple[sz.Str, ...]] | None = None,
    ) -> None:
        # Caller must provide dict with sz.Str keys and values - no type conversion needed
        self.macros = defined_macros.copy()
        # Parameter lists of the function-like macros in self.macros, keyed by the
        # same bare name. A name present here is function-like: it expands only
        # when followed by an argument list, never on its own (C11 6.10.3p10).
        self.function_params: dict[sz.Str, tuple[sz.Str, ...]] = dict(function_params) if function_params else {}
        self.verbose = verbose
        self.compiler_path = compiler_path
        self.cppflags = cppflags
        # Diagnostics for the current controlling expression: the text handed to
        # the evaluator after macro expansion, and any expansion the engine
        # declined to perform. Both are reported when the expression turns out
        # to be unevaluable, so the user sees why a branch was assumed false.
        self._expansion_notes: list[str] = []
        self._last_expanded: str = ""
        # Rebound to the BuildContext's set in process_structured; the instance
        # set only serves a preprocessor driven directly, without a context.
        self._warned_conditions: set[tuple[str, str, str]] = set()
        # Same rebinding for the deferred-report store. Deferral is only armed
        # when the context reports an enclosing convergence; otherwise a
        # diagnostic is printed the moment it is found.
        self._pending_warnings: dict[tuple[str, str, str], str] = {}
        self._resolved_conditions: set[tuple[str, str, str]] = set()
        self._defer_warnings: bool = False
        self._current_filepath: str = "<unknown>"
        # Per-call channel from process_structured to _handle_define_structured:
        # which include guard macro to skip when re-#define'd. Kept as instance
        # state rather than a param because _DIRECTIVE_DISPATCH calls every
        # handler with the fixed (directive, condition_stack) signature. Reset at
        # the top of each process_structured call.
        self._include_guard = None

    def _strip_comments_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Strip C/C++ style comments from StringZilla expressions."""
        from compiletools.stringzilla_utils import strip_sz

        # Strip C++ style line comments
        comment_pos = expr_sz.find("//")
        if comment_pos >= 0:
            expr_sz = expr_sz[:comment_pos]
            expr_sz = strip_sz(expr_sz)

        # Strip C-style block comments using StringZilla operations
        start_pos = expr_sz.find("/*")
        if start_pos >= 0:
            # Build list of non-comment regions
            regions = []
            pos = 0

            while True:
                start_pos = expr_sz.find("/*", pos)
                if start_pos < 0:
                    # No more comments, add remaining text
                    if pos < len(expr_sz):
                        regions.append(expr_sz[pos:])
                    break

                # Add text before comment
                if start_pos > pos:
                    regions.append(expr_sz[pos:start_pos])

                # Find end of comment
                end_pos = expr_sz.find("*/", start_pos + 2)
                if end_pos < 0:
                    # Unclosed comment - skip rest
                    break

                # Add space where comment was
                regions.append(sz.Str(" "))
                pos = end_pos + 2

            # Join regions efficiently using concat_sz
            from compiletools.stringzilla_utils import concat_sz

            expr_sz = concat_sz(*regions) if regions else sz.Str("")

            # Normalize whitespace: convert to str, normalize, convert back
            # (for tiny expressions this is acceptable and simpler than vectorization)
            if len(expr_sz) > 0:
                parts = str(expr_sz).split()
                if parts:
                    expr_sz = sz.Str(" ".join(parts))
                else:
                    expr_sz = sz.Str("")

        return expr_sz

    def _evaluate_expression_sz(self, expr_sz: sz.Str) -> int:
        """Evaluate a StringZilla expression using native StringZilla operations"""
        self._expansion_notes = []
        # Strip comments FIRST (faster - avoids expanding macros inside comments)
        stripped_sz = self._strip_comments_sz(expr_sz)
        # Then expand macros
        expanded_sz = self._recursive_expand_macros_sz(stripped_sz)
        # For now, convert final expression to str for safe_eval, but this could be optimized
        expr_str = str(expanded_sz)
        self._last_expanded = expr_str
        result = self._safe_eval(expr_str)
        if self._expansion_notes:
            # An expansion the engine declined leaves source text the tokenizer
            # then reads as a bare 0, which can still parse to a plausible-
            # looking number. Refuse the whole expression instead: the caller
            # turns this into the unevaluable-condition report.
            raise ValueError("; ".join(self._expansion_notes))
        return result

    def _expand_defined_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Expand defined(MACRO) expressions using StringZilla operations"""

        result_parts = []
        i = 0

        while i < len(expr_sz):
            # Look for 'defined'
            defined_pos = expr_sz.find("defined", i)
            if defined_pos == -1:
                # No more 'defined' occurrences
                result_parts.append(expr_sz[i:])
                break

            # Add text before 'defined'
            if defined_pos > i:
                result_parts.append(expr_sz[i:defined_pos])

            # Check if this is actually 'defined' keyword (not part of identifier)
            if defined_pos > 0 and is_alnum_or_underscore_sz(expr_sz, defined_pos - 1):
                # Part of another identifier
                result_parts.append(expr_sz[defined_pos : defined_pos + 7])
                i = defined_pos + 7
                continue

            after_defined = defined_pos + 7  # len('defined')
            if after_defined < len(expr_sz) and is_alnum_or_underscore_sz(expr_sz, after_defined):
                # Part of longer identifier
                result_parts.append(expr_sz[defined_pos:after_defined])
                i = after_defined
                continue

            # Skip whitespace after 'defined' - vectorized
            j = expr_sz.find_first_not_of(" \t", after_defined)
            if j == -1:
                j = len(expr_sz)

            if j >= len(expr_sz):
                result_parts.append(expr_sz[defined_pos:])
                break

            # Check for parenthesized form: defined(MACRO)
            macro_name = None
            end_pos = j

            ch = expr_sz[j : j + 1]
            if ch == "(":
                # Find macro name inside parens
                j += 1
                # Skip whitespace - vectorized
                j = expr_sz.find_first_not_of(" \t", j)
                if j == -1:
                    j = len(expr_sz)

                # Extract macro name - vectorized
                if j < len(expr_sz) and is_alpha_or_underscore_sz(expr_sz, j):
                    macro_start = j
                    # Find end of identifier (alphanumeric + underscore)
                    identifier_end = expr_sz.find_first_not_of(
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", macro_start
                    )
                    j = identifier_end if identifier_end != -1 else len(expr_sz)
                    macro_name = expr_sz[macro_start:j]

                    # Skip whitespace before closing paren - vectorized
                    next_non_ws = expr_sz.find_first_not_of(" \t", j)
                    j = next_non_ws if next_non_ws != -1 else len(expr_sz)

                    # Check for closing paren. An unterminated parenthesized
                    # form (no ')') is unparseable: clear macro_name so it falls
                    # through to the "keep as is" branch below rather than being
                    # rewritten to a corrupt '1(MACRO' / '0(MACRO'.
                    closed = False
                    if j < len(expr_sz):
                        ch = expr_sz[j : j + 1]
                        if ch == ")":
                            end_pos = j + 1
                            closed = True
                    if not closed:
                        macro_name = None
            else:
                # Space form: defined MACRO - vectorized
                if is_alpha_or_underscore_sz(expr_sz, j):
                    macro_start = j
                    # Find end of identifier (alphanumeric + underscore)
                    identifier_end = expr_sz.find_first_not_of(
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", macro_start
                    )
                    j = identifier_end if identifier_end != -1 else len(expr_sz)
                    macro_name = expr_sz[macro_start:j]
                    end_pos = j

            # Replace with 1 or 0
            if macro_name:
                result_parts.append(sz.Str("1") if macro_name in self.macros else sz.Str("0"))
                i = end_pos
            else:
                # Couldn't parse, keep as is
                result_parts.append(expr_sz[defined_pos:after_defined])
                i = after_defined

        from compiletools.stringzilla_utils import concat_sz

        return concat_sz(*result_parts) if result_parts else sz.Str("")

    def _expand_has_functions_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Expand __has_* function calls by querying the compiler.

        Handles __has_include(<header>), __has_include("header"),
        __has_builtin(__builtin_x), __has_feature(cxx_rvalue_references), etc.

        If no compiler_path is set, all __has_* calls evaluate to 0.

        Probe suppression is BRANCH-level only. This expansion runs eagerly,
        before the expression parser, so the compiler probe (query_has_function)
        fires here regardless of the parser's later boolean short-circuit
        (evaluate=False on the dead side of &&/||/?:). A __has_* in a dead
        SUB-expression of a LIVE controlling expression is therefore still
        probed (e.g. ``0 && __has_include(<x.h>)`` probes <x.h>); only a __has_*
        inside a wholly dead #if/#elif BRANCH is skipped (its controlling
        expression is never expanded). Result-correct either way — the parser
        discards the unused probe result — so this is documented, not refactored
        to lazy thunks.
        """
        from compiletools.compiler_macros import query_has_function
        from compiletools.stringzilla_utils import concat_sz, strip_sz

        result_parts = []
        i = 0

        while i < len(expr_sz):
            # Look for '__has_'
            has_pos = expr_sz.find("__has_", i)
            if has_pos == -1:
                result_parts.append(expr_sz[i:])
                break

            # Add text before '__has_'
            if has_pos > i:
                result_parts.append(expr_sz[i:has_pos])

            # Check if this is actually a standalone identifier start (not part of a larger identifier)
            if has_pos > 0 and is_alnum_or_underscore_sz(expr_sz, has_pos - 1):
                # Part of another identifier
                result_parts.append(expr_sz[has_pos : has_pos + 6])
                i = has_pos + 6
                continue

            # Find end of the function name (alphanumeric + underscore)
            name_end = expr_sz.find_first_not_of(
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", has_pos
            )
            if name_end == -1:
                name_end = len(expr_sz)

            func_name = expr_sz[has_pos:name_end]

            # Skip whitespace after function name
            j = expr_sz.find_first_not_of(" \t", name_end)
            if j == -1:
                j = len(expr_sz)

            # Must have opening paren to be a function call. Slice-index
            # (``[j:j+1]``) rather than bare-int-index: ``sz.Str[j]`` decodes a
            # single byte as UTF-8 and raises UnicodeDecodeError on the leading
            # byte of any multi-byte char (see module-level _ID_*_BYTESET note);
            # the slice yields a 1-char Str that compares correctly to "(".
            if j >= len(expr_sz) or expr_sz[j : j + 1] != "(":
                # Not a function call - leave unchanged
                result_parts.append(func_name)
                i = name_end
                continue

            # Find matching closing paren, handling nested parens and angle brackets
            paren_depth = 1
            k = j + 1
            while k < len(expr_sz) and paren_depth > 0:
                # Slice-index (not bare ``expr_sz[k]``): a bare-int index decodes
                # one byte as UTF-8 and raises UnicodeDecodeError when the operand
                # contains a multi-byte char (e.g. <föö.h>). The 1-char Str slice
                # is safe and compares correctly to "(" / ")".
                ch = expr_sz[k : k + 1]
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
                k += 1

            if paren_depth != 0:
                # Unmatched parens - leave unchanged
                result_parts.append(expr_sz[has_pos:k])
                i = k
                continue

            # Extract the full function call: __has_include(<iostream>)
            # A18: the bare pp-tokens operand form is subject to macro expansion
            # (e.g.
            #   #define HEADER <foo.h>
            #   #if __has_include(HEADER)
            # must probe __has_include(<foo.h>), not the literal token HEADER).
            # The compiler probe runs on a fresh stdin TU that knows nothing of
            # our #defines, so we expand the operand ourselves here. Only
            # object-macro expansion is applied — defined() was already consumed
            # by _expand_defined_sz before this method ran, and nested __has_*
            # operands are not valid C, so neither needs re-running. Expansion
            # is iterated to a fixed point so chained macros (HEADER -> NAME ->
            # <foo.h>) fully resolve before the probe.
            #
            # A1 / C23 6.10.1: the *header-name* operand form — a literal
            # "foo.h" or <foo.h> — must NOT be macro-expanded; only the bare
            # pp-tokens form is. So with `#define foo bar`,
            # __has_include("foo.h") probes "foo.h" (not "bar.h") and
            # __has_include(<foo.h>) probes <foo.h> (not <bar.h>). We detect the
            # header-name form by its leading " or < (after stripping leading
            # whitespace) and skip expansion for it.
            operand = expr_sz[j + 1 : k - 1]
            stripped_operand = strip_sz(operand)
            # Slice-index ([0:1]) not bare [0]: a bare int index decodes one
            # byte as UTF-8 and raises on a multi-byte leading byte.
            first_char = stripped_operand[0:1] if len(stripped_operand) > 0 else sz.Str("")
            if first_char == '"' or first_char == "<":
                # Header-name form: literal, not macro-expanded.
                expanded_operand = operand
            else:
                expanded_operand = self._expand_object_macros_recursive_sz(operand)
            call_str = str(expr_sz[has_pos : j + 1]) + str(expanded_operand) + ")"

            if not self.compiler_path:
                # No compiler available - evaluate to 0
                result_parts.append(sz.Str("0"))
            else:
                value = query_has_function(self.compiler_path, call_str, self.cppflags, self.verbose)
                result_parts.append(sz.Str(str(value)))

            i = k

        return concat_sz(*result_parts) if result_parts else sz.Str("")

    def _expand_macros_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Replace macro names with their values using StringZilla operations"""
        # First handle defined() expressions to avoid expanding macros inside them
        result = self._expand_defined_sz(expr_sz)
        # Then expand __has_* function calls by querying the compiler (each
        # call's operand is object-macro-expanded inside _expand_has_functions_sz)
        result = self._expand_has_functions_sz(result)
        # Then expand function-like macro invocations. This runs BEFORE the
        # object pass so an argument list is still attached to its macro name.
        result = self._expand_function_macros_sz(result)
        # Finally expand object-like macros in the remaining expression body
        return self._expand_object_macros_sz(result)

    def _expand_function_macros_sz(self, expr_sz: sz.Str, active: frozenset = frozenset()) -> sz.Str:
        """Expand ``NAME(arg, ...)`` invocations of function-like macros.

        Arguments are substituted positionally into the macro body by token
        text, exactly as the C preprocessor does — no parentheses are added
        around an argument, so ``#define TWICE(x) x*2`` expands ``TWICE(1+1)``
        to ``1+1*2`` (3), matching cpp rather than the intuitive 4.

        ``active`` carries the names currently being expanded so a macro cannot
        expand itself (C11 6.10.3.4p2, the "blue paint" rule); a self-recursive
        definition terminates instead of iterating to the expansion cap.

        Unsupported constructs (``...``/``__VA_ARGS__``, an argument-count
        mismatch, an unterminated argument list) are left in the expression
        verbatim with a note recorded in ``self._expansion_notes``. The
        surviving text is then rejected by the tokenizer, so the condition is
        reported as unevaluable rather than silently evaluating to something
        else.
        """
        if not self.function_params:
            return expr_sz

        result_parts: list[sz.Str] = []
        i = 0
        length = len(expr_sz)

        while i < length:
            identifier_start = expr_sz.find_first_of(_ID_START_BYTESET, i)
            if identifier_start == -1:
                result_parts.append(expr_sz[i:])
                break
            if identifier_start > i:
                result_parts.append(expr_sz[i:identifier_start])

            identifier_end = expr_sz.find_first_not_of(_ID_CONT_BYTESET, identifier_start)
            if identifier_end == -1:
                identifier_end = length
            name = expr_sz[identifier_start:identifier_end]

            params = self.function_params.get(name)
            open_paren = expr_sz.find_first_not_of(" \t", identifier_end) if identifier_end < length else -1
            has_arguments = open_paren != -1 and str(expr_sz[open_paren : open_paren + 1]) == "("
            if params is not None and has_arguments and name in active:
                # Blue paint (C11 6.10.3.4p2) stops the recursion, but the
                # invocation it leaves behind is not a value cpp would accept
                # in an #if -- gcc calls it "missing binary operator".
                self._note(f"recursive invocation of {name} cannot be expanded in #if")
            if params is None or not has_arguments or name in active:
                result_parts.append(name)
                i = identifier_end
                continue

            call = _split_macro_arguments(expr_sz, open_paren)
            if call is None:
                self._note(f"unterminated argument list for function-like macro {name}")
                result_parts.append(expr_sz[identifier_start:])
                i = length
                continue

            args, call_end = call
            expansion = self._substitute_macro_arguments(name, params or (), args, active)
            if expansion is None:
                result_parts.append(expr_sz[identifier_start:call_end])
            else:
                result_parts.append(expansion)
            i = call_end

        return concat_sz(*result_parts) if result_parts else sz.Str("")

    def _substitute_macro_arguments(
        self, name: sz.Str, params: tuple[sz.Str, ...], args: list[sz.Str], active: frozenset
    ) -> sz.Str | None:
        """Substitute ``args`` for ``params`` in the body of ``name``.

        Returns the expanded body, or None when the invocation cannot be
        expanded (the caller then leaves the source text in place).
        """
        if any(str(p) == "..." for p in params):
            self._note(f"variadic macro {name} is not supported in #if")
            return None

        # ``F()`` with a zero-parameter macro is a call with no arguments, not
        # a call with one empty argument.
        if len(params) == 0 and len(args) == 1 and len(strip_sz(args[0])) == 0:
            args = []

        if len(args) != len(params):
            self._note(f"macro {name} takes {len(params)} argument(s) but was invoked with {len(args)}")
            return None

        body = self.macros.get(name, sz.Str(""))
        if len(params) == 0:
            substituted = body
        else:
            # Argument text is macro-expanded before substitution (C11
            # 6.10.3.1p1). The object pass runs on the outer fixed-point
            # iteration; this handles a function-like call inside an argument.
            replacements = {p: self._expand_function_macros_sz(strip_sz(a), active) for p, a in zip(params, args)}
            substituted = _replace_identifiers_sz(body, replacements)

        return self._expand_function_macros_sz(substituted, active | {name})

    def _note(self, message: str) -> None:
        """Record why an expansion was declined, for the unevaluable-#if report."""
        if message not in self._expansion_notes:
            self._expansion_notes.append(message)

    def _expand_object_macros_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Replace object-like macro identifiers with their bodies (single pass).

        Pure object-macro substitution only — does NOT touch defined() or
        __has_* calls. Used both for the general expression body (after
        defined()/__has_* have been consumed) and, in isolation, to expand a
        __has_* operand before the has-check (A18).
        """
        result = expr_sz
        # Start from the beginning and find identifier patterns
        i = 0
        result_len = len(result)

        while i < result_len:
            # Vectorized hop to the next ASCII identifier-start byte. This
            # also robustly skips any non-ASCII bytes (e.g. UTF-8 multi-byte
            # sequences from an em-dash or emoji embedded in a macro value),
            # which cannot begin a C/C++ identifier.
            identifier_start = result.find_first_of(_ID_START_BYTESET, i)
            if identifier_start == -1:
                break

            # Find the end of the identifier - vectorized
            identifier_end = result.find_first_not_of(_ID_CONT_BYTESET, identifier_start)
            i = identifier_end if identifier_end != -1 else result_len

            # Extract the identifier
            identifier = result[identifier_start:i]

            # Skip reserved words
            if identifier in _RESERVED_WORDS:
                continue

            # A function-like macro name is only expanded when followed by an
            # argument list (C11 6.10.3p10); on its own it stays an identifier,
            # which _tokenize_c_expression then reads as 0. Any invocation has
            # already been consumed by _expand_function_macros_sz.
            if identifier in self.function_params:
                continue

            # Check if it's a macro and replace it
            if identifier in self.macros:
                value = self.macros[identifier]
                # Replace in the result string
                before = result[:identifier_start]
                after = result[i:]
                result = before + value + after
                # Adjust position and length to account for replacement
                i = identifier_start + len(value)
                result_len = len(result)

        return result

    def _expand_object_macros_recursive_sz(self, expr_sz: sz.Str, max_iterations: int = 10) -> sz.Str:
        """Iterate object-macro expansion to a fixed point (A18 operand expansion).

        Like _recursive_expand_macros_sz but restricted to object-macro
        substitution — it deliberately does NOT re-run defined()/__has_*
        handling, so it is safe to call on a __has_* operand. ``max_iterations``
        caps depth to defeat cyclic ``#define`` pairs, mirroring the cap in
        _recursive_expand_macros_sz. When the cap is hit AND the operand is
        still changing on each pass — a genuine cycle rather than benign
        convergence — a warning is emitted at ``verbose >= 1`` (matching the
        sibling), so a half-expanded token being handed to the __has_* probe is
        not silently truncated. The last seen expression is returned regardless.
        """
        previous_expr = sz.Str("")
        iteration = 0
        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_object_macros_sz(expr_sz)
            iteration += 1

        # If we hit the iteration cap AND the operand was still mutating on the
        # last pass, the expansion was truncated — warn the user (mirrors
        # _recursive_expand_macros_sz, scoped to the __has_* operand context).
        if iteration == max_iterations and expr_sz != previous_expr:
            if self.verbose >= 1:
                print(
                    f"WARNING: SimplePreprocessor._expand_object_macros_recursive_sz hit "
                    f"max_iterations={max_iterations} while a __has_* operand was still "
                    f"changing — likely a recursive macro definition cycle. "
                    f"Truncated result: {expr_sz!r} (previous: {previous_expr!r})"
                )

        return expr_sz

    def _recursive_expand_macros_sz(self, expr_sz: sz.Str, max_iterations: int = 10) -> sz.Str:
        """Recursively expand macros using StringZilla operations until no more changes occur.

        ``max_iterations`` (default 10) caps the expansion depth to defeat
        pathological macro definitions that would otherwise loop forever
        (e.g. ``#define A B`` and ``#define B A``). When the cap is hit
        AND the expression is still changing on each pass — indicating a
        genuine cycle rather than benign convergence — a warning is emitted
        at ``verbose >= 1`` so the user knows their macro definitions are
        cyclic and the result was truncated. The last seen expression is
        returned regardless.
        """
        previous_expr = sz.Str("")  # Initialize with empty StringZilla.Str instead of None
        iteration = 0

        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_macros_sz(expr_sz)
            iteration += 1

        # If we hit the iteration cap AND the expression was still mutating
        # on the last pass, the expansion was truncated — warn the user.
        if iteration == max_iterations and expr_sz != previous_expr:
            if self.verbose >= 1:
                print(
                    f"WARNING: SimplePreprocessor._recursive_expand_macros_sz hit "
                    f"max_iterations={max_iterations} while expression was still "
                    f"changing — likely a recursive macro definition cycle. "
                    f"Truncated result: {expr_sz!r} (previous: {previous_expr!r})"
                )

        return expr_sz

    def resolve_computed_include(self, include_arg: "sz.Str | str") -> str | None:
        """Resolve a computed #include expression using current macro state.

        For #include XSTR(COMPILETIME_INCLUDE_FILE) where
        COMPILETIME_INCLUDE_FILE is defined as linux_extra.h,
        strips function-like wrappers and expands macros to return "linux_extra.h".

        Returns resolved filename string, or None if not a computed include
        or if unresolvable.
        """
        arg = str(include_arg).strip()
        if not arg or arg[0] in ('"', "<"):
            return None

        # Strip the function-like macro wrapper: XSTR(FOO) -> FOO. Peel only a
        # single *balanced* outer ``(...)`` pair — the first '(' must be matched
        # by the final ')' enclosing the entire remainder. A greedy
        # ``inner.index("(")`` + ``[:-1]`` slice that repeats every level would
        # over-peel inner parens belonging to the stripped content (e.g.
        # ``XSTR(KEEP(name))`` -> ``name`` instead of ``KEEP(name)``, or
        # ``JOIN(A,B)(C)`` -> the syntactic garbage ``A,B)(C``). Anything left
        # inside (a further wrapper, a nested function-like call) is handed to
        # macro expansion rather than blindly discarded.
        inner = self._strip_one_balanced_wrapper(arg)
        if inner is None:
            inner = arg

        expanded = str(self._recursive_expand_macros_sz(sz.Str(inner)))
        return expanded if expanded != inner else None

    @staticmethod
    def _strip_one_balanced_wrapper(expr: str) -> str | None:
        """Remove exactly one outer ``(...)`` pair if it balances the whole tail.

        Returns the de-wrapped, stripped inner expression when ``expr`` ends in
        ``)`` and the first ``(`` is balanced by that final ``)`` (so the pair
        encloses the entire remainder after the leading text). Returns ``None``
        when there is no such balanced outer pair — including unbalanced input —
        so the caller leaves the expression untouched for macro expansion.
        """
        if not expr.endswith(")"):
            return None
        open_idx = expr.find("(")
        if open_idx == -1:
            return None

        depth = 0
        for i in range(open_idx, len(expr)):
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    # The first '(' closes at index i. It is a true outer
                    # wrapper only if it closes at the very last character.
                    if i == len(expr) - 1:
                        return expr[open_idx + 1 : -1].strip()
                    return None
        # Ran off the end with depth > 0: unbalanced, leave it alone.
        return None

    def process_structured(self, file_result: "FileAnalysisResult", context) -> list[int]:
        """Process FileAnalysisResult and return active line numbers using structured directive data.

        Args:
            file_result: FileAnalysisResult with structured directive information
            context: BuildContext for the current build session

        Returns:
            List of line numbers (0-based) that are active after conditional compilation
        """
        # Lookup filepath from content hash for logging
        from compiletools.global_hash_registry import get_filepath_by_hash

        filepath = get_filepath_by_hash(file_result.content_hash, context)
        self._current_filepath = filepath or "<unknown>"
        # getattr, not attribute access: callers are free to pass a duck-typed
        # context carrying only the caches they need (test_preprocessing_cache_scoping
        # does), and a diagnostic must not be the thing that breaks them.
        self._warned_conditions = getattr(context, "warned_preprocessor_conditions", self._warned_conditions)
        self._pending_warnings = getattr(context, "pending_preprocessor_warnings", self._pending_warnings)
        self._resolved_conditions = getattr(context, "resolved_preprocessor_conditions", self._resolved_conditions)
        self._defer_warnings = getattr(context, "preprocessor_convergence_depth", 0) > 0

        # Store include guard so _handle_define_structured can skip it
        # Include guards should not be added to macro state as they only prevent
        # re-inclusion and don't affect which includes are active
        self._include_guard = file_result.include_guard

        # Track statistics
        _stats["call_count"] += 1
        _stats["files_processed"][filepath] += 1

        line_count = file_result.line_count
        active_lines = []

        # Stack to track conditional compilation state
        # Each entry: (is_active, seen_else, any_condition_met)
        condition_stack = [(True, False, False)]

        # Convert directive_by_line to a sorted list for processing in order
        directive_lines = sorted(file_result.directive_by_line.keys())
        directive_iter = iter(directive_lines)
        next_directive_line = next(directive_iter, None)

        i = 0
        while i < line_count:
            # Check if current line has a directive
            if i == next_directive_line:
                directive = file_result.directive_by_line[i]

                # Handle multiline directives - skip continuation lines
                continuation_lines = directive.continuation_lines

                # Handle the directive
                handled = self._handle_directive_structured(directive, condition_stack, i + 1)

                # Include #define and #undef lines in active_lines even when handled (for macro extraction)
                # Also include unhandled directives (like #include) if in active context
                if condition_stack[-1][0]:
                    if directive.directive_type in ("define", "undef") or handled is False:
                        active_lines.append(i)
                        # Add continuation lines too
                        active_lines.extend(i + j + 1 for j in range(continuation_lines) if i + j + 1 < line_count)

                # Skip the continuation lines we've already processed
                i += continuation_lines + 1
                next_directive_line = next(directive_iter, None)
            else:
                # Regular line - include if we're in an active context
                if condition_stack[-1][0]:
                    active_lines.append(i)
                i += 1

        return active_lines

    def _handle_directive_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]], line_num: int
    ) -> bool:
        """Handle a specific preprocessor directive using structured data"""
        dispatch_info = _DIRECTIVE_DISPATCH.get(directive.directive_type)
        if dispatch_info:
            handler_name, needs_directive = dispatch_info
            handler = getattr(self, handler_name)
            if needs_directive:
                handler(directive, condition_stack)
            else:
                handler(condition_stack)
            return True
        # Unknown directive - ignore but don't consume the line
        # This allows #include and other directives to be processed normally
        if self.verbose >= 8:
            print(f"SimplePreprocessor: Ignoring unknown directive #{directive.directive_type}")
        return False

    def _handle_else(self, condition_stack: list[tuple[bool, bool, bool]]) -> None:
        """Handle #else directive"""
        if len(condition_stack) <= 1:
            return

        _, seen_else, any_condition_met = condition_stack.pop()
        if not seen_else:
            parent_active = condition_stack[-1][0]
            new_active = not any_condition_met and parent_active
            condition_stack.append((new_active, True, any_condition_met or new_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #else -> {new_active}")
        else:
            condition_stack.append((False, True, any_condition_met))

    def _handle_endif(self, condition_stack: list[tuple[bool, bool, bool]]) -> None:
        """Handle #endif directive"""
        if len(condition_stack) > 1:
            condition_stack.pop()
            if self.verbose >= 9:
                print("SimplePreprocessor: #endif")

    def _handle_define_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #define directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context

        if directive.macro_name:
            # Skip include guard - it only prevents re-inclusion and should not
            # pollute the macro state used for cache keys
            if self._include_guard is not None and directive.macro_name == self._include_guard:
                if self.verbose >= 9:
                    print(f"SimplePreprocessor: skipping include guard {directive.macro_name}")
                return

            # A bodyless object-like macro stands for 1 in a controlling
            # expression; a bodyless function-like macro expands to nothing.
            if directive.macro_value is not None:
                macro_value = directive.macro_value
            else:
                macro_value = sz.Str("") if directive.macro_params is not None else sz.Str("1")
            self.macros[directive.macro_name] = macro_value
            if directive.macro_params is not None:
                self.function_params[directive.macro_name] = tuple(directive.macro_params)
            else:
                # A redefinition can turn a function-like macro object-like.
                self.function_params.pop(directive.macro_name, None)
            if self.verbose >= 9:
                print(f"SimplePreprocessor: defined macro {directive.macro_name} = {macro_value}")

    def _handle_undef_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #undef directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context

        if directive.macro_name and directive.macro_name in self.macros:
            del self.macros[directive.macro_name]
            self.function_params.pop(directive.macro_name, None)
            if self.verbose >= 9:
                print(f"SimplePreprocessor: undefined macro {directive.macro_name}")

    def _handle_ifdef_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #ifdef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = is_defined and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifdef {directive.macro_name} -> {is_defined}")

    def _handle_ifndef_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #ifndef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = (not is_defined) and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifndef {directive.macro_name} -> {not is_defined}")

    def _handle_if_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #if directive using structured data"""
        if not condition_stack[-1][0]:
            # Parent branch is already dead, so this #if can never be taken.
            # Skip evaluating the controlling expression to avoid spurious
            # __has_include / __has_* compiler probes (an observable side effect
            # of _evaluate_expression_sz). Still push an inactive frame so
            # nesting depth and bookkeeping stay correct. This is the exact frame
            # the old code pushed when result was AND-ed with a False parent.
            condition_stack.append((False, False, False))
        elif directive.condition:
            try:
                # Pass the raw condition: _evaluate_expression_sz strips comments
                # internally as its first step, so no handler-level strip is needed.
                result = self._evaluate_expression_sz(directive.condition)
                self._note_condition_resolved(directive)
                is_active = bool(result) and condition_stack[-1][0]
                condition_stack.append((is_active, False, is_active))
                if self.verbose >= 9:
                    print(f"SimplePreprocessor: #if {directive.condition} -> {result} ({is_active})")
            except PreprocessorExpressionError:
                # A genuine internal evaluation failure (e.g. an over-deep but
                # VALID expression) must NOT be degraded to a false branch --
                # that would silently drop true source. Surface it loudly.
                raise
            except Exception as e:
                # A malformed/garbage user expression: assume false.
                self._warn_unevaluable_condition(directive, e)
                condition_stack.append((False, False, False))
        else:
            # No condition provided
            condition_stack.append((False, False, False))

    def _handle_elif_structured(
        self, directive: "PreprocessorDirective", condition_stack: list[tuple[bool, bool, bool]]
    ) -> None:
        """Handle #elif directive using structured data"""
        if len(condition_stack) <= 1:
            return

        _, seen_else, any_condition_met = condition_stack.pop()
        parent_active = condition_stack[-1][0]
        if not parent_active and not seen_else and not any_condition_met and directive.condition:
            # Parent branch is dead, so this #elif can never be taken. Skip the
            # eval to avoid spurious __has_include / __has_* compiler probes.
            # Behavior-preserving: new_active = bool(result) and parent_active is
            # necessarily False, hence new_any_condition_met == any_condition_met.
            condition_stack.append((False, False, any_condition_met))
        elif not seen_else and not any_condition_met and directive.condition:
            try:
                # Pass the raw condition: _evaluate_expression_sz strips comments
                # internally as its first step, so no handler-level strip is needed.
                result = self._evaluate_expression_sz(directive.condition)
                self._note_condition_resolved(directive)
                new_active = bool(result) and parent_active
                new_any_condition_met = any_condition_met or new_active
                condition_stack.append((new_active, False, new_any_condition_met))
                if self.verbose >= 9:
                    print(f"SimplePreprocessor: #elif {directive.condition} -> {result} ({new_active})")
            except PreprocessorExpressionError:
                # A genuine internal evaluation failure must NOT be degraded to
                # a false branch (see the matching note in the #if handler).
                raise
            except Exception as e:
                self._warn_unevaluable_condition(directive, e)
                condition_stack.append((False, False, any_condition_met))
        else:
            # Either we already found a true condition or seen_else is True
            condition_stack.append((False, seen_else, any_condition_met))

    def _warn_unevaluable_condition(self, directive: "PreprocessorDirective", error: Exception) -> None:
        """Report a controlling expression the evaluator could not represent.

        An unevaluable condition is indistinguishable in the output from a
        legitimately false one, which is what let a whole class of wrong-branch
        bugs pass unnoticed (a function-like macro guard, an expression outside
        the supported subset, a valid expression the evaluator rejects). It is
        therefore reported at ``verbose >= 1`` on stderr, in one grep-able line,
        rather than at the verbose-8 level that only a debugging session sees.
        The branch is still assumed false: this changes what the user is told,
        not what is built.

        Repeats of the same condition in one file are reported once per build
        session, not once per preprocessor instance: magicflags evaluates the
        same file under a fresh instance on every pass.

        Inside a magicflags convergence the report is recorded rather than
        printed. An early pass evaluates a file before the macros that control
        it have been discovered, so a condition unevaluable there is routinely
        resolved by a later pass; printing from that pass produces
        "assuming false" on a build whose final answer is the true branch.
        ``_note_condition_resolved`` retracts the record when that happens and
        ``flush_pending_warnings`` reports whatever is still unevaluable once
        the macro state has settled.
        """
        if self.verbose < 1:
            return

        condition = str(directive.condition)
        seen_key = (self._current_filepath, directive.directive_type, condition)
        if seen_key in self._warned_conditions:
            return

        detail = ""
        if self._last_expanded and self._last_expanded != condition:
            detail = f"; expanded to '{self._last_expanded}'"
        if self._expansion_notes:
            detail += "; " + "; ".join(self._expansion_notes)

        message = (
            f"SimplePreprocessor warning: {self._current_filepath}:{directive.line_num + 1}: "
            f"cannot evaluate '#{directive.directive_type} {condition}' ({error})"
            f"{detail} - assuming false"
        )

        if self._defer_warnings:
            if seen_key not in self._resolved_conditions:
                self._pending_warnings.setdefault(seen_key, message)
            return

        self._warned_conditions.add(seen_key)
        print(message, file=sys.stderr)

    def _note_condition_resolved(self, directive: "PreprocessorDirective") -> None:
        """Record that this condition is evaluable, and drop any pending report.

        Marking is what makes the retraction stick. Passes are not ordered
        best-last: a convergence can evaluate a file correctly and then process
        it again from a state that cannot (a cache replay that strands a
        function-like macro's parameter list, say), and that later pass would
        re-record a condition already shown to be evaluable.
        """
        if not self._defer_warnings:
            return
        key = (self._current_filepath, directive.directive_type, str(directive.condition))
        self._resolved_conditions.add(key)
        self._pending_warnings.pop(key, None)

    def _safe_eval(self, expr: str) -> int:
        """Safely evaluate a numeric expression"""
        # Clean up the expression
        expr = expr.strip()

        # Remove trailing backslashes from multiline directives and normalize whitespace
        # Remove backslashes followed by whitespace (multiline continuations)
        expr = _RE_BACKSLASH_WHITESPACE.sub(" ", expr)
        # Remove any remaining trailing backslashes
        expr = expr.rstrip("\\").strip()

        # First clean up any malformed expressions from macro replacement
        # Fix cases like "0(0)" which occur when macros expand to adjacent numbers
        expr = _RE_MALFORMED_NUMBERS.sub(r"\1 * \2", expr)

        # Remove C-style integer suffixes (L, UL, LL, ULL, etc.)
        expr = _RE_INTEGER_SUFFIXES.sub(r"\1", expr)

        # Normalize C-style numeric literals to Python ints (hex, bin, octal)
        expr = self._normalize_numeric_literals(expr)

        try:
            tokens = self._tokenize_c_expression(expr)
            return _CExpressionParser(tokens).parse()
        except ValueError:
            # ValueError from _tokenize_c_expression signals an unsafe
            # expression (unrecognized identifier or non-arithmetic character).
            # The legacy contract surfaces it to the #if/#elif caller so the
            # verbose-8 log identifies which directive failed; genuinely
            # malformed expressions (SyntaxError, ZeroDivisionError) degrade to
            # 0 silently below.
            raise
        except (SyntaxError, ZeroDivisionError) as e:
            # A genuinely malformed / garbage user expression: degrade to 0
            # (documented behaviour). This is DELIBERATELY narrow -- it must not
            # catch PreprocessorExpressionError (a real internal failure such as
            # an over-deep-but-valid expression) or any other unexpected
            # exception (a programming bug), because degrading those to 0 would
            # silently drop a possibly-true #if branch from compilation.
            if self.verbose >= 8:
                print(f"SimplePreprocessor: Expression evaluation failed for '{expr}': {e}")
            return 0

    @staticmethod
    def _tokenize_c_expression(expr: str) -> list[_CExprToken]:
        """Tokenize the safe integer expression subset accepted by _safe_eval."""
        tokens: list[_CExprToken] = []
        i = 0
        multi_ops = ("&&", "||", "<<", ">>", "==", "!=", "<=", ">=")
        # ``?`` and ``:`` are the ternary conditional-operator tokens (A7).
        single_ops = set("()+-*/%<>!&|^~?:")
        word_ops = {"and": "&&", "or": "||", "not": "!"}

        while i < len(expr):
            ch = expr[i]
            if ch.isspace():
                i += 1
                continue

            if ch.isdigit():
                start = i
                i += 1
                while i < len(expr) and expr[i].isdigit():
                    i += 1
                tokens.append(int(expr[start:i]))
                continue

            matched_op = None
            for op in multi_ops:
                if expr.startswith(op, i):
                    matched_op = op
                    break
            if matched_op is not None:
                tokens.append(matched_op)
                i += len(matched_op)
                continue

            if ch in single_ops:
                tokens.append(ch)
                i += 1
                continue

            if ch.isalpha() or ch == "_":
                start = i
                i += 1
                while i < len(expr) and (expr[i].isalnum() or expr[i] == "_"):
                    i += 1
                word = expr[start:i]
                if word in word_ops:
                    tokens.append(word_ops[word])
                    continue
                # A2: C preprocessor semantics — any identifier surviving macro
                # expansion in a controlling expression is replaced by the
                # integer 0. (Genuinely unsafe input is a *non*-identifier byte,
                # handled by the catch-all below.) Emitting 0 instead of raising
                # lets the parser run, which is what makes the A6 short-circuit
                # reachable (a raise here would discard the whole expression).
                tokens.append(0)
                continue

            # A stray byte that is neither part of a number, operator, nor a
            # ``[A-Za-z_][A-Za-z0-9_]*`` identifier (e.g. ``@``, ``$``, a
            # backtick, or a quote) is genuine garbage — reject it so the
            # #if/#elif caller assumes false.
            raise ValueError(f"Unsafe expression: {expr}")

        tokens.append("EOF")
        return tokens

    def _normalize_numeric_literals(self, expr: str) -> str:
        """Convert C-style numeric literals (hex, bin, oct) to decimal strings.

        - 0x... or 0X... -> decimal
        - 0b... or 0B... -> decimal
        - 0... (octal) -> decimal, but leave single '0' as is and ignore 0x/0b prefixes
        """

        def repl_hex(m: re.Match[str]) -> str:
            return str(int(m.group(0), 16))

        def repl_bin(m: re.Match[str]) -> str:
            return str(int(m.group(0), 2))

        def repl_oct(m: re.Match[str]) -> str:
            s = m.group(0)
            # avoid replacing just '0'
            if s == "0":
                return s
            return str(int(s, 8))

        # Replace hex first
        expr = _RE_HEX_LITERAL.sub(repl_hex, expr)
        # Replace binary
        expr = _RE_BIN_LITERAL.sub(repl_bin, expr)
        # Replace octal: leading 0 followed by one or more octal digits, not 0x/0b already handled
        expr = _RE_OCT_LITERAL.sub(repl_oct, expr)
        return expr


def flush_pending_warnings(context: Any) -> int:
    """Report the conditions still unevaluable now the macro state has settled.

    Returns the number of lines printed. Entries already reported are skipped
    and the store is emptied either way, so a second flush over the same
    context prints nothing.
    """
    pending = getattr(context, "pending_preprocessor_warnings", None)
    if not pending:
        return 0

    reported = getattr(context, "warned_preprocessor_conditions", None)
    printed = 0
    for key, message in pending.items():
        if reported is not None:
            if key in reported:
                continue
            reported.add(key)
        print(message, file=sys.stderr)
        printed += 1

    pending.clear()
    resolved = getattr(context, "resolved_preprocessor_conditions", None)
    if resolved is not None:
        resolved.clear()
    return printed


@contextlib.contextmanager
def converging(context: Any) -> "Iterator[None]":
    """Defer unevaluable-condition reports for the duration of a convergence.

    magicflags evaluates a file repeatedly, and an early pass runs before the
    macros controlling that file have been discovered, so its verdict is not
    the build's answer. Reports raised inside this region are held and only
    printed on exit, minus any condition a later pass managed to evaluate.

    Re-entrant: only the outermost region flushes. Consumers that drive the
    preprocessor without a convergence never enter it and keep reporting
    immediately.
    """
    depth = getattr(context, "preprocessor_convergence_depth", None)
    if depth is None:
        yield
        return

    context.preprocessor_convergence_depth = depth + 1
    try:
        yield
    finally:
        context.preprocessor_convergence_depth -= 1
        if context.preprocessor_convergence_depth == 0:
            flush_pending_warnings(context)


def print_preprocessor_stats() -> None:
    """Print SimplePreprocessor call statistics only."""
    print("\n=== SimplePreprocessor Call Statistics ===")
    print(f"Total process_structured calls: {_stats['call_count']}")
    print("\nTop 20 most processed files:")
    for filepath, count in _stats["files_processed"].most_common(20):
        print(f"  {count:6d}x  {filepath}")
