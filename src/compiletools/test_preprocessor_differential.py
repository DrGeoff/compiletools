"""Differential test: compiletools' hand-written preprocessor vs a real compiler.

This is a *differential* (oracle-based) test. It compares compiletools' own
conditional-compilation engine

    file_analyzer.analyze_file()  +  SimplePreprocessor.process_structured()

against a real C preprocessor (``<cxx> -E -P``) treated as ground truth. The
technique targets the highest-value bug class for a build tool: *silent
miscompiles*, where the hand-written preprocessor keeps or drops the wrong set
of source lines and the build quietly compiles the wrong translation unit.

How the oracle works
--------------------
Each candidate source wraps unique sentinel tokens (``SENTINEL_<n>``) in
generated ``#if / #ifdef / #ifndef / #elif / #else / #endif / #define`` scaffolding.
Running the source through ``<cxx> -E -P`` and scraping the surviving
``SENTINEL_<n>`` tokens gives the ground-truth *active* set. Running the same
source through compiletools and mapping its active line indices back to the
sentinels on those lines gives the candidate set. The two sets must be equal.

Hypothesis drives a bounded generator over a small integer-expression /
directive grammar (decimal literals, ``+ - * / %``, ``<< >>``, ``& | ^ ~``,
``&& || !``, the six comparisons, ``?:``, ``defined(NAME)``, parentheses, and
object-macro names). Nesting and length are bounded so each compiler subprocess
stays fast.

Staying inside the well-defined common subset
---------------------------------------------
Rather than statically bound expression magnitudes, we let the compiler itself
mark the boundary of the well-defined subset: any generated program the compiler
rejects (non-zero exit) or merely warns about (integer overflow, shift-count,
division by zero, ...) is discarded via ``assume``. What remains is exactly the
subset where C's behaviour is unambiguous, and there compiletools must agree.

That gate is necessary but NOT sufficient: it sees only what the compiler
chooses to diagnose. Where a compiler accepts undefined behaviour *silently*,
the UB reaches the comparison and the verdict becomes a property of the
installed toolchain rather than of compiletools. Such shapes have to be kept out
of the generator instead -- see the negative shift count below.

Deliberate generator exclusions (documented, not accidental)
------------------------------------------------------------
* ``__has_include`` / ``__has_*`` builtins -- they shell out to the compiler as
  an observable side effect; not part of the pure conditional-eval contract.
* FUNCTION-LIKE macros used inside ``#if`` -- known-unsupported by
  SimplePreprocessor (it only tracks object-like macros).
* Integer suffixes (``U`` / ``L`` / ``UL`` ...) -- SimplePreprocessor strips
  them, losing signedness; see the KNOWN-DIVERGENCE xfail list below.
* Leading-zero (octal) literals -- kept out of the generator so the ``08``
  invalid-octal divergence (also an xfail) can't pollute the random stream.
* NEGATIVE shift counts -- the RHS of ``<<`` / ``>>`` is forced to a
  non-negative literal. A negative count is undefined in C (C99 6.5.7p3) and the
  compilers genuinely disagree: for ``#if (1 >> (0 - 1))`` gcc 16.1 keeps the
  block (it flips the shift direction, ``1 << 1`` -> 2) and emits NO diagnostic
  even under ``-Wall -Wextra -pedantic``, while clang 22.1 warns "integer
  overflow in preprocessor expression" and treats it as 0. compiletools raises
  ``ValueError: negative shift count`` out of ``_parse_shift`` and degrades the
  condition to false. There is therefore no ground truth to compare against, and
  because gcc is the silent one the ``assume`` gate cannot discard it -- the
  property test failed on a gcc box and passed on a clang box, purely from the
  toolchain. This is NOT recorded as a known-divergence xfail: those assert "the
  C-standard-correct answer", and for UB no such answer exists.
  Out-of-range (too large) positive counts are left in: both compilers diagnose
  those, so the gate handles them.

* LEFT SHIFTS WITH A COMPUTED LHS -- ``<<`` takes two literals, bounded so the
  result stays at or under ``2**62``. The left shift is the ONLY operator whose
  overflow gcc does not diagnose: it warns for ``9223372036854775807 + 1``,
  ``4294967296 * 4294967296`` and ``(1 << 62) * 4``, but is silent for
  ``(4 << 62)`` (exactly 2**64) and ``((1 << 62) << 1)``. C evaluates ``#if`` in
  ``intmax_t`` and wraps, so gcc drops those blocks while compiletools' Python
  bignums keep them -- the bignum-vs-``intmax_t`` divergence already recorded as
  a strict xfail below, reached through a shift instead of an addition, and
  invisible to the ``assume`` gate. Measured at 2 failures in 6 fresh
  default-budget runs before this bound.
  A computed LHS cannot be allowed even with a small count, because an inner
  shift can already have produced ``2**62``. Bounding the count alone is not
  sufficient either: at ``_expr(3)`` each level can multiply (adding subtree
  exponents) or shift (adding the count), so the reachable maximum is
  ``2**(24 + 4*C)`` and staying under ``2**63`` would need ``C <= 9`` --
  abandoning large shift counts, where preprocessor bugs actually live. Two
  literals keep counts up to 56 in the stream instead.
  ``>>`` is unconstrained on the left: it only ever shrinks the magnitude, so it
  cannot overflow. Any overflow *downstream* of a shift travels through an
  operator gcc does diagnose, so the gate still catches it.

Known expected divergences
---------------------------
Three real, confirmed differences between SimplePreprocessor and C are recorded
as ``strict`` xfails in :func:`test_known_divergence_is_still_present` below,
NOT fixed here (preprocessor fixes are owned elsewhere). ``strict=True`` means
that the day any of them is fixed the xfail turns into an XPASS *failure*, so
this test actively alerts us to flip it to a normal assertion. The random
generator is separately kept from producing these shapes so the property test
stays a clean green that would go red the instant a genuine new divergence
appears.
"""

