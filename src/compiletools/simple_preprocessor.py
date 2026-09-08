"""Simple C preprocessor for handling conditional compilation directives."""

import contextlib
import operator
import re
import sys
from collections import Counter
from collections.abc import Callable
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

    The distinction is load-bearing: a real evaluation failure (e.g. a
    ``RecursionError`` from a deeply-nested-but-VALID expression) must not
    degrade to "the expression is false", which silently drops a true ``#if``
    branch from compilation -- a silent-miscompile class bug. This exception is
    deliberately NOT swallowed by the false-degrading paths so such failures
    surface loudly instead of quietly excluding source.
    """


class UnsafeExpressionError(ValueError):
    """The tokenizer's deliberate "this is not an arithmetic expression" signal.

    Raised only by ``_tokenize_c_expression``, for a byte that is neither part
    of a number, an operator, nor a ``[A-Za-z_][A-Za-z0-9_]*`` identifier -- or
    for a pp-number that is not a valid integer constant (an invalid octal
    digit as in ``08``, an invalid suffix run as in ``1UU``, or a value too
    large even for ``uintmax_t``). The
    ``#if``/``#elif`` handlers turn it into an unevaluable-condition report and
    assume false.

    It is a distinct type because a bare ``ValueError`` cannot carry that
    meaning: Python raises ``ValueError`` from ordinary integer arithmetic too
    (a negative shift count), so a catch on the bare type cannot tell
    dangerous input apart from a well-formed ``#if (1 >> (~0))`` whose branch
    must not be dropped. A ``ValueError`` subclass so callers catching the
    base type still catch it.
    """


class MacroVerdictConflictError(RuntimeError):
    """Two targets' settled walks disagree about one ``#if`` occurrence.

    Raised by :func:`check_verdict_conflicts` when a condition some target's
    walk could not evaluate (and assumed false) resolves TRUE under another
    target's settled macro state. The two verdicts both feed the final
    product — the targets' objects are compiled under contradictory flags —
    so this is a product inconsistency, not a diagnostic nicety. The message
    is complete end-user prose including remedies, rendered as-is by cake's
    generic fatal-error path.
    """


# (filepath, directive_type, line_num, expansion_kind, expression) -- one
# expansion occurrence. See VerdictPartition.pending_expansions.
ExpansionKey = tuple[str, str, int, str, str]

# Condition-occurrence kinds, as recorded in condition_occurrences and
# replayed by preprocessing_cache._replay_condition_occurrences. The two
# resolved kinds carry the truth value because conflict classification
# distinguishes a gate another target resolved TRUE (the products genuinely
# diverge) from one resolved FALSE (the assume-false merely coincides).
CONDITION_RESOLVED_TRUE = "resolved_true"
CONDITION_RESOLVED_FALSE = "resolved_false"
CONDITION_UNREACHED = "unreached"
CONDITION_UNEVALUABLE = "unevaluable"

# The two expansion helpers, as they appear in the ``kind`` slot of an
# expansion key and of a recorded occurrence.
EXPANSION_KIND = "expansion"
HAS_OPERAND_EXPANSION_KIND = "has_operand_expansion"
EXPANSION_OCCURRENCE_KINDS = frozenset({EXPANSION_KIND, HAS_OPERAND_EXPANSION_KIND})

# Stands in for the directive an expansion belongs to when there is none.
_NO_EXPANSION_SITE = ("<none>", -1)

# Precompiled regex patterns for _safe_eval
_RE_BACKSLASH_WHITESPACE = re.compile(r"\\\s*")
_RE_MALFORMED_NUMBERS = re.compile(r"(\d+)\s*\(\s*(\d+)\s*\)")
# Strip-and-forget is gone: integer suffixes are no longer removed by a regex
# pre-pass (that threw away the U bit, making ``-1 > 0u`` a signed comparison)
# and numeric literals are no longer rewritten to decimal text (which silently
# accepted ``08`` as 8). Instead one ``_RE_INT_LITERAL`` match per digit run
# classifies the whole literal -- body, valid suffix, and boundary -- and
# ``_literal`` decides the value's signedness. Only the two text cleanups
# below survive as pre-passes.
_RE_INT_LITERAL = re.compile(
    r"(0[xX][0-9A-Fa-f]+|0[bB][01]+|[0-9]+)"
    r"((?:[uU](?:ll|LL|l|L)?|(?:ll|LL|l|L)[uU]?)?)"
    r"(?![0-9A-Za-z_])"
)

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


# A value token carries its C signedness alongside the int: True = the
# constant is unsigned (uintmax_t), False = signed (intmax_t). Operators keep
# the pair together so the usual arithmetic conversions can act on it.
_CValue = tuple[int, bool]
_CExprToken = _CValue | str


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


# Width of the type C evaluates ``#if`` arithmetic in (``intmax_t``). Every
# arithmetic result is truncated to this width (``_wrap``); the constant is
# used directly by the left-shift count clamp in
# ``_CExpressionParser._c_shift`` -- a count at or past the width shifts every
# bit out and yields 0, before any wrapping happens.
_INTMAX_PRECISION = 64

# The uintmax_t mask and the intmax_t maximum. ``_wrap`` and ``_literal`` keep
# every value inside their range, which is the invariant that lets the
# parser's plain Python ``<``, ``//`` and ``>>`` produce the C answers once
# both operands of an operator are converted.
_UMASK = (1 << 64) - 1
_SMAX = (1 << 63) - 1


def _wrap(value: int, unsigned: bool) -> int:
    """Truncate an arithmetic result to intmax_t/uintmax_t width (C11 6.10.1p4).

    ``#if`` arithmetic is evaluated in intmax_t (signed) or uintmax_t
    (unsigned). A signed result that left that range wraps two's-complement --
    libcpp pedwarns "integer overflow in preprocessor expression" and uses the
    wrapped value, so ``9223372036854775807 + 1 < 0`` keeps the block. An
    unsigned result wraps mod 2**64 (defined, silent). Keeping every signed
    value inside [-2**63, 2**63) is also the invariant that makes Python's own
    comparisons and shifts produce the C answers once both operands are
    converted.
    """
    value &= _UMASK
    return value if unsigned or value <= _SMAX else value - (1 << 64)


def _literal(body: str, suffix: str) -> _CValue:
    """Classify one integer constant: parse the value and fix its signedness.

    Implements C11 6.4.4.1p5 the way libcpp does. An explicit ``u``/``U``
    suffix makes the constant unsigned; without one, a constant that does not
    fit intmax_t but fits uintmax_t is unsigned -- silently for a
    hex/octal/binary constant, with a pedwarn from the compiler for a decimal
    one (the value is the same either way, so the diagnostic is not
    reproduced). ``l``/``ll`` change nothing: inside ``#if`` every signed type
    evaluates as intmax_t (C11 6.10.1p4).

    A pp-number that is not a valid integer constant raises
    UnsafeExpressionError: an invalid digit in an octal constant (``08``), or
    a value too large even for uintmax_t -- there gcc truncates mod 2**64 with
    a warning while clang hard-errors, so there is no common C answer to
    emulate and warned-false is the honest outcome.
    """
    if body[:2] in ("0x", "0X"):
        value = int(body, 16)
    elif body[:2] in ("0b", "0B"):
        value = int(body, 2)
    elif len(body) > 1 and body[0] == "0":
        if "8" in body or "9" in body:
            raise UnsafeExpressionError(f"invalid digit in octal constant: {body}")
        value = int(body, 8)
    else:
        value = int(body)
    if value > _UMASK:
        raise UnsafeExpressionError(f"integer constant is too large for its type: {body}{suffix}")
    return value, ("u" in suffix or "U" in suffix) or value > _SMAX


def _c_trunc_div(lhs: int, rhs: int) -> int:
    """C truncating division (round toward zero), unlike Python's floor ``//``."""
    if rhs == 0:
        raise ZeroDivisionError("integer division by zero")
    quotient = abs(lhs) // abs(rhs)
    return -quotient if (lhs < 0) != (rhs < 0) else quotient


