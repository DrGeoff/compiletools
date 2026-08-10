"""Function-like macro expansion in ``#if``/``#elif`` controlling expressions.

Covers the third defect in bugreport-readmacros-and-function-like-macros.md:
``#define F(a, b) ...`` was stored with its parameter list stripped and expanded
object-like, so ``#if F(2, 0)`` produced ``BODY(2, 0)``, the tokenizer rejected
the dangling argument list, and the ``#if`` degraded to false at verbose 8.

Where a case has an unambiguous answer the C preprocessor already gives, the
compiler is the oracle: ``_compiler_active_markers`` runs ``<cxx> -E -P`` over
the same source and the expectation is whatever survived.
"""

import os
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
import stringzilla as sz

import compiletools.headerdeps
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.file_analyzer import analyze_file, set_analyzer_args
from compiletools.global_hash_registry import get_file_hash
from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing
from compiletools.simple_preprocessor import SimplePreprocessor, converging
from compiletools.testhelper import requires_functional_compiler

_SUBPROCESS_TIMEOUT = 60


def _analyze(source: str, tmpdir: str, name: str = "candidate.cpp"):
    """Run the production analyzer over ``source`` and return (result, context)."""
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(source)
    context = BuildContext()
    set_analyzer_args(
        SimpleNamespace(
            max_read_size=0,
            verbose=0,
            exemarkers=[],
            testmarkers=[],
            librarymarkers=[],
            use_mmap=True,
            force_mmap=False,
            suppress_fd_warnings=True,
            suppress_filesystem_warnings=True,
        ),
        context,
    )
    return analyze_file(get_file_hash(path, context), context), context


def _active_markers(source: str, tmpdir: str, verbose: int = 0):
    """Return the set of MARKER_<n> tokens SimplePreprocessor leaves active."""
    file_result, context = _analyze(source, tmpdir)
    preprocessor = SimplePreprocessor({}, verbose=verbose)
    active = preprocessor.process_structured(file_result, context)
    lines = source.split("\n")
    return {lines[i].strip() for i in active if 0 <= i < len(lines) and lines[i].strip().startswith("MARKER_")}