import os
import re
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import compiletools.apptools as apptools
from compiletools.build_context import BuildContext
from compiletools.file_analyzer import analyze_file, set_analyzer_args
from compiletools.global_hash_registry import get_file_hash
from compiletools.simple_preprocessor import SimplePreprocessor
from compiletools.testhelper import requires_functional_compiler

# ----------------------------------------------------------------------------
# Tunables. Each Hypothesis example spawns one compiler subprocess, so the
# example counts double as the subprocess budget. Overridable from the
# environment for a heavier soak run without editing the file.
# ----------------------------------------------------------------------------
_EXPR_EXAMPLES = int(os.environ.get("CT_DIFF_EXPR_EXAMPLES", "150"))
_PROG_EXAMPLES = int(os.environ.get("CT_DIFF_PROG_EXAMPLES", "120"))
_MACRO_EXAMPLES = int(os.environ.get("CT_DIFF_MACRO_EXAMPLES", "60"))
_SUBPROCESS_TIMEOUT = 20  # seconds; preprocessing a tiny file is sub-second

# Object-macro name pool. The CTGEN_ prefix cannot collide with any real
# compiler built-in, so an undefined CTGEN_ name evaluates to 0 in both the
# compiler and SimplePreprocessor -- keeping "undefined => 0" agreement clean.
_NAMES = ["CTGEN_A", "CTGEN_B", "CTGEN_C", "CTGEN_D", "CTGEN_E", "CTGEN_F"]

_SENTINEL_RE = re.compile(r"\bSENTINEL_(\d+)\b")

# The one compiler used as the oracle for the whole module. None => every test
# skips cleanly via @requires_functional_compiler.
_CXX = apptools.get_functional_cxx_compiler()