# Binary-operator dispatch for ``_CExpressionParser._apply_binary``. Both
# tables receive operands ALREADY converted (C11 6.3.1.8): when either side is
# unsigned both have been masked to uintmax_t, so plain Python comparison and
# arithmetic give the C answer. Comparisons yield a signed int 0/1 and never
# wrap; arithmetic results are wrapped to the converted width by the caller.
# Shifts are absent by design -- they are exempt from the conversions and
# handled in ``_parse_shift``. ``/`` and ``%`` mirror libcpp's ``num_div_op``:
# INTMAX_MIN / -1 wraps back to INTMAX_MIN (it warns, then uses the wrapped
# value) and the remainder is defined from the truncated quotient.
_COMPARISON_OPS: dict[str, Callable[[int, int], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}
_ARITHMETIC_OPS: dict[str, Callable[[int, int], int]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "&": operator.and_,
    "^": operator.xor,
    "|": operator.or_,
    "/": _c_trunc_div,
    "%": lambda lv, rv: lv - _c_trunc_div(lv, rv) * rv,
}
# The operators that can raise ZeroDivisionError and so must be skipped in a
# dead subtree (A6/A7) rather than evaluated and discarded.
_DIVISION_OPS = frozenset(("/", "%"))


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
            return value[0]
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
    # token stream stays aligned), but the dead arithmetic that could raise
    # (``/`` and ``%``, ZeroDivisionError) or cost real work (``<<``/``>>`` with
    # a large count) is skipped and a placeholder 0 is returned. The result of a
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

    def _parse_conditional(self, evaluate: bool) -> _CValue:
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
            take_true = evaluate and condition[0] != 0
            true_value = self._parse_conditional(evaluate=take_true)
            if not self._match(":"):
                raise SyntaxError("expected ':' in conditional expression")
            false_value = self._parse_conditional(evaluate=evaluate and condition[0] == 0)
            # C11 6.5.15p5: the usual arithmetic conversions apply to BOTH arms
            # even though only one is evaluated, so ``(0u ? 1 : -1) < 0`` keeps
            # the block (signed arms -> signed -1) while ``(0 ? 1u : -1) < 0``
            # drops it (an unsigned arm makes the result
            # uintmax_t(-1) = 2**64-1, which is not < 0). Verified against
            # libcpp's CPP_COLON reduction.
            unsigned = true_value[1] or false_value[1]
            return _wrap(true_value[0] if condition[0] != 0 else false_value[0], unsigned), unsigned
        finally:
            self._depth -= 1

    def _parse_logical_or(self, evaluate: bool) -> _CValue:
        value = self._parse_logical_and(evaluate)
        while self._match("||"):
            # A6: once the LHS is true the result is 1 and the RHS is dead.
            rhs_live = evaluate and value[0] == 0
            rhs = self._parse_logical_and(rhs_live)
            # ``||`` yields a signed ``int`` 0/1 (C11 6.5.14), so the result
            # carries no operand signedness. With no operator matched the
            # operand tuple is returned as-is: a lone ``18446744073709551615``
            # must stay unsigned, or the value invariant would break.
            value = (1 if value[0] != 0 or rhs[0] != 0 else 0, False)
        return value

    def _parse_logical_and(self, evaluate: bool) -> _CValue:
        value = self._parse_bitwise_or(evaluate)
        while self._match("&&"):
            # A6: once the LHS is false the result is 0 and the RHS is dead.
            rhs_live = evaluate and value[0] != 0
            rhs = self._parse_bitwise_or(rhs_live)
            # ``&&`` yields a signed ``int`` 0/1 (C11 6.5.13).
            value = (1 if value[0] != 0 and rhs[0] != 0 else 0, False)
        return value

    def _parse_bitwise_or(self, evaluate: bool) -> _CValue:
        value = self._parse_bitwise_xor(evaluate)
        while self._match("|"):
            value = self._apply_binary("|", value, self._parse_bitwise_xor(evaluate), evaluate)
        return value

    def _parse_bitwise_xor(self, evaluate: bool) -> _CValue:
        value = self._parse_bitwise_and(evaluate)
        while self._match("^"):
            value = self._apply_binary("^", value, self._parse_bitwise_and(evaluate), evaluate)
        return value

    def _parse_bitwise_and(self, evaluate: bool) -> _CValue:
        value = self._parse_equality(evaluate)
        while self._match("&"):
            value = self._apply_binary("&", value, self._parse_equality(evaluate), evaluate)
        return value

    def _parse_equality(self, evaluate: bool) -> _CValue:
        value = self._parse_relational(evaluate)
        while op := self._match("==", "!="):
            value = self._apply_binary(op, value, self._parse_relational(evaluate), evaluate)
        return value

    def _parse_relational(self, evaluate: bool) -> _CValue:
        value = self._parse_shift(evaluate)
        while op := self._match("<", "<=", ">", ">="):
            value = self._apply_binary(op, value, self._parse_shift(evaluate), evaluate)
        return value

    def _parse_shift(self, evaluate: bool) -> _CValue:
        value = self._parse_additive(evaluate)
        while op := self._match("<<", ">>"):
            rhs = self._parse_additive(evaluate)
            if not evaluate:
                # Dead subtree (A6/A7): skip the shift entirely. The returned
                # value is discarded, so there is nothing to compute. (A live
                # oversized shift is bounded too, by ``_c_shift``'s clamp.)
                value = (0, value[1])
            else:
                # Shifts do NOT go through ``_apply_binary``: they are exempt
                # from the usual arithmetic conversions (C11 6.5.7p3), so the
                # LEFT operand's signedness alone decides the result's.
                value = (self._c_shift(value[0], op, rhs[0], value[1]), value[1])
        return value

    @classmethod
    def _c_shift(cls, value: int, op: str, count: int, unsigned: bool) -> int:
        """Apply ``<<``/``>>`` the way libcpp's ``num_binary_op``/``num_lshift`` do.

        Three rules, all measured against ``g++ -E -P -Wall -Wextra -pedantic``.

        A NEGATIVE count is undefined in C99 6.5.7p3, and Python's shift
        operators raise ``ValueError`` on one. libcpp instead reverses the
        direction and negates the count, so ``1 >> -1`` is ``1 << 1`` and the
        ``#if`` branch is kept. clang treats the same expressions as an
        overflow yielding 0, so the shapes stay out of the differential test's
        generator -- following gcc here is a choice between two compilers, not
        between right and wrong.

        A LEFT shift by ``intmax_t``'s precision or more yields 0, matching
        libcpp's ``num_lshift``: once the count reaches the width, every bit is
        shifted out. The clamp applies to a count that arrived positive as much
        as to one the flip produced -- ``#if (1 << 64)`` is a block gcc drops,
        and evaluating it in Python bignums kept it. Right shifts need no clamp:
        Python's arithmetic shift already sign-fills to 0 or -1 as libcpp does.

        Below the clamp, a left shift's result wraps to the LEFT operand's
        width -- shifts are exempt from the usual arithmetic conversions (C11
        6.5.7p3), so the count never participates and ``unsigned`` is the left
        operand's signedness. ``1 << 63`` is INTMAX_MIN, the truthy value
        behind a block gcc keeps. A right shift needs no wrap (the value
        invariant already keeps it in range); an unsigned right shift falls out
        of the stored non-negative representation as a logical shift, a signed
        one sign-fills, both as libcpp does.

        The clamp is also what keeps the operation total. A live
        ``1 << 10000000000`` would otherwise materialise a gigabyte-wide bignum
        before being tested for truth, and a count past ``PY_SSIZE_T_MAX``
        raises out of the arithmetic entirely.
        """
        if count < 0:
            op = ">>" if op == "<<" else "<<"
            count = -count
        if op == "<<" and count >= _INTMAX_PRECISION:
            return 0
        if op == "<<":
            return _wrap(value << count, unsigned)
        return value >> count

    def _parse_additive(self, evaluate: bool) -> _CValue:
        value = self._parse_multiplicative(evaluate)
        while op := self._match("+", "-"):
            value = self._apply_binary(op, value, self._parse_multiplicative(evaluate), evaluate)
        return value

    def _parse_multiplicative(self, evaluate: bool) -> _CValue:
        value = self._parse_unary(evaluate)
        while op := self._match("*", "/", "%"):
            value = self._apply_binary(op, value, self._parse_unary(evaluate), evaluate)
        return value

    def _parse_unary(self, evaluate: bool) -> _CValue:
        # Track depth here too: stacked prefix operators (``!!!...``) recurse
        # through ``_parse_unary`` without passing ``_parse_conditional``, so
        # this is the other unbounded-recursion vector.
        self._enter()
        try:
            if self._match("+"):
                return self._parse_unary(evaluate)
            if self._match("-"):
                value, unsigned = self._parse_unary(evaluate)
                # Unary minus keeps the operand's signedness and wraps: in
                # libcpp -INTMAX_MIN stays INTMAX_MIN, it does not overflow.
                return _wrap(-value, unsigned), unsigned
            if self._match("!"):
                value, _ = self._parse_unary(evaluate)
                return (0 if value else 1, False)
            if self._match("~"):
                value, unsigned = self._parse_unary(evaluate)
                return _wrap(~value, unsigned), unsigned
            return self._parse_primary(evaluate)
        finally:
            self._depth -= 1

    def _parse_primary(self, evaluate: bool) -> _CValue:
        token = self._peek()
        if not isinstance(token, str):
            self.pos += 1
            return token
        if self._match("("):
            value = self._parse_conditional(evaluate)
            if not self._match(")"):
                raise SyntaxError("missing closing parenthesis")
            return value
        raise SyntaxError("expected integer or parenthesized expression")

    def _apply_binary(self, op: str, lhs: _CValue, rhs: _CValue, evaluate: bool) -> _CValue:
        """Apply a non-shift binary operator under the usual arithmetic
        conversions (C11 6.3.1.8): if either operand is unsigned, both are
        reinterpreted as uintmax_t and the result is unsigned; the six
        comparisons yield a signed ``int`` 0/1 either way. Arithmetic results
        wrap via ``_wrap``. Mirrors libcpp's ``num_binary_op``/``num_div_op``;
        shifts bypass this (see ``_parse_shift``).
        """
        lv, lu = lhs
        rv, ru = rhs
        unsigned = lu or ru
        if unsigned:
            lv &= _UMASK  # reinterpret a negative signed operand
            rv &= _UMASK
        if (compare := _COMPARISON_OPS.get(op)) is not None:
            return (1 if compare(lv, rv) else 0), False
        if not evaluate and op in _DIVISION_OPS:
            # Dead subtree (A6/A7): skip the div/mod so a dead ``x / 0``
            # never raises. The returned value is discarded.
            return (0, unsigned)
        return _wrap(_ARITHMETIC_OPS[op](lv, rv), unsigned), unsigned


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
        # Same rebinding for the deferred-report stores, which live on the
        # context's CURRENT VerdictPartition (one per root target). Deferral
        # is only armed when the context reports an enclosing convergence;
        # otherwise a diagnostic is printed the moment it is found.
        self._pending_warnings: dict[tuple[str, str, str, int], tuple[str, bool, str]] = {}
        self._resolved_conditions: dict[tuple[str, str, str, int], bool] = {}
        # The same pair for the expansion-cap reports, rebound the same way.
        self._pending_expansion_warnings: dict[ExpansionKey, str] = {}
        self._resolved_expansions: set[ExpansionKey] = set()
        # Session-wide message library, rebound from the context; carries the
        # composed message a warm-hit replay needs (see BuildContext).
        self._verdict_messages: dict[tuple[str, str, str, int], tuple[str, bool, str]] = {}
        self._defer_warnings: bool = False
        self._current_filepath: str = "<unknown>"
        # The directive whose text the expansion helpers are working on, as
        # (directive_type, line_num). Set around directive dispatch; the
        # fallback stands in for an expansion with no directive to name (the
        # computed-include resolver, and magicflags' settled-state expander,
        # which runs outside process_structured entirely).
        self._expansion_site: tuple[str, int] = _NO_EXPANSION_SITE
        # Occurrence-keyed record of every _note_condition_resolved /
        # _note_condition_unreached / _note_expansion_settled call made during
        # ONE process_structured call, reset at its start. The kind slot says
        # which store an entry addresses on replay.
        # preprocessing_cache attaches this to the
        # cached ProcessingResult so a later cache HIT for the same file can
        # replay the same pending-store retractions a live rerun would have
        # made, without re-running the preprocessor. See
        # ProcessingResult.condition_occurrences.
        self.condition_occurrences: list[tuple[str, str, str, int]] = []
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
        convergence — a report is raised at ``verbose >= 1`` (matching the
        sibling), so a half-expanded token being handed to the __has_* probe is
        not silently truncated. The last seen expression is returned regardless.
        """
        original_sz = expr_sz
        previous_expr = sz.Str("")
        iteration = 0
        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_object_macros_sz(expr_sz)
            iteration += 1

        # If we hit the iteration cap AND the operand was still mutating on the
        # last pass, the expansion was truncated — report it (mirrors
        # _recursive_expand_macros_sz, scoped to the __has_* operand context).
        if iteration == max_iterations and expr_sz != previous_expr:
            self._report_expansion_cycle(
                HAS_OPERAND_EXPANSION_KIND,
                original_sz,
                f"WARNING: SimplePreprocessor._expand_object_macros_recursive_sz hit "
                f"max_iterations={max_iterations} while a __has_* operand was still "
                f"changing — likely a recursive macro definition cycle. "
                f"Truncated result: {expr_sz!r} (previous: {previous_expr!r})",
            )
        else:
            self._note_expansion_settled(HAS_OPERAND_EXPANSION_KIND, original_sz)

        return expr_sz

    def _recursive_expand_macros_sz(self, expr_sz: sz.Str, max_iterations: int = 10) -> sz.Str:
        """Recursively expand macros using StringZilla operations until no more changes occur.

        ``max_iterations`` (default 10) caps the expansion depth to defeat
        pathological macro definitions that would otherwise loop forever
        (e.g. ``#define A B`` and ``#define B A``). When the cap is hit
        AND the expression is still changing on each pass — indicating a
        genuine cycle rather than benign convergence — a report is raised
        at ``verbose >= 1`` so the user knows their macro definitions are
        cyclic and the result was truncated. The last seen expression is
        returned regardless.
        """
        original_sz = expr_sz
        previous_expr = sz.Str("")  # Initialize with empty StringZilla.Str instead of None
        iteration = 0

        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_macros_sz(expr_sz)
            iteration += 1

        # If we hit the iteration cap AND the expression was still mutating
        # on the last pass, the expansion was truncated — report it.
        if iteration == max_iterations and expr_sz != previous_expr:
            self._report_expansion_cycle(
                EXPANSION_KIND,
                original_sz,
                f"WARNING: SimplePreprocessor._recursive_expand_macros_sz hit "
                f"max_iterations={max_iterations} while expression was still "
                f"changing — likely a recursive macro definition cycle. "
                f"Truncated result: {expr_sz!r} (previous: {previous_expr!r})",
            )
        else:
            self._note_expansion_settled(EXPANSION_KIND, original_sz)

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
        self.condition_occurrences = []
        # getattr, not attribute access: callers are free to pass a duck-typed
        # context carrying only the caches they need (test_preprocessing_cache_scoping
        # does), and a diagnostic must not be the thing that breaks them.
        self._warned_conditions = getattr(context, "warned_preprocessor_conditions", self._warned_conditions)
        self._pending_warnings = getattr(context, "pending_preprocessor_warnings", self._pending_warnings)
        self._resolved_conditions = getattr(context, "resolved_preprocessor_conditions", self._resolved_conditions)
        self._pending_expansion_warnings = getattr(
            context, "pending_preprocessor_expansion_warnings", self._pending_expansion_warnings
        )
        self._resolved_expansions = getattr(context, "resolved_preprocessor_expansions", self._resolved_expansions)
        self._verdict_messages = getattr(context, "verdict_messages", self._verdict_messages)
        self._expansion_site = _NO_EXPANSION_SITE
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

                # Handle the directive. The site names whichever directive the
                # expansion helpers are working under, so an expansion the
                # iteration cap truncates can be reported against a line.
                self._expansion_site = (directive.directive_type, directive.line_num)
                handled = self._handle_directive_structured(directive, condition_stack, i + 1)
                self._expansion_site = _NO_EXPANSION_SITE

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
            self._note_condition_unreached(directive)
            condition_stack.append((False, False, False))
        elif directive.condition:
            try:
                # Pass the raw condition: _evaluate_expression_sz strips comments
                # internally as its first step, so no handler-level strip is needed.
                result = self._evaluate_expression_sz(directive.condition)
                self._note_condition_resolved(directive, result)
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
            self._note_condition_unreached(directive)
            condition_stack.append((False, False, any_condition_met))
        elif not seen_else and not any_condition_met and directive.condition:
            try:
                # Pass the raw condition: _evaluate_expression_sz strips comments
                # internally as its first step, so no handler-level strip is needed.
                result = self._evaluate_expression_sz(directive.condition)
                self._note_condition_resolved(directive, result)
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
            # Either we already found a true condition or seen_else is True, so
            # this #elif is short-circuited and its condition is never consulted.
            self._note_condition_unreached(directive)
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

        Inside a convergence the report is recorded rather than printed. An
        early pass evaluates a file before the macros that control it have
        been discovered, so a condition unevaluable there is routinely
        resolved by a later pass; printing from that pass produces
        "assuming false" on a build whose final answer is the true branch.
        ``_note_condition_resolved`` retracts the record when that happens
        (within the same verdict partition — one root target's walks) and
        ``flush_pending_warnings`` reports whatever is still unevaluable once
        the macro state has settled.

        The DEFERRED record is written at every verbosity, with the verbose
        gate carried as the record's ``audible`` flag: conflict
        classification (``check_verdict_conflicts``) must see the
        assume-false verdict even in a quiet build, because two targets
        disagreeing about a shared gate is a product inconsistency, not a
        log line. Only the immediate print keeps the verbose >= 1 gate.
        """
        condition = str(directive.condition)
        seen_key = (self._current_filepath, directive.directive_type, condition)
        occurrence_key = seen_key + (directive.line_num,)
        self.condition_occurrences.append(
            (CONDITION_UNEVALUABLE, directive.directive_type, condition, directive.line_num)
        )

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
        # The evaluator's reason rides the record as its own field: the
        # conflict report needs it verbatim, and re-parsing the composed
        # message would grab a function-like condition's own argument list.
        record = (message, self.verbose >= 1, str(error))
        # Write-once so the first (most contextful) composition wins; a warm
        # hit replaying this occurrence into another partition reads it back.
        self._verdict_messages.setdefault(occurrence_key, record)

        if self._defer_warnings:
            # Recorded per occurrence, not per condition: a file can spell the
            # same condition twice with only one of them reachable, and a
            # position-free key would let either one's fate decide the other's.
            # The flush collapses these back to one line per condition.
            if occurrence_key not in self._resolved_conditions:
                self._pending_warnings.setdefault(occurrence_key, record)
            return

        if self.verbose < 1:
            return
        if seen_key in self._warned_conditions:
            return
        self._warned_conditions.add(seen_key)
        print(message, file=sys.stderr)

    def _note_condition_resolved(self, directive: "PreprocessorDirective", result) -> None:
        """Record that this occurrence is evaluable, and drop its pending report.

        Marking is what makes the retraction stick. Passes are not ordered
        best-last: a convergence can evaluate a file correctly and then process
        it again from a state that cannot (a cache replay that strands a
        function-like macro's parameter list, say), and that later pass would
        re-record an occurrence already shown to be evaluable.

        Retracts only THIS occurrence, like ``_note_condition_unreached``, and
        only within the current verdict partition — another target's walk
        assuming the same occurrence false is a distinct settled verdict that
        this one does not supersede (``check_verdict_conflicts`` is where the
        two meet). Evaluability is a property of the macro state, and the
        state is positional too: an ``#undef`` plus a different-arity
        redefinition between two textually identical occurrences makes one
        evaluable and the other not, so a text-wide retraction would let the
        evaluable one silence the report the other still owes.

        The recorded kind carries the truth value (*result*): classification
        must tell a TRUE resolution (the sibling target's product genuinely
        diverges from an assume-false) from a FALSE one (the outcomes
        coincide today and the divergence is latent).
        """
        # Recorded UNCONDITIONALLY, before the deferral gate: evaluability is
        # a property of the run, not of whether a convergence is listening.
        # A cache entry seeded at depth 0 (the production order — the
        # headerdeps walk runs outside any converging region, before the
        # magicflags convergence over the same context caches) must still
        # carry the occurrences a later warm hit inside a convergence needs
        # to replay; gating this on _defer_warnings left such entries empty
        # and stranded the early pass's pending record.
        kind = CONDITION_RESOLVED_TRUE if result else CONDITION_RESOLVED_FALSE
        self.condition_occurrences.append(
            (kind, directive.directive_type, str(directive.condition), directive.line_num)
        )
        if not self._defer_warnings:
            return
        key = (self._current_filepath, directive.directive_type, str(directive.condition), directive.line_num)
        # Membership is sticky (protects against a later failing pass
        # re-recording), but the truth value is last-write-wins: successive
        # passes refine the macro state, and classification wants the most
        # settled answer, not the first.
        self._resolved_conditions[key] = bool(result)
        self._pending_warnings.pop(key, None)

    def _note_condition_unreached(self, directive: "PreprocessorDirective") -> None:
        """Record that this pass never reached the condition, and drop any report.

        A dead enclosing branch means the directive is not part of the build,
        so an "assuming false" attributed to it is telling the user about a
        line that was never compiled either way. The skip is silent in a
        non-deferring run because nothing evaluates and nothing warns; inside a
        convergence an EARLIER pass may already have recorded the condition
        pending, from a state where the parent was still live.

        Deliberately does NOT mark the condition resolved. The two stores are
        asymmetric because the failure they guard against is opposite:
        re-recording is a bug for a condition already shown evaluable and is
        the requirement for one merely unreached, since a later pass that
        reaches it and still cannot evaluate it must warn. Making this sticky
        would silence exactly the liveparent arm the fix has to preserve.

        Retracts only THIS occurrence, which is why the pending store is keyed
        by line. Reachability is positional: a file can spell one condition
        twice with one occurrence live and unevaluable and the other under a
        dead branch, and a position-free retraction would delete the live one's
        report. ``_note_condition_resolved`` is occurrence-keyed for the same
        reason, with the macro state playing the role of reachability.
        """
        # Unconditional for the same reason as _note_condition_resolved's
        # record: a depth-0-seeded cache entry must carry its occurrences.
        self.condition_occurrences.append(
            (CONDITION_UNREACHED, directive.directive_type, str(directive.condition), directive.line_num)
        )
        if not self._defer_warnings:
            return
        key = (self._current_filepath, directive.directive_type, str(directive.condition), directive.line_num)
        self._pending_warnings.pop(key, None)

    def _expansion_key(self, kind: str, original_sz: "sz.Str") -> ExpansionKey:
        """Address the expansion currently running: site plus kind plus input text."""
        directive_type, line_num = self._expansion_site
        return (self._current_filepath, directive_type, line_num, kind, str(original_sz))

    def _report_expansion_cycle(self, kind: str, original_sz: "sz.Str", message: str) -> None:
        """Report an expansion the iteration cap truncated.

        Inside a magicflags convergence the report is recorded rather than
        printed, for the same reason the unevaluable-condition report is: an
        early pass expands under a macro state the build has not settled on,
        where ``A`` and ``B`` can both be defined and mutually referential
        while the settled state expands the same text cleanly. Printing from
        that pass contradicts the flags the build goes on to emit.
        ``_note_expansion_settled`` retracts the record when a later pass
        expands the same occurrence under the cap, and
        ``flush_pending_warnings`` prints whatever is still truncated once the
        macro state has settled. The truncation itself is unaffected either
        way — this changes what the user is told, not what is built.
        """
        if self.verbose < 1:
            return
        if not self._defer_warnings:
            print(message)
            return
        key = self._expansion_key(kind, original_sz)
        if key not in self._resolved_expansions:
            self._pending_expansion_warnings.setdefault(key, message)

    def _note_expansion_settled(self, kind: str, original_sz: "sz.Str") -> None:
        """Record that this expansion reached a fixed point under the cap.

        The occurrence is appended unconditionally, like
        ``_note_condition_resolved``'s: it rides on the cached
        ProcessingResult, whose key contains neither the verbosity nor the
        convergence depth of the run that computed it, so a depth-0 seeding
        pass must leave a later warm hit inside a convergence something to
        replay.

        Sticky, again like the conditions: a pass that hits the cap AFTER one
        that expanded the same occurrence cleanly must not re-record it.
        """
        self.condition_occurrences.append((kind, self._expansion_site[0], str(original_sz), self._expansion_site[1]))
        if not self._defer_warnings:
            return
        key = self._expansion_key(kind, original_sz)
        self._resolved_expansions.add(key)
        self._pending_expansion_warnings.pop(key, None)

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

        # Integer-literal handling (hex/bin/octal, u/ll suffixes, intmax_t
        # classification) lives entirely in the tokenizer via _RE_INT_LITERAL +
        # _literal: unlike the old strip-and-normalize pre-passes, that keeps
        # the u suffix, which decides signedness in comparisons like -1 > 0u,
        # and rejects pp-numbers like 08 that are not integer constants.
        # Tokenizing and parsing are separate try blocks so the two failure
        # channels stay separate. The tokenizer's UnsafeExpressionError means
        # "this is not an arithmetic expression"; anything the PARSER raises is
        # a statement about a well-formed expression, and must not be read as
        # the unsafe signal. A single ``except ValueError`` around both spoke
        # for the tokenizer and silently swallowed the parser's arithmetic
        # ValueError (negative shift count) under the same label.
        #
        # UnsafeExpressionError propagates to the #if/#elif caller, which turns
        # it into the unevaluable-condition report.
        tokens = self._tokenize_c_expression(expr)

        try:
            return _CExpressionParser(tokens).parse()
        except ValueError as e:
            # The parser reached an operation it cannot represent on a
            # well-formed expression. That is an evaluator failure, never a
            # false branch -- surface it the way an over-deep expression is
            # surfaced. Defence in depth: the shift operators handle a negative
            # count themselves (C semantics), so no live path reaches here.
            raise PreprocessorExpressionError(f"cannot evaluate '{expr}': {e}") from e
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
                m = _RE_INT_LITERAL.match(expr, i)
                if m is None:
                    # A digit run that _RE_INT_LITERAL cannot match is a
                    # pp-number that is not an integer constant: a bad octal
                    # digit (``08``), a trailing garbage suffix (``123abc``),
                    # or a suffix run like ``1UU``. Raise rather than guess.
                    raise UnsafeExpressionError(f"malformed integer constant near {expr[i : i + 8]!r}")
                tokens.append(_literal(m.group(1), m.group(2) or ""))
                i = m.end()
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
                tokens.append((0, False))
                continue

            # A stray byte that is neither part of a number, operator, nor a
            # ``[A-Za-z_][A-Za-z0-9_]*`` identifier (e.g. ``@``, ``$``, a
            # backtick, or a quote) is genuine garbage — reject it so the
            # #if/#elif caller assumes false.
            raise UnsafeExpressionError(f"Unsafe expression: {expr}")

        tokens.append("EOF")
        return tokens


def flush_pending_warnings(context: Any) -> int:
    """Report the diagnostics still owed now the macro state has settled.

    Two kinds are deferred through a convergence — conditions the evaluator
    could not represent, and macro expansions the iteration cap truncated —
    and both are flushed here, across every verdict partition. Returns the
    total number of lines printed.
    """
    partitions = getattr(context, "verdict_partitions", None)
    if partitions is None:
        # Duck-typed context carrying the flat stores directly (tests that
        # drive the flush over a SimpleNamespace).
        return _flush_one_partition_conditions(context, context) + _flush_one_partition_expansions(context)

    # The None partition holds verdicts from walks with no target attribution
    # (target discovery, direct driving). Any named partition's settled
    # resolution retracts from it — conditions and cap-truncated expansions
    # alike: an unattributed walk is by construction a provisional
    # approximation, never a settled per-target answer.
    unattributed = partitions.get(None)
    if unattributed is not None and (unattributed.pending or unattributed.pending_expansions):
        for partition_root, partition in partitions.items():
            if partition_root is None:
                continue
            for key in partition.resolved:
                unattributed.pending.pop(key, None)
            for expansion_key in partition.resolved_expansions:
                unattributed.pending_expansions.pop(expansion_key, None)

    printed = 0
    for partition in partitions.values():
        printed += _flush_one_partition_conditions(partition, context)
        printed += _flush_one_partition_expansions(partition)
    partitions.clear()
    return printed


def _flush_one_partition_conditions(store_holder: Any, context: Any) -> int:
    """Report one partition's still-unevaluable conditions.

    Returns the number of lines printed. Entries already reported are skipped
    and the store is emptied either way, so a second flush over the same
    partition prints nothing.

    The store holds one entry per OCCURRENCE so that a retraction can name the
    occurrence it belongs to; the output contract is one line per condition per
    file per build session, so the dedupe here drops the line number from the
    key and the session memo collapses across partitions. A record's
    ``audible`` flag carries the recording run's verbose gate: an inaudible
    record exists only for conflict classification and is never printed.

    The resolved store is cleared even when nothing is pending: both stores
    describe one settled walk, and a key surviving into a later flush would
    silence a report that walk goes on to owe.
    """
    resolved = getattr(store_holder, "resolved_preprocessor_conditions", None)
    if resolved is None:
        resolved = getattr(store_holder, "resolved", None)
    if resolved is not None:
        resolved.clear()

    pending = getattr(store_holder, "pending_preprocessor_warnings", None)
    if pending is None:
        pending = getattr(store_holder, "pending", None)
    if not pending:
        return 0

    reported = getattr(context, "warned_preprocessor_conditions", None)
    seen: set[tuple[str, str, str]] = set()
    printed = 0
    for key, record in pending.items():
        if isinstance(record, tuple):
            message, audible = record[0], record[1]
        else:
            message, audible = record, True
        if not audible:
            continue
        condition_key = key[:3]
        if condition_key in seen:
            continue
        seen.add(condition_key)
        if reported is not None:
            if condition_key in reported:
                continue
            reported.add(condition_key)
        print(message, file=sys.stderr)
        printed += 1

    pending.clear()
    return printed


def _flush_one_partition_expansions(store_holder: Any) -> int:
    """Report one partition's cap-truncated expansions.

    One line per surviving occurrence, on stdout, matching the immediate path:
    two truncations at two sites are two cycles for the user to fix, so unlike
    the condition flush there is nothing to collapse.

    The resolved store is cleared even when nothing is pending, for the same
    one-settled-walk reason as the condition flush.
    """
    resolved = getattr(store_holder, "resolved_preprocessor_expansions", None)
    if resolved is None:
        resolved = getattr(store_holder, "resolved_expansions", None)
    if resolved is not None:
        resolved.clear()

    pending = getattr(store_holder, "pending_preprocessor_expansion_warnings", None)
    if pending is None:
        pending = getattr(store_holder, "pending_expansions", None)
    if not pending:
        return 0

    for message in pending.values():
        print(message)
    printed = len(pending)
    pending.clear()
    return printed


def check_verdict_conflicts(context: Any, *, mode: str = "error", tool: str = "ct-cake") -> int:
    """Cross-target verdict classification, run once all walks have settled.

    A condition occurrence one target's settled walk could not evaluate
    (assumed false) is compared against every other target's settled verdict
    for the same occurrence:

    - resolved TRUE elsewhere — the products genuinely diverge: the two
      targets' objects compile the shared header's flags differently. Hard
      :class:`MacroVerdictConflictError` (or a stderr warning under
      ``mode="warn"``), at every verbosity: this is a product inconsistency,
      not a log line.
    - resolved only FALSE elsewhere — the outcomes coincide today, but the
      gate is one macro bump away from diverging. Reported as a warning with
      the same guidance, honouring the recording run's verbose gate.
    - unevaluable everywhere (or nowhere else seen) — left for the ordinary
      flush; not a conflict.

    Must run BEFORE the backend executes: reporting after the link has
    published a wrong binary is an apology, not a guard. The ``None``
    partition (unattributed walks — target discovery, the pre-fetch scan
    outside any per-target attribution) never participates: its verdicts are
    provisional by construction.

    A classified occurrence's pending record is dropped so the later flush
    does not restate a condition this report already explained. *tool* is
    the entry point's name and only prefixes the report — a refusal must be
    attributed to the command the user actually ran. Returns the number of
    conflicts found (both severities).
    """
    partitions = getattr(context, "verdict_partitions", None)
    if not partitions:
        return 0

    named = {root: p for root, p in partitions.items() if root is not None}
    conflicts = 0
    for root, partition in named.items():
        for key in list(partition.pending):
            record = partition.pending[key]
            audible = record[1]
            reason = record[2] if len(record) > 2 else record[0]
            true_roots = [r for r, p in named.items() if r != root and p.resolved.get(key) is True]
            false_roots = [r for r, p in named.items() if r != root and p.resolved.get(key) is False]
            if not true_roots and not false_roots:
                continue

            filepath, directive_type, condition, line_num = key
            divergent = bool(true_roots)
            other_root = true_roots[0] if divergent else false_roots[0]
            verdict_word = "TRUE" if divergent else "FALSE"
            severity = "error" if (divergent and mode == "error") else "warning"
            report = (
                f"{tool} {severity}: conflicting verdicts for '#{directive_type} {condition}' "
                f"at {filepath}:{line_num + 1}\n"
                f"  evaluates {verdict_word} under target {other_root}\n"
                f"  cannot evaluate under target {root} ({reason}) - assumed false\n"
                + (
                    f"  These targets compile {filepath}'s conditional flags differently.\n"
                    if divergent
                    else "  The outcomes coincide today; the gate diverges when its macros change.\n"
                )
                + "Remedies:\n"
                f"  - make the gate self-contained: #include the header defining its macros from {filepath} itself\n"
                "  - or define the controlling macros project-wide (CPPFLAGS in ct.conf.d/) so every target agrees\n"
                "  - or, if per-target divergence is intended, wrap the gate in #ifdef so the undefined arm is explicit"
            )

            partition.pending.pop(key, None)
            conflicts += 1
            if divergent:
                if mode == "error":
                    raise MacroVerdictConflictError(report)
                print(report, file=sys.stderr)
            elif audible:
                print(report, file=sys.stderr)
    return conflicts


@contextlib.contextmanager
def verdict_session(context: Any, *, mode: str = "error", tool: str = "ct-cake") -> "Iterator[None]":
    """One tool invocation's deferral region plus conflict classification.

    The shared shape for every entry point that walks more than one target
    through magicflags (ct-cake, ct-filelist, ct-compilation-database,
    ct-magicflags): a ``converging`` region spanning all walks, with
    ``check_verdict_conflicts`` run once the body completes, BEFORE the
    region's exit flush clears the partitions. One implementation so the
    tools agree — a conflict ct-cake refuses must not pass silently through
    ct-filelist over the same tree.

    Classification is skipped when the body raises (the partitions are
    incomplete, and the original error is the diagnostic); the region's
    flush still runs. Cake additionally calls the classifier itself between
    build_graph and execute — earlier than this exit — and that call pops
    what it classifies, so the exit call here reports nothing twice.
    """
    with converging(context):
        yield
        check_verdict_conflicts(context, mode=mode, tool=tool)


@contextlib.contextmanager
def provisional_scan(context: Any) -> "Iterator[None]":
    """Defer AND DISCARD: a region for scans with no settling pass at all.

    ``ct-fetch`` walks targets with an empty macro state purely to find
    ``//#GIT=`` declarations. Every one of its evaluation verdicts is
    provisional by construction and the tool has no build to settle them
    against, so printing "assuming false" from it contradicts the very next
    ``ct-cake`` run — the cross-tool version of the contradiction
    ``converging`` exists to remove. On outermost exit the partitions are
    dropped unprinted instead of flushed; nested inside a real region (cake
    driving the same scan) it only deepens the depth counter and the
    enclosing session keeps ownership of the stores.
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
            partitions = getattr(context, "verdict_partitions", None)
            if partitions is not None:
                partitions.clear()


@contextlib.contextmanager
def verdict_root(context: Any, root: "str | None") -> "Iterator[None]":
    """Attribute the enclosed walks' verdicts to *root*'s partition.

    Wrap the widest span in which every preprocessor pass serves ONE root
    target — magicflags' per-target parse, or the per-target half of the
    pre-fetch scan. Restores the previous attribution on exit, so nesting a
    per-target span inside an unattributed region behaves.
    """
    if not hasattr(context, "current_verdict_root"):
        yield
        return
    prior = context.current_verdict_root
    context.current_verdict_root = root
    try:
        yield
    finally:
        context.current_verdict_root = prior


@contextlib.contextmanager
def converging(context: Any) -> "Iterator[None]":
    """Hold unevaluable-condition and expansion-cycle reports until the state settles.

    Inside this region a report is provisional, not the build's answer.
    Reports raised here are held and only printed on exit, minus any
    occurrence a later pass evaluated or expanded cleanly. Two callers need
    that, for the same reason:

    * magicflags evaluates a file repeatedly, and an early pass runs before
      the macros controlling that file have been discovered;
    * ``ct-cake`` walks every target with an empty macro state before it
      builds, to find ``//#GIT=`` declarations (``fetch._reachable_sources``),
      and that walk cannot evaluate a gate whose macro lives in another file.

    Wrap the widest region in which a later pass can still supersede an
    earlier verdict. For cake that is the whole of ``Cake.process``: the
    pre-fetch scan and the convergence that settles it are both inside, so
    the scan's verdicts reconcile against the build's own instead of racing
    it to stderr. A region that ends before the settling pass runs would
    flush the very reports it was opened to hold.

    Supersession has a boundary: it is only legitimate between passes over
    the SAME root target (approximations of one settled answer), never
    between two targets' settled states (distinct final answers that
    coexist in the product). ``verdict_root`` marks the target a walk
    serves, the stores are partitioned by it, and retraction stays inside a
    partition; ``check_verdict_conflicts`` is where two partitions'
    disagreements become an error or warning, before the backend executes.

    Re-entrant: only the outermost region flushes. Consumers that drive the
    preprocessor without one never enter it and keep reporting immediately.
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