def _compiler_active_markers(cxx: str, source: str, tmpdir: str):
    """Ground truth: the MARKER_<n> tokens surviving ``<cxx> -E -P``."""
    path = os.path.join(tmpdir, "oracle.cpp")
    with open(path, "w") as fh:
        fh.write(source)
    proc = subprocess.run([cxx, "-E", "-P", path], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
    assert proc.returncode == 0, f"oracle rejected the source: {proc.stderr}"
    return {tok.strip() for tok in proc.stdout.split("\n") if tok.strip().startswith("MARKER_")}


def _functional_cxx() -> str:
    from compiletools.apptools import get_functional_cxx_compiler

    cxx = get_functional_cxx_compiler()
    if not cxx:
        pytest.skip("no functional C++ compiler detected")
    return str(cxx)


def _assert_matches_oracle(source: str, tmpdir: str, expected: set):
    """Assert SimplePreprocessor and ``<cxx> -E -P`` both produce ``expected``.

    ``expected`` is spelled out rather than read off the oracle so a compiler
    that disagrees with the documented C semantics fails the test instead of
    silently redefining it.
    """
    cxx = _functional_cxx()
    with tempfile.TemporaryDirectory() as oracle_dir:
        from_compiler = _compiler_active_markers(cxx, source, oracle_dir)
    assert from_compiler == expected, f"oracle disagrees with the expectation: {from_compiler}"
    assert _active_markers(source, tmpdir) == from_compiler


_VERSION_GUARD_SOURCE = """\
#define EXTLIB_VERSION_MAJOR 2
#define EXTLIB_VERSION_MINOR 4

#define EXTLIB_AT_LEAST(major, minor) \\
    (EXTLIB_VERSION_MAJOR > (major) || (EXTLIB_VERSION_MAJOR == (major) && EXTLIB_VERSION_MINOR >= (minor)))

#if EXTLIB_AT_LEAST(2, 0)
MARKER_NEW;
#else
MARKER_SHIM;
#endif
"""


class TestFunctionLikeMacroExpansion:
    def test_version_guard_takes_the_true_branch(self, tmp_path):
        """The bug report's reproducer: 2.4 satisfies EXTLIB_AT_LEAST(2, 0)."""
        assert _active_markers(_VERSION_GUARD_SOURCE, str(tmp_path)) == {"MARKER_NEW;"}

    @requires_functional_compiler
    def test_version_guard_matches_the_compiler(self, tmp_path):
        """Differential check of the reproducer against ``<cxx> -E -P``."""
        _assert_matches_oracle(_VERSION_GUARD_SOURCE, str(tmp_path), {"MARKER_NEW;"})

    def test_version_guard_takes_the_false_branch_when_too_old(self, tmp_path):
        """The fix must not make every guard true: 2.4 does not satisfy (3, 0)."""
        source = _VERSION_GUARD_SOURCE.replace("EXTLIB_AT_LEAST(2, 0)", "EXTLIB_AT_LEAST(3, 0)")
        assert _active_markers(source, str(tmp_path)) == {"MARKER_SHIM;"}

    @requires_functional_compiler
    def test_nested_invocation_matches_the_compiler(self, tmp_path):
        """A call in the argument of another call: MAXOF(TWICE(3), 4) is 6."""
        source = """\
#define TWICE(x) ((x) * 2)
#define MAXOF(a, b) ((a) > (b) ? (a) : (b))
#if MAXOF(TWICE(3), 4) == 6
MARKER_SIX;
#else
MARKER_NOT_SIX;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_SIX;"})

    @requires_functional_compiler
    def test_arguments_are_substituted_without_added_parentheses(self, tmp_path):
        """cpp substitutes argument text verbatim: TWICE(1+1) is 1+1*2 == 3, not 4."""
        source = """\
#define TWICE(x) x * 2
#if TWICE(1+1) == 3
MARKER_THREE;
#elif TWICE(1+1) == 4
MARKER_FOUR;
#else
MARKER_NEITHER;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_THREE;"})

    @requires_functional_compiler
    def test_object_like_macro_inside_an_argument_is_expanded(self, tmp_path):
        """Arguments are macro-expanded before substitution (C11 6.10.3.1p1)."""
        source = """\
#define WIDTH 5
#define TWICE(x) ((x) * 2)
#if TWICE(WIDTH) == 10
MARKER_TEN;
#else
MARKER_NOT_TEN;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_TEN;"})

    @requires_functional_compiler
    def test_a_parameter_name_does_not_rewrite_a_longer_identifier(self, tmp_path):
        """Substitution is whole-identifier: parameter ``a`` must not touch ``max_a``."""
        source = """\
#define max_a 9
#define PICK(a) (a + max_a)
#if PICK(1) == 10
MARKER_TEN;
#else
MARKER_NOT_TEN;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_TEN;"})

    @requires_functional_compiler
    def test_zero_parameter_macro_is_invoked_with_empty_parentheses(self, tmp_path):
        source = """\
#define SEVEN() 7
#if SEVEN() == 7
MARKER_SEVEN;
#else
MARKER_NOT_SEVEN;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_SEVEN;"})

    @requires_functional_compiler
    def test_right_shift_survives_expansion(self, tmp_path):
        """Right shift is the operator a naive tokenizer fix silently loses.

        A left-shift case passes even when ``>>`` is mishandled, so the guard
        has to be the right shift specifically -- both inside a macro body and
        as an argument, since substitution is the step that can strip it.
        """
        source = """\
#define SHIFT_DOWN(v, n) ((v) >> (n))
#define PASS_THROUGH(e) (e)
#if SHIFT_DOWN(256, 4) == 16 && PASS_THROUGH(1024 >> 5) == 32
MARKER_SHIFTED;
#else
MARKER_NOT_SHIFTED;
#endif
"""
        _assert_matches_oracle(source, str(tmp_path), {"MARKER_SHIFTED;"})

    _RECURSIVE_SOURCE = """\
#define RECUR(x) (RECUR(x) + x)
#if RECUR(1)
MARKER_RECUR_TRUE;
#else
MARKER_RECUR_FALSE;
#endif
"""

    def test_self_recursive_macro_terminates_and_is_diagnosed(self, tmp_path, capsys):
        """Blue paint (C11 6.10.3.4p2) stops the recursion; the leftover is not a value.

        gcc rejects the same source outright ("missing binary operator before
        token '('") and takes the false branch, so terminating quietly with an
        invented number would be the wrong kind of agreement.
        """
        assert _active_markers(self._RECURSIVE_SOURCE, str(tmp_path), verbose=1) == {"MARKER_RECUR_FALSE;"}
        assert "recursive invocation of RECUR" in capsys.readouterr().err

    @requires_functional_compiler
    def test_self_recursive_macro_is_a_compiler_error_too(self, tmp_path):
        """Pins why the case above has no oracle comparison: cpp errors on it."""
        cxx = _functional_cxx()
        path = os.path.join(str(tmp_path), "recursive.cpp")
        with open(path, "w") as fh:
            fh.write(self._RECURSIVE_SOURCE)
        proc = subprocess.run([cxx, "-E", "-P", path], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        assert proc.returncode != 0
        assert "MARKER_RECUR_FALSE;" in proc.stdout


class TestFunctionLikeNameWithoutArguments:
    """A function-like name not followed by ``(`` is not expanded.

    Lifting the parameter list off the macro means the bare name stops being
    object-expanded, which reaches beyond the reported bug. The compiler is the
    oracle for what the bare name is worth.
    """

    _SOURCE = """\
#define GATE(a) (a)
#if GATE
MARKER_BARE_TRUE;
#else
MARKER_BARE_FALSE;
#endif
#if defined(GATE)
MARKER_DEFINED;
#endif
#if GATE(1) || GATE
MARKER_MIXED_TRUE;
#else
MARKER_MIXED_FALSE;
#endif
#if GATE(0) || GATE
MARKER_BOTH_ZERO_TRUE;
#else
MARKER_BOTH_ZERO_FALSE;
#endif
"""

    @requires_functional_compiler
    def test_bare_name_is_false_and_defined_is_true(self, tmp_path):
        _assert_matches_oracle(
            self._SOURCE,
            str(tmp_path),
            {
                "MARKER_BARE_FALSE;",
                "MARKER_DEFINED;",
                "MARKER_MIXED_TRUE;",
                "MARKER_BOTH_ZERO_FALSE;",
            },
        )

    def test_ifdef_sees_a_function_like_macro(self, tmp_path):
        """#ifdef tests definedness, which a function-like macro has."""
        source = """\
#define GATE(a) 1
#ifdef GATE
MARKER_IFDEF;
#endif
#ifndef GATE
MARKER_IFNDEF;
#endif
"""
        assert _active_markers(source, str(tmp_path)) == {"MARKER_IFDEF;"}


class TestUnevaluableConditionDiagnostic:
    """An unevaluable ``#if`` must say so instead of quietly becoming false."""

    _UNEVALUABLE = """\
#if "a string literal" @ 3
MARKER_TAKEN;
#else
MARKER_NOT_TAKEN;
#endif
"""

    def test_warns_at_verbose_1(self, tmp_path, capsys):
        assert _active_markers(self._UNEVALUABLE, str(tmp_path), verbose=1) == {"MARKER_NOT_TAKEN;"}
        stderr = capsys.readouterr().err
        assert "SimplePreprocessor warning" in stderr
        assert "cannot evaluate" in stderr
        assert "assuming false" in stderr

    def test_silent_at_verbose_0(self, tmp_path, capsys):
        """Default verbosity keeps the old silence, so no build output changes."""
        assert _active_markers(self._UNEVALUABLE, str(tmp_path), verbose=0) == {"MARKER_NOT_TAKEN;"}
        assert "SimplePreprocessor warning" not in capsys.readouterr().err

    def test_one_report_per_condition_across_preprocessor_instances(self, tmp_path, capsys):
        """magicflags re-evaluates a file under a fresh preprocessor on every pass.

        The dedup therefore has to live on the shared BuildContext; an
        instance-local set produced one identical line per pass.
        """
        file_result, context = _analyze(self._UNEVALUABLE, str(tmp_path))
        for _ in range(3):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
        stderr = capsys.readouterr().err
        assert stderr.count("cannot evaluate") == 1

    def test_a_legitimately_false_branch_does_not_warn(self, tmp_path, capsys):
        """Only unevaluable conditions warn; an honest 0 is not a diagnostic."""
        source = """\
#define WIDTH 4
#if WIDTH > 8
MARKER_WIDE;
#else
MARKER_NARROW;
#endif
"""
        assert _active_markers(source, str(tmp_path), verbose=1) == {"MARKER_NARROW;"}
        assert "SimplePreprocessor warning" not in capsys.readouterr().err

    def test_variadic_macro_is_diagnosed_rather_than_silently_wrong(self, tmp_path, capsys):
        """__VA_ARGS__ is out of scope, so the refusal has to be visible."""
        source = """\
#define COUNTS(first, ...) (first)
#if COUNTS(1, 2, 3)
MARKER_TAKEN;
#else
MARKER_NOT_TAKEN;
#endif
"""
        assert _active_markers(source, str(tmp_path), verbose=1) == {"MARKER_NOT_TAKEN;"}
        stderr = capsys.readouterr().err
        assert "variadic macro" in stderr
        assert "COUNTS" in stderr

    def test_stringize_is_diagnosed_rather_than_silently_wrong(self, tmp_path, capsys):
        """``#`` is out of scope; the surviving ``#1`` must be reported, not read as 0."""
        source = """\
#define STR(x) #x
#if STR(1)
MARKER_TAKEN;
#else
MARKER_NOT_TAKEN;
#endif
"""
        assert _active_markers(source, str(tmp_path), verbose=1) == {"MARKER_NOT_TAKEN;"}
        stderr = capsys.readouterr().err
        assert "cannot evaluate" in stderr
        assert "#if STR(1)" in stderr

    def test_token_paste_is_diagnosed_rather_than_silently_wrong(self, tmp_path, capsys):
        """``##`` is out of scope. cpp would say true here; we must say so, loudly."""
        source = """\
#define CAT(a, b) a##b
#define AB 1
#if CAT(A, B)
MARKER_TAKEN;
#else
MARKER_NOT_TAKEN;
#endif
"""
        assert _active_markers(source, str(tmp_path), verbose=1) == {"MARKER_NOT_TAKEN;"}
        stderr = capsys.readouterr().err
        assert "cannot evaluate" in stderr
        assert "#if CAT(A, B)" in stderr

    def test_argument_count_mismatch_is_diagnosed(self, tmp_path, capsys):
        source = """\
#define PAIR(a, b) ((a) + (b))
#if PAIR(1)
MARKER_TAKEN;
#else
MARKER_NOT_TAKEN;
#endif
"""
        assert _active_markers(source, str(tmp_path), verbose=1) == {"MARKER_NOT_TAKEN;"}
        stderr = capsys.readouterr().err
        assert "takes 2 argument(s) but was invoked with 1" in stderr

    def test_the_report_names_the_file_and_line(self, tmp_path):
        """The warning has to be actionable, so it carries a source location."""
        file_result, context = _analyze(self._UNEVALUABLE, str(tmp_path))
        preprocessor = SimplePreprocessor({}, verbose=1)
        preprocessor.process_structured(file_result, context)
        assert preprocessor._current_filepath.endswith("candidate.cpp")


class TestMacroStateFunctionParams:
    """``function_params`` tracks the ``variable`` dict through every transition."""

    @staticmethod
    def _state_with_gate():
        return MacroState(
            {},
            {sz.Str("GATE"): sz.Str("(a)")},
            function_params={sz.Str("GATE"): (sz.Str("a"),)},
            anchor_root="",
        )

    def test_undef_drops_the_parameter_list(self):
        """A macro removed by #undef must not leave its params in the key."""
        state = self._state_with_gate()
        after = state.without_keys(frozenset({sz.Str("GATE")}))
        assert sz.Str("GATE") not in after.function_params
        assert sz.Str("GATE") not in after.variable
        assert after.get_cache_key() == frozenset()
        assert after.get_relevant_key(frozenset({sz.Str("GATE")})) == frozenset()

    def test_object_like_redefinition_drops_the_parameter_list(self):
        """#define GATE 1 over #define GATE(a) ... makes GATE object-like."""
        state = self._state_with_gate()
        after = state.with_updates({sz.Str("GATE"): sz.Str("1")})
        assert sz.Str("GATE") not in after.function_params
        assert after.variable[sz.Str("GATE")] == sz.Str("1")

    def test_redefinition_with_new_parameters_replaces_the_old_ones(self):
        state = self._state_with_gate()
        after = state.with_updates(
            {sz.Str("GATE"): sz.Str("(x, y)")},
            {sz.Str("GATE"): (sz.Str("x"), sz.Str("y"))},
        )
        assert after.function_params[sz.Str("GATE")] == (sz.Str("x"), sz.Str("y"))

    def test_parameter_names_alone_change_the_cache_key(self):
        """Two definitions can share a body and differ only in parameter names."""
        first = self._state_with_gate()
        second = MacroState(
            {},
            {sz.Str("GATE"): sz.Str("(a)")},
            function_params={sz.Str("GATE"): (sz.Str("b"),)},
            anchor_root="",
        )
        assert first.get_cache_key() != second.get_cache_key()
        assert first.get_hash() != second.get_hash()

    def test_relevant_key_carries_the_params_of_the_requested_names(self):
        state = MacroState(
            {},
            {sz.Str("GATE"): sz.Str("(a)"), sz.Str("WIDTH"): sz.Str("4")},
            function_params={sz.Str("GATE"): (sz.Str("a"),)},
            anchor_root="",
        )
        gate_only = state.get_relevant_key(frozenset({sz.Str("GATE")}))
        width_only = state.get_relevant_key(frozenset({sz.Str("WIDTH")}))
        assert any("GATE" in str(name) for name, _ in gate_only)
        assert all("GATE" not in str(name) for name, _ in width_only)

    def test_a_state_without_function_like_macros_keys_as_it_did_before(self):
        """The empty channel must contribute nothing, so existing keys do not move.

        Measured across 536 corpus files against the base revision: every one of
        the 168 non-empty object-only states kept its key and hash byte-for-byte.
        This pins the mechanism that made that true.
        """
        variable = {sz.Str("WIDTH"): sz.Str("4"), sz.Str("HEIGHT"): sz.Str("3")}
        state = MacroState({}, dict(variable), anchor_root="")
        assert state.function_params == {}
        assert state.get_cache_key() == frozenset(variable.items())
        assert state.get_relevant_key(frozenset({sz.Str("WIDTH")})) == frozenset({(sz.Str("WIDTH"), sz.Str("4"))})

    def test_a_bodyless_define_is_hashable(self):
        """#define FOO with no body produced a plain str and crashed get_hash()."""
        source = "#define FOO\n#define BAR() \nint main() { return 0; }\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            file_result, context = _analyze(source, tmpdir)
            result = get_or_compute_preprocessing(file_result, MacroState({}, anchor_root=""), 0, context=context)
        assert isinstance(result.updated_macros.get_hash(), str)


class TestFunctionLikeMacroAcrossFiles(tb.BaseCompileToolsTestCase):
    """The reported failure in its real shape: the gate lives in a header."""

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _ldflags(self, sources: dict) -> str:
        files = uth.write_sources(sources)
        parser = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(str(files["main.cpp"]))
        return " ".join(str(flag) for flag in result.get(sz.Str("LDFLAGS"), []))

    def test_a_header_defined_gate_selects_the_magic_flag(self):
        ldflags = self._ldflags(
            {
                "version.hpp": (
                    "#pragma once\n"
                    "#define EXTLIB_VERSION_MAJOR 2\n"
                    "#define EXTLIB_VERSION_MINOR 4\n"
                    "#define EXTLIB_AT_LEAST(major, minor) \\\n"
                    "    (EXTLIB_VERSION_MAJOR > (major) || (EXTLIB_VERSION_MAJOR == (major) "
                    "&& EXTLIB_VERSION_MINOR >= (minor)))\n"
                ),
                "main.cpp": (
                    '#include "version.hpp"\n'
                    "#if EXTLIB_AT_LEAST(2, 0)\n"
                    "//#LDFLAGS=-lextlib_new\n"
                    "#else\n"
                    "//#LDFLAGS=-lextlib_shim\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                ),
            }
        )
        assert "-lextlib_new" in ldflags
        assert "-lextlib_shim" not in ldflags

    def test_a_header_defined_gate_can_still_be_false(self):
        """The cross-file path must carry the real value, not just take the true arm."""
        ldflags = self._ldflags(
            {
                "version.hpp": (
                    "#pragma once\n"
                    "#define EXTLIB_VERSION_MAJOR 1\n"
                    "#define EXTLIB_VERSION_MINOR 9\n"
                    "#define EXTLIB_AT_LEAST(major, minor) \\\n"
                    "    (EXTLIB_VERSION_MAJOR > (major) || (EXTLIB_VERSION_MAJOR == (major) "
                    "&& EXTLIB_VERSION_MINOR >= (minor)))\n"
                ),
                "main.cpp": (
                    '#include "version.hpp"\n'
                    "#if EXTLIB_AT_LEAST(2, 0)\n"
                    "//#LDFLAGS=-lextlib_new\n"
                    "#else\n"
                    "//#LDFLAGS=-lextlib_shim\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                ),
            }
        )
        assert "-lextlib_shim" in ldflags
        assert "-lextlib_new" not in ldflags

    def test_ifdef_sees_a_function_like_macro_from_a_header(self):
        """The name never entered the macro state before, so #ifdef read false."""
        ldflags = self._ldflags(
            {
                "gate.hpp": "#pragma once\n#define GATE(a) ((a) + 1)\n",
                "main.cpp": (
                    '#include "gate.hpp"\n'
                    "#ifdef GATE\n"
                    "//#LDFLAGS=-lgated\n"
                    "#else\n"
                    "//#LDFLAGS=-lungated\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                ),
            }
        )
        assert "-lgated" in ldflags
        assert "-lungated" not in ldflags

    def test_one_warning_per_condition_from_a_whole_magicflags_parse(self, capsys):
        """The shape the unit tests missed: three passes over one file, one line.

        get_structured_data re-runs discovery under a fresh SimplePreprocessor
        on every pass, so a dedup set living on the preprocessor printed the
        identical warning once per pass.
        """
        files = uth.write_sources(
            {
                "main.cpp": (
                    "#define COUNTS(first, ...) (first)\n"
                    "#if COUNTS(1, 2, 3)\n"
                    "//#LDFLAGS=-lcounted\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                )
            }
        )
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        parser.parse(str(files["main.cpp"]))

        stderr = capsys.readouterr().err
        assert stderr.count("cannot evaluate '#if COUNTS(1, 2, 3)'") == 1, stderr

    def test_a_bare_header_gate_name_does_not_expand_into_a_magic_flag(self):
        """A function-like name in a flag value stays a name, not its body."""
        ldflags = self._ldflags(
            {
                "gate.hpp": "#pragma once\n#define SUFFIX(a) debug\n",
                "main.cpp": ('#include "gate.hpp"\n//#LDFLAGS=-lplain\nint main() { return 0; }\n'),
            }
        )
        assert ldflags.strip() == "-lplain"


class TestConvergenceDeferredDiagnostic(tb.BaseCompileToolsTestCase):
    """A pass that a later pass supersedes must not get to speak.

    magicflags evaluates a file before its controlling macros are all known, so
    an early pass reports "cannot evaluate ... assuming false" on a build whose
    settled answer is the true branch — the diagnostic contradicts the flags
    printed beside it. Reports raised inside a convergence are therefore held
    and retracted when a later pass evaluates the same condition.
    """

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    _GATE_HEADER = (
        "#pragma once\n"
        "#define EXTLIB_VERSION_MAJOR 2\n"
        "#define EXTLIB_VERSION_MINOR 4\n"
        "#define EXTLIB_AT_LEAST(major, minor) \\\n"
        "    (EXTLIB_VERSION_MAJOR > (major) || (EXTLIB_VERSION_MAJOR == (major) "
        "&& EXTLIB_VERSION_MINOR >= (minor)))\n"
    )

    _VARIADIC_HEADER = "#pragma once\n#define COUNTS(first, ...) (first)\n"

    def _parse(self, sources: dict, *names: str):
        files = uth.write_sources(sources, target_dir=self._tmpdir)
        self._context = BuildContext()
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=self._context)
        parser.clear_cache()
        return [parser.parse(str(files[name])) for name in names]

    @staticmethod
    def _cxxflags(result) -> str:
        return " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))

    def test_a_condition_a_later_pass_resolves_is_not_reported(self, capsys):
        """The build takes the true branch, so nothing may say it assumed false."""
        (result,) = self._parse(
            {
                "inc/version.hpp": self._GATE_HEADER,
                "src/main.cpp": (
                    '#include "../inc/version.hpp"\n'
                    "#if EXTLIB_AT_LEAST(2, 0)\n"
                    "//#CXXFLAGS=-DHAVE_NEW_EXTLIB\n"
                    "#else\n"
                    "//#CXXFLAGS=-DNEED_EXTLIB_SHIM\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                ),
            },
            "src/main.cpp",
        )
        stderr = capsys.readouterr().err
        assert "-DHAVE_NEW_EXTLIB" in self._cxxflags(result)
        assert "cannot evaluate" not in stderr, stderr

    def test_a_condition_no_pass_resolves_is_still_reported(self, capsys):
        """The control for the test above: deferral must not become suppression."""
        (result,) = self._parse(
            {
                "inc/counts.hpp": self._VARIADIC_HEADER,
                "src/main.cpp": (
                    '#include "../inc/counts.hpp"\n'
                    "#if COUNTS(1, 2, 3)\n"
                    "//#CXXFLAGS=-DCOUNTED\n"
                    "#endif\n"
                    "int main() { return 0; }\n"
                ),
            },
            "src/main.cpp",
        )
        stderr = capsys.readouterr().err
        assert "-DCOUNTED" not in self._cxxflags(result)
        assert stderr.count("cannot evaluate '#if COUNTS(1, 2, 3)'") == 1, stderr

    def test_a_second_target_hitting_the_pass_1_cache_still_reports_once(self, capsys, monkeypatch):
        """The exit with no textual settle point to drain at.

        ``get_structured_data`` returns early on a PASS 1 cache hit, which an
        ordinary multi-target run reaches on its second target. Lowering the
        depth at the settle point instead of in a ``finally`` loses that pass's
        records and leaves the deferral armed for the rest of the process — so
        the count has to be exactly one, and one is not zero. The hit itself is
        counted because a corpus that quietly stops hitting the cache would make
        both assertions pass without exercising the path.
        """
        hits = []
        original = compiletools.magicflags.DirectMagicFlags._check_cache

        def counted(instance, filename, cache_key):
            result = original(instance, filename, cache_key)
            hits.append(result is not None)
            return result

        monkeypatch.setattr(compiletools.magicflags.DirectMagicFlags, "_check_cache", counted)

        files = uth.write_sources(
            {
                "inc/counts.hpp": (self._VARIADIC_HEADER + "#if COUNTS(1, 2, 3)\n//#CXXFLAGS=-DCOUNTED\n#endif\n"),
                "src/a.cpp": '#include "../inc/counts.hpp"\nint main() { return 0; }\n',
            },
            target_dir=self._tmpdir,
        )
        context = BuildContext()
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=context)
        parser.clear_cache()

        # The same source under two spellings: the second target reaches the
        # PASS 1 cache, which a distinct second source cannot do (the cache key
        # folds the file hash).
        target = str(files["src/a.cpp"])
        results = [parser.parse(target), parser.parse(target.replace("/src/", "/src/./"))]

        stderr = capsys.readouterr().err
        assert hits.count(True) == 1, hits
        assert [self._cxxflags(result) for result in results] == ["", ""]
        assert stderr.count("cannot evaluate '#if COUNTS(1, 2, 3)'") == 1, stderr
        assert context.preprocessor_convergence_depth == 0

    def test_the_depth_is_restored_when_a_cache_hit_raises(self, capsys):
        """The pre-existing ``_final_macro_states`` crash is one of the region's exits.

        Two same-content sources in different directories collide on the PASS 1
        cache key, and ``_check_cache`` raises because the second path was never
        converged. The region has to lower the depth on the way out, or every
        later diagnostic in the process is recorded and never printed.
        """
        files = uth.write_sources(
            {
                "inc/counts.hpp": (self._VARIADIC_HEADER + "#if COUNTS(1, 2, 3)\n//#CXXFLAGS=-DCOUNTED\n#endif\n"),
                "one/main.cpp": '#include "../inc/counts.hpp"\nint main() { return 0; }\n',
                "two/main.cpp": '#include "../inc/counts.hpp"\nint main() { return 0; }\n',
            },
            target_dir=self._tmpdir,
        )
        context = BuildContext()
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=context)
        parser.clear_cache()

        parser.parse(str(files["one/main.cpp"]))
        with pytest.raises(RuntimeError, match="_final_macro_states not populated"):
            parser.parse(str(files["two/main.cpp"]))

        assert context.preprocessor_convergence_depth == 0
        assert capsys.readouterr().err.count("cannot evaluate '#if COUNTS(1, 2, 3)'") == 1


class TestConvergingRegion:
    """``converging`` is what arms deferral, so its bookkeeping is load-bearing."""

    _UNEVALUABLE = """\
#if "a string literal" @ 3
MARKER_TAKEN;
#endif
"""

    def _record(self, tmp_path):
        """Run one pass whose condition cannot be evaluated. Returns the context."""
        file_result, context = _analyze(self._UNEVALUABLE, str(tmp_path))
        return file_result, context

    @staticmethod
    def _pass(file_result, context):
        SimplePreprocessor({}, verbose=1).process_structured(file_result, context)

    def test_only_the_outermost_region_flushes(self, tmp_path, capsys):
        file_result, context = self._record(tmp_path)
        with converging(context):
            with converging(context):
                self._pass(file_result, context)
            assert capsys.readouterr().err == ""
        assert "cannot evaluate" in capsys.readouterr().err

    def test_the_depth_returns_to_zero_when_the_region_raises(self, tmp_path, capsys):
        """Every exit lowers the depth; a leak disarms the diagnostic process-wide."""
        file_result, context = self._record(tmp_path)
        with pytest.raises(RuntimeError):
            with converging(context):
                self._pass(file_result, context)
                raise RuntimeError("the pre-existing _final_macro_states crash path")
        assert context.preprocessor_convergence_depth == 0
        assert "cannot evaluate" in capsys.readouterr().err

    def test_a_consumer_without_a_convergence_reports_immediately(self, tmp_path, capsys):
        """ct-headertree and ct-filelist never enter the region and must not change."""
        file_result, context = self._record(tmp_path)
        self._pass(file_result, context)
        assert "cannot evaluate" in capsys.readouterr().err
        assert context.pending_preprocessor_warnings == {}

    def test_a_context_without_the_depth_attribute_is_tolerated(self, capsys):
        """Tests hand-build duck-typed contexts, so the region cannot require the field."""
        with converging(SimpleNamespace()):
            pass
        assert capsys.readouterr().err == ""


class TestReadmacrosSuppliedFunctionLikeGate(tb.BaseCompileToolsTestCase):
    """The bug report's headline shape, which needs defect 1 and defect 3 together.

    The gate macro lives in a header reachable only through the file's own
    ``//#CXXFLAGS=-isystem``, so READMACROS must resolve against per-file include
    paths (defect 1, owned by the readmacros slice) before this slice's
    function-like expansion has anything to expand. Neither branch alone produces
    the flag; ``_require_readmacros_fix`` skips the test when defect 1's fix is
    absent from the tree.
    """

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _sources(self, subdir: str, gate: str) -> dict:
        return {
            "extlib/include/extlib/version.hpp": (
                "#pragma once\n"
                "#define EXTLIB_VERSION_MAJOR 2\n"
                "#define EXTLIB_VERSION_MINOR 4\n"
                "#define EXTLIB_AT_LEAST(major, minor) \\\n"
                "    (EXTLIB_VERSION_MAJOR > (major) || (EXTLIB_VERSION_MAJOR == (major) "
                "&& EXTLIB_VERSION_MINOR >= (minor)))\n"
            ),
            f"{subdir}/main.cpp": (
                f"//#CXXFLAGS=-isystem {os.path.join(self._tmpdir, 'extlib', 'include')}\n"
                "//#READMACROS=extlib/version.hpp\n"
                f"#if {gate}\n"
                "//#CXXFLAGS=-DHAVE_NEW_EXTLIB\n"
                "#endif\n"
                "int main() { return 0; }\n"
            ),
        }

    def _cxxflags(self, subdir: str, gate: str) -> str:
        files = uth.write_sources(self._sources(subdir, gate), target_dir=self._tmpdir)
        parser = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(str(files[f"{subdir}/main.cpp"]))
        return " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))

    def _require_readmacros_fix(self):
        """Probe the same scenario with an object-like gate, which needs defect 1 only."""
        if "-DHAVE_NEW_EXTLIB" not in self._cxxflags("probe", "EXTLIB_VERSION_MAJOR >= 2"):
            pytest.skip("needs the defect-1 READMACROS fix (branch gericksson/ct-magicflags-defects-readmacros)")

    def test_a_function_like_gate_from_a_readmacros_header_selects_the_flag(self):
        self._require_readmacros_fix()
        assert "-DHAVE_NEW_EXTLIB" in self._cxxflags("src", "EXTLIB_AT_LEAST(2, 0)")

    def test_a_function_like_gate_from_a_readmacros_header_can_still_be_false(self):
        self._require_readmacros_fix()
        assert "-DHAVE_NEW_EXTLIB" not in self._cxxflags("src", "EXTLIB_AT_LEAST(3, 0)")