# ----------------------------------------------------------------------------
# Oracle + candidate evaluation helpers
# ----------------------------------------------------------------------------
def _compiler_active_sentinels(src: str):
    """Ground truth via ``<cxx> -E -P``.

    Returns (ok, sentinel_id_set). ``ok`` is False when the source falls
    outside the well-defined common subset -- the compiler either errored
    (non-zero exit: e.g. div-by-zero, invalid octal) or emitted a warning
    (integer overflow, shift-count out of range, ...). Callers ``assume(ok)``
    so only cleanly-defined programs reach the comparison.

    The candidate must carry a C++ suffix: ``_CXX`` is a C++ driver, and
    clang++ emits an unconditional driver warning for a ``.c`` input
    ("treating 'c' input as 'c++' ... deprecated") that the any-stderr
    gate would read as "outside the subset" for EVERY generated program.
    """
    assert _CXX is not None  # guaranteed by @requires_functional_compiler
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "candidate.cpp")
        with open(path, "w") as fh:
            fh.write(src)
        try:
            proc = subprocess.run(
                [_CXX, "-E", "-P", path],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, set()

    if proc.returncode != 0 or proc.stderr.strip():
        # Errors OR warnings => outside the well-defined subset. Discard.
        return False, set()

    ids = {int(m.group(1)) for m in _SENTINEL_RE.finditer(proc.stdout)}
    return True, ids


def _compiler_macro_names(src: str):
    """Final CTGEN_ macro name set via ``<cxx> -dM -E`` (the same flag pair
    magicflags.py uses). Returns (ok, name_set); ok mirrors the subset gate.
    C++ suffix for the same reason as ``_compiler_active_sentinels``."""
    assert _CXX is not None  # guaranteed by @requires_functional_compiler
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "candidate.cpp")
        with open(path, "w") as fh:
            fh.write(src)
        try:
            proc = subprocess.run(
                [_CXX, "-dM", "-E", path],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, set()

    if proc.returncode != 0 or proc.stderr.strip():
        return False, set()

    names = set()
    for line in proc.stdout.splitlines():
        m = re.match(r"#define\s+(CTGEN_[A-Z0-9_]+)\b", line)
        if m:
            names.add(m.group(1))
    return True, names


def _ct_process(src: str):
    """Run compiletools' production analysis path on ``src``.

    Returns (active_line_indices, final_macro_state) where active_line_indices
    is the 0-based list from process_structured and final_macro_state is the
    SimplePreprocessor's accumulated macro dict after processing.
    """
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "candidate.c")
        with open(path, "w") as fh:
            fh.write(src)
        ctx = BuildContext()
        args = SimpleNamespace(
            max_read_size=0,
            verbose=0,
            exemarkers=[],
            testmarkers=[],
            librarymarkers=[],
            use_mmap=True,
            force_mmap=False,
            suppress_fd_warnings=True,
            suppress_filesystem_warnings=True,
        )
        set_analyzer_args(args, ctx)
        content_hash = get_file_hash(path, ctx)
        file_result = analyze_file(content_hash, ctx)
        # Empty base macro dict: the CTGEN_ namespace is disjoint from compiler
        # built-ins, so starting empty matches the compiler's "undefined => 0"
        # treatment for every name the generator emits.
        pp = SimplePreprocessor({}, verbose=0)
        active = pp.process_structured(file_result, ctx)
        return active, pp.macros


def _ct_active_sentinels(src: str):
    """compiletools' surviving-sentinel set: map active line indices back to
    the SENTINEL_<n> tokens that sit on those lines."""
    active, _macros = _ct_process(src)
    lines = src.split("\n")
    result = set()
    for i in active:
        if 0 <= i < len(lines):
            m = _SENTINEL_RE.search(lines[i])
            if m:
                result.add(int(m.group(1)))
    return result


# ----------------------------------------------------------------------------
# Expression grammar (gcc-clean common subset; no suffixes, no leading-zero
# octal, bounded nesting depth so SimplePreprocessor never hits its recursion
# limit). Anything that overflows/UB is filtered post-hoc by the compiler gate.
# ----------------------------------------------------------------------------
_BINOPS = ["+", "-", "*", "&", "|", "^", "==", "!=", "<", ">", "<=", ">=", "&&", "||"]
_UNOPS = ["!", "~", "-", "+"]


def _leaf():
    return st.one_of(
        st.integers(min_value=0, max_value=64).map(str),
        st.sampled_from(_NAMES),
        st.sampled_from(_NAMES).map(lambda n: f"defined({n})"),
        st.sampled_from(_NAMES).map(lambda n: f"defined {n}"),
    )


def _expr(depth):
    if depth <= 0:
        return _leaf()
    sub = _expr(depth - 1)
    binary = st.builds(lambda a, op, b: f"({a} {op} {b})", sub, st.sampled_from(_BINOPS), sub)
    # Force a non-zero literal on the RHS of / and % so we don't waste examples
    # on div-by-zero (which the compiler would reject and we'd discard anyway).
    divmod_ = st.builds(
        lambda a, op, b: f"({a} {op} {b})",
        sub,
        st.sampled_from(["/", "%"]),
        st.integers(min_value=1, max_value=64).map(str),
    )
    # Shifts are the one place the compiler gate cannot be relied on, so both
    # shift operators are constructed rather than drawn from _BINOPS. See the
    # two shift notes in the module docstring.
    #
    # ``>>`` only ever shrinks the magnitude, so its LHS may be any
    # subexpression. The count is a non-negative literal below the 64-bit width
    # (a negative count is UB; a count >= width is UB). A NEGATIVE LHS is
    # implementation-defined (C99 6.5.7p5), not UB — and both supported
    # compilers arithmetic-shift, which matches Python bignum semantics
    # (-1 >> 3 == -1 in all three), so it is safe to leave in the stream.
    rshift_ = st.builds(
        lambda a, b: f"({a} >> {b})",
        sub,
        st.integers(min_value=0, max_value=62).map(str),
    )
    # ``<<`` is the only operator whose overflow gcc does NOT diagnose, so it
    # cannot take a computed LHS: an inner shift can already have produced
    # 2**62, and shifting that again overflows intmax_t silently. Both operands
    # are literals bounded so the result stays at or under 2**62 < INTMAX_MAX.
    # Any *later* overflow (e.g. multiplying two such results) travels through
    # an operator gcc does diagnose, so the gate discards it.
    lshift_ = st.builds(
        lambda a, b: f"({a} << {b})",
        st.integers(min_value=0, max_value=64).map(str),
        st.integers(min_value=0, max_value=56).map(str),
    )
    unary = st.builds(lambda op, a: f"({op}{a})", st.sampled_from(_UNOPS), sub)
    ternary = st.builds(lambda c, a, b: f"({c} ? {a} : {b})", sub, sub, sub)
    return st.one_of(_leaf(), binary, divmod_, rshift_, lshift_, unary, ternary)


def _sample_expressions(count: int) -> list[str]:
    """Draw ``count`` expressions from the grammar into a plain list, so a test
    can assert over the whole sample instead of one example at a time."""
    drawn: list[str] = []

    # derandomize: the guard consuming these samples claims to fail
    # deterministically, so the draw must be seed-stable, not just very likely
    # to contain a shift.
    @settings(max_examples=count, deadline=None, database=None, derandomize=True, suppress_health_check=_SUPPRESSED)
    @given(expr=_expr(3))
    def _collect(expr):
        drawn.append(expr)

    _collect()
    return drawn


# ----------------------------------------------------------------------------
# Structured-program grammar: a randomized but always-balanced tree of
# directives with sentinels interspersed, exercising the branch state machine
# (nested #if/#elif/#else, #define/#undef interactions) rather than just single
# expressions.
# ----------------------------------------------------------------------------
_COND = st.one_of(
    st.sampled_from(_NAMES).map(lambda n: f"defined({n})"),
    st.sampled_from(_NAMES).map(lambda n: f"!defined({n})"),
    st.sampled_from(_NAMES),
    _expr(2),
)

_SENT_NODE = st.just(("sent",))
_DEFINE_NODE = st.builds(
    lambda name, val: ("define", name, str(val)),
    st.sampled_from(_NAMES),
    st.integers(min_value=0, max_value=64),
)
_UNDEF_NODE = st.builds(lambda name: ("undef", name), st.sampled_from(_NAMES))
_BASE_NODE = st.one_of(_SENT_NODE, _SENT_NODE, _DEFINE_NODE, _UNDEF_NODE)


def _make_if(node_strat):
    """Build an #if/#elif*/#else? node whose bodies are lists of ``node_strat``."""
    branch = st.tuples(_COND, st.lists(node_strat, max_size=3))
    return st.builds(
        lambda branches, els: ("if", branches, els),
        st.lists(branch, min_size=1, max_size=3),
        st.one_of(st.none(), st.lists(node_strat, max_size=3)),
    )


_NODE = st.recursive(_BASE_NODE, _make_if, max_leaves=25)
_PROGRAM = st.lists(_NODE, min_size=1, max_size=6)


def _render_program(program):
    """Flatten a generated program tree to C source text, assigning unique
    sequential SENTINEL_<n> ids in source order. Returns (source, sentinel_count)."""
    lines = []
    counter = [0]

    def emit_nodes(nodes):
        for node in nodes:
            emit(node)

    def emit(node):
        kind = node[0]
        if kind == "sent":
            idx = counter[0]
            counter[0] += 1
            lines.append(f"SENTINEL_{idx}")
        elif kind == "define":
            lines.append(f"#define {node[1]} {node[2]}")
        elif kind == "undef":
            lines.append(f"#undef {node[1]}")
        elif kind == "if":
            branches, els = node[1], node[2]
            for i, (cond, body) in enumerate(branches):
                lines.append(f"#{'if' if i == 0 else 'elif'} {cond}")
                emit_nodes(body)
            if els is not None:
                lines.append("#else")
                emit_nodes(els)
            lines.append("#endif")

    emit_nodes(program)
    return "\n".join(lines) + "\n", counter[0]


_SUPPRESSED = [HealthCheck.filter_too_much, HealthCheck.too_slow]


# ----------------------------------------------------------------------------
# Property tests
# ----------------------------------------------------------------------------
# A shift operator must always be followed by a bare non-negative literal and
# the closing paren of its own ``({a} {op} {b})`` node. Anything else means the
# RHS is a subexpression again, which can evaluate negative.
_UNCONSTRAINED_SHIFT_RE = re.compile(r"(?:<<|>>) (?!\d+\))")

# ``<<`` additionally needs a literal LHS, so the whole node is ``(int << int)``.
# A computed LHS can already be 2**62, and gcc does not diagnose the overflow.
# Checked by counting: every ``<<`` in the string must belong to such a node.
_LSHIFT_NODE_RE = re.compile(r"\(\d+ << \d+\)")


def test_generator_never_emits_a_negative_shift_count():
    """The grammar must not produce a negative shift count.

    It is undefined in C, gcc and clang disagree on it, and gcc emits no
    diagnostic -- so the ``assume`` gate cannot discard it and
    ``test_single_if_expression_matches_compiler`` would fail or pass purely
    according to which compiler is installed. See the module docstring.

    Asserted structurally, not by sampling for a raising expression. Reaching a
    negative shift by chance is rare: 400 examples of the pre-fix grammar did
    not rediscover one, so a sampling guard would have passed against the very
    regression it exists to catch (the original counterexample took a full-suite
    run to surface). The structural assertion fails immediately and
    deterministically instead. Needs no compiler, so it holds on a
    toolchain-less box too.
    """
    unconstrained = [op for op in ("<<", ">>") if op in _BINOPS]
    assert not unconstrained, (
        f"{unconstrained} are in _BINOPS, whose RHS is an arbitrary "
        f"subexpression that can evaluate negative. Shifts must come from the "
        f"rshift_/lshift_ strategies in _expr, which pin the count to a "
        f"non-negative literal."
    )

    # Second line of defence: a *new* strategy could reintroduce a shift with a
    # subexpression RHS without touching _BINOPS.
    samples = _sample_expressions(300)
    assert any("<<" in s or ">>" in s for s in samples), (
        "no shift appeared in 300 samples — the shift strategies are unreachable, so this guard would be vacuous"
    )
    offenders = [s for s in samples if _UNCONSTRAINED_SHIFT_RE.search(s)]
    assert not offenders, (
        f"generated shifts whose RHS is not a bare non-negative literal, so the "
        f"count can evaluate negative: {offenders[:3]!r}"
    )

    # ``<<`` needs the stronger invariant: a literal LHS too, since gcc does not
    # diagnose left-shift overflow (see the module docstring).
    assert any("<<" in s for s in samples), (
        "no left shift appeared in 300 samples — the lshift_ guard below would be vacuous"
    )
    computed_lhs = [s for s in samples if s.count("<<") != len(_LSHIFT_NODE_RE.findall(s))]
    assert not computed_lhs, (
        f"generated left shifts whose LHS is not a bare literal, so the result "
        f"can silently exceed intmax_t: {computed_lhs[:3]!r}"
    )


@requires_functional_compiler
def test_oracle_accepts_a_trivially_well_defined_program():
    """The subset gate must pass the simplest possible well-defined input.

    The gate treats ANY stderr as "outside the well-defined subset", so a
    diagnostic that fires on every input starves the property tests: all
    examples get discarded and Hypothesis dies with Unsatisfiable instead
    of testing anything. The one known way to trip this is naming the
    candidate file ``.c`` while invoking the C++ driver — clang++ then
    emits an unconditional driver warning ("treating 'c' input as 'c++'
    ... deprecated") on every single run, while g++ stays silent, making
    the whole module's verdict a function of which compiler won the
    functional-compiler probe. Both drivers preprocess the candidate in
    C++ mode regardless, so the oracle must present a C++-suffixed file
    and this test must pass under either compiler.
    """
    ok, ids = _compiler_active_sentinels("#if 1\nSENTINEL_0\n#endif\n")
    assert ok, (
        "the oracle discarded '#if 1' — its compiler invocation emits a "
        "diagnostic on every input, so every Hypothesis example gets "
        "assume()d away and the property tests are vacuous"
    )
    assert ids == {0}

    ok, names = _compiler_macro_names("#define CTGEN_A 1\n")
    assert ok, "the -dM -E oracle discarded a bare well-formed #define"
    assert names == {"CTGEN_A"}


@requires_functional_compiler
@settings(max_examples=_EXPR_EXAMPLES, deadline=None, suppress_health_check=_SUPPRESSED)
@given(expr=_expr(3))
def test_single_if_expression_matches_compiler(expr):
    """A single ``#if <expr>`` wrapping one sentinel must survive iff the real
    compiler keeps it. Filters out any expression the compiler flags as outside
    the well-defined subset (overflow / shift UB / div-by-zero)."""
    src = f"#if {expr}\nSENTINEL_0\n#endif\n"
    ok, expected = _compiler_active_sentinels(src)
    assume(ok)
    got = _ct_active_sentinels(src)
    assert got == expected, f"expr={expr!r} compiler={expected} compiletools={got}"


@requires_functional_compiler
@settings(max_examples=_PROG_EXAMPLES, deadline=None, suppress_health_check=_SUPPRESSED)
@given(program=_PROGRAM)
def test_structured_program_active_lines_match_compiler(program):
    """A nested tree of directives with interspersed sentinels must yield the
    exact same surviving-sentinel set under compiletools as under the real
    compiler -- the core active-line oracle over the branch state machine."""
    src, count = _render_program(program)
    assume(count > 0)  # only meaningful when at least one sentinel exists
    ok, expected = _compiler_active_sentinels(src)
    assume(ok)
    got = _ct_active_sentinels(src)
    assert got == expected, f"\n--- source ---\n{src}\ncompiler={expected} compiletools={got}"


@requires_functional_compiler
@settings(max_examples=_MACRO_EXAMPLES, deadline=None, suppress_health_check=_SUPPRESSED)
@given(
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("define"), st.sampled_from(_NAMES), st.integers(0, 64)),
            st.tuples(st.just("undef"), st.sampled_from(_NAMES), st.none()),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_macro_dump_name_set_matches_compiler(ops):
    """Macro-dump oracle: after a sequence of top-level #define/#undef, the set
    of CTGEN_ macro *names* still defined must match ``<cxx> -dM -E``."""
    lines = []
    for kind, name, val in ops:
        if kind == "define":
            lines.append(f"#define {name} {val}")
        else:
            lines.append(f"#undef {name}")
    src = "\n".join(lines) + "\n"

    ok, expected_names = _compiler_macro_names(src)
    assume(ok)

    _active, macros = _ct_process(src)
    got_names = {str(k) for k in macros if str(k).startswith("CTGEN_")}
    assert got_names == expected_names, f"\n--- source ---\n{src}\ncompiler={expected_names} compiletools={got_names}"


# ----------------------------------------------------------------------------
# KNOWN DIVERGENCES -- confirmed real differences between SimplePreprocessor
# and C. Recorded as *strict* xfails: they fail today (documenting the bug) and
# will turn into XPASS *failures* the moment the preprocessor is fixed, forcing
# whoever fixes it to convert these to plain assertions. Fixes are owned by a
# separate preprocessor branch; this test only tracks the divergences.
#
# Each case runs ONLY through compiletools and asserts the C-standard-correct
# answer, so it needs no compiler and cannot be masked by a missing toolchain.
# ----------------------------------------------------------------------------
def _ct_if_active(expr: str) -> bool:
    """True iff compiletools keeps the sentinel guarded by ``#if <expr>``."""
    src = f"#if {expr}\nSENTINEL_0\n#endif\n"
    return 0 in _ct_active_sentinels(src)


_KNOWN_DIVERGENCES = [
    pytest.param(
        "-1 > 0u",
        True,
        id="signed-unsigned-comparison",
        marks=pytest.mark.xfail(
            strict=True,
            reason="U/L suffixes are stripped, so signedness is lost: C evaluates "
            "'-1 > 0u' as an unsigned comparison (true), SimplePreprocessor as "
            "signed (false).",
        ),
    ),
    pytest.param(
        "9223372036854775807 + 1 < 0",
        True,
        id="intmax-overflow-wraparound",
        marks=pytest.mark.xfail(
            strict=True,
            reason="C #if arithmetic is intmax_t and wraps on overflow "
            "(INTMAX_MAX + 1 -> INTMAX_MIN < 0, true); SimplePreprocessor uses "
            "Python bignums, so no wraparound (false).",
        ),
    ),
    pytest.param(
        "08",
        False,
        id="invalid-octal-accepted",
        marks=pytest.mark.xfail(
            strict=True,
            reason="'08' is an invalid octal constant that C rejects (line "
            "dropped); SimplePreprocessor silently accepts it as non-zero and "
            "keeps the line.",
        ),
    ),
    # NOTE: the former "deep-nesting-recursionerror" divergence was fixed by the
    # preprocessor-bugfix change (explicit _MAX_DEPTH guard + PreprocessorExpressionError
    # instead of a swallowed RecursionError->false). Deeply-nested valid expressions now
    # evaluate correctly, so the case is no longer a known divergence and has been
    # removed from this list. See test_simple_preprocessor.py for its regression tests.
]


@requires_functional_compiler
@pytest.mark.parametrize("expr, c_correct_active", _KNOWN_DIVERGENCES)
def test_known_divergence_is_still_present(expr, c_correct_active):
    """Assert the C-standard-correct outcome. Each is a strict xfail today: the
    assertion fails because SimplePreprocessor diverges. When the preprocessor
    is fixed the assertion passes -> XPASS -> strict failure -> we get alerted
    to delete the xfail marker and (if desired) fold the case into the property
    tests' allowed grammar."""
    assert _ct_if_active(expr) is c_correct_active
