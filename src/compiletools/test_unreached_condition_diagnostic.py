"""A condition the settled build never reaches must not be reported.

The deferral added for R8 holds an "assuming false" report until the macro
state settles and retracts it when a later pass evaluates the same condition.
It does not cover the other way a pass can supersede an early record: the
settled state can make the *enclosing* branch dead, so the condition is never
evaluated again — neither resolved nor re-recorded — and the early record
survives the flush and prints against a build that never compiled the line.

Three skips can reach a directive that way: the dead-parent skip in the ``#if``
handler, the dead-parent skip in the ``#elif`` handler, and the ``#elif``
short-circuit once an earlier branch of the chain has been taken.  Each is
covered below, together with the mirror flip (parent dead early, live at
settle) where the warning is correct and must survive.
"""

import os
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
from compiletools.simple_preprocessor import SimplePreprocessor, converging

# The gate every arm shares: function-like, so it is unevaluable until
# READMACROS or an include has supplied its definition.
_GATE = "EXTLIB_AT_LEAST(2, 0)"
_MACROS_HEADER = "#define EXTLIB_AT_LEAST(maj, min) ((maj) <= 2)\n"


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


class TestASupersededPassIsRetractedBySkipping:
    """Two passes over one file, the second from a settled macro state.

    Driving the preprocessor directly is what lets each of the three skips be
    named: the end-to-end arms below show the user-visible answer, these show
    which handler produced it.  Both passes run inside one ``converging``
    region, so the first pass's record is deferred exactly as magicflags defers
    it, and the flush at region exit is the moment of truth.
    """

    @staticmethod
    def _two_passes(source: str, tmpdir, settled: dict) -> BuildContext:
        """Pass 1 knows nothing; pass 2 knows ``settled``. Returns the shared context."""
        file_result, context = _analyze(source, str(tmpdir))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
            assert context.pending_preprocessor_warnings, "pass 1 recorded nothing to retract"
            SimplePreprocessor(settled, verbose=1).process_structured(file_result, context)
        return context

    def _flushed(self, source: str, tmpdir, settled: dict, capsys) -> str:
        self._two_passes(source, tmpdir, settled)
        return capsys.readouterr().err

    _IF_DEAD_PARENT = f"#if !USE_NEW\n#if {_GATE}\nMARKER_INNER;\n#endif\n#endif\n"

    _ELIF_DEAD_PARENT = f"#if !USE_NEW\n#if 0\nMARKER_NEVER;\n#elif {_GATE}\nMARKER_INNER;\n#endif\n#endif\n"

    _ELIF_SHORT_CIRCUIT = f"#if USE_NEW\nMARKER_FIRST;\n#elif {_GATE}\nMARKER_INNER;\n#endif\n"

    def test_an_if_under_a_parent_that_died_is_not_reported(self, tmp_path, capsys):
        """Site 1: the dead-parent skip in the ``#if`` handler.

        Pass 1 has ``USE_NEW`` unknown, so ``!USE_NEW`` is live and the inner
        gate records pending. At settle ``USE_NEW`` is 1 and the inner ``#if``
        is skipped without evaluating, so nothing retracts it but the skip.
        """
        stderr = self._flushed(self._IF_DEAD_PARENT, tmp_path, {sz.Str("USE_NEW"): sz.Str("1")}, capsys)
        assert "cannot evaluate" not in stderr, stderr

    def test_an_elif_under_a_parent_that_died_is_not_reported(self, tmp_path, capsys):
        """Site 2: the dead-parent skip in the ``#elif`` handler."""
        stderr = self._flushed(self._ELIF_DEAD_PARENT, tmp_path, {sz.Str("USE_NEW"): sz.Str("1")}, capsys)
        assert "cannot evaluate" not in stderr, stderr

    def test_an_elif_an_earlier_branch_short_circuited_is_not_reported(self, tmp_path, capsys):
        """Site 3: the chain took an earlier branch, so the ``#elif`` is never consulted.

        No parked corpus reaches this one. Pass 1 reads ``USE_NEW`` as 0 and
        falls through to the ``#elif``; at settle the ``#if`` is taken, and the
        ``#elif`` lands in the handler's final ``else`` with
        ``any_condition_met`` already true.
        """
        stderr = self._flushed(self._ELIF_SHORT_CIRCUIT, tmp_path, {sz.Str("USE_NEW"): sz.Str("1")}, capsys)
        assert "cannot evaluate" not in stderr, stderr

    @pytest.mark.parametrize(
        "source",
        [_IF_DEAD_PARENT, _ELIF_DEAD_PARENT, _ELIF_SHORT_CIRCUIT],
        ids=["if-dead-parent", "elif-dead-parent", "elif-short-circuit"],
    )
    def test_a_settled_state_that_still_reaches_the_gate_still_reports(self, source, tmp_path, capsys):
        """The falsifier for all three sites: retracting on skip must not become suppression.

        Same three sources, but the settled state leaves the gate reachable —
        ``USE_NEW`` 0 keeps ``!USE_NEW`` live and keeps the ``#if USE_NEW``
        chain falling through to its ``#elif``. The gate is genuinely
        unevaluable in the settled build, so the report is correct and has to
        survive.
        """
        stderr = self._flushed(source, tmp_path, {sz.Str("USE_NEW"): sz.Str("0")}, capsys)
        assert f"cannot evaluate '#if {_GATE}'" in stderr or f"cannot evaluate '#elif {_GATE}'" in stderr, stderr

    def test_the_record_is_dropped_rather_than_marked_resolved(self, tmp_path):
        """The two stores are asymmetric, and the asymmetry is the design.

        Marking the condition resolved would be sticky, and a third pass that
        reached it and still could not evaluate it would then be silenced —
        which is exactly the falsifier above. Only the pending record goes.
        """
        context = self._two_passes(self._IF_DEAD_PARENT, tmp_path, {sz.Str("USE_NEW"): sz.Str("1")})
        assert context.pending_preprocessor_warnings == {}
        assert all(_GATE not in condition for _, _, condition in context.resolved_preprocessor_conditions)

    def test_a_skip_outside_a_convergence_stays_silent(self, tmp_path, capsys):
        """ct-headertree and ct-filelist never defer, so the skip has nothing to do.

        The dead branch is silent for the older reason — nothing evaluates, so
        nothing warns — and the new retraction must not invent a report or a
        store entry on a context that never armed deferral.
        """
        file_result, context = _analyze(self._IF_DEAD_PARENT, str(tmp_path))
        SimplePreprocessor({sz.Str("USE_NEW"): sz.Str("1")}, verbose=1).process_structured(file_result, context)
        assert capsys.readouterr().err == ""
        assert context.pending_preprocessor_warnings == {}


class TestTheFourArmsEndToEnd(tb.BaseCompileToolsTestCase):
    """The parked R9 corpus, run through ``ct-magicflags``' own convergence.

    All four share the delivery shape the residual was filed against:
    ``//#READMACROS=macros.h`` in the source, with ``macros.h`` in no include
    graph, and the gate inside an included ``gate.h``.  ``deadparent`` and
    ``nested_live`` differ by one character in ``macros.h`` — the parent's
    liveness flip is the whole variable.
    """

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _run(self, gate_header: str, macros_header: str, capsys) -> tuple[str, str]:
        files = uth.write_sources(
            {
                "src/macros.h": macros_header,
                "src/gate.h": gate_header,
                "src/main.cpp": '//#READMACROS=macros.h\n#include "gate.h"\nint main() { return 0; }\n',
            },
            target_dir=self._tmpdir,
        )
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(str(files["src/main.cpp"]))
        flags = " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))
        return flags, capsys.readouterr().err

    _FLAT_GATE = f"#pragma once\n#if {_GATE}\n//#CXXFLAGS=-DINNER_TRUE\n#endif\n//#CXXFLAGS=-DGATE_SEEN\n"
    _NESTED_GATE = (
        f"#pragma once\n#if !USE_NEW\n#if {_GATE}\n//#CXXFLAGS=-DINNER_TRUE\n#endif\n#endif\n//#CXXFLAGS=-DGATE_SEEN\n"
    )
    _LIVE_PARENT_GATE = (
        f"#pragma once\n#if USE_NEW\n#if {_GATE}\n//#CXXFLAGS=-DINNER_TRUE\n#endif\n#endif\n//#CXXFLAGS=-DGATE_SEEN\n"
    )

    def test_control_takes_the_inner_branch_silently(self, capsys):
        flags, stderr = self._run(self._FLAT_GATE, "#define USE_NEW 1\n" + _MACROS_HEADER, capsys)
        assert flags == "-DINNER_TRUE -DGATE_SEEN", flags
        assert "cannot evaluate" not in stderr, stderr

    def test_nested_live_takes_the_inner_branch_silently(self, capsys):
        """The parent stays live at settle, so nothing is skipped and R8 covers it."""
        flags, stderr = self._run(self._NESTED_GATE, "#define USE_NEW 0\n" + _MACROS_HEADER, capsys)
        assert flags == "-DINNER_TRUE -DGATE_SEEN", flags
        assert "cannot evaluate" not in stderr, stderr

    def test_deadparent_is_silent(self, capsys):
        """R9 itself: one character apart from ``nested_live``, and it used to warn."""
        flags, stderr = self._run(self._NESTED_GATE, "#define USE_NEW 1\n" + _MACROS_HEADER, capsys)
        assert flags == "-DGATE_SEEN", flags
        assert "cannot evaluate" not in stderr, stderr

    def test_liveparent_still_warns(self, capsys):
        """The end-to-end falsifier: the settled build does reach an unevaluable gate.

        ``macros.h`` defines ``USE_NEW`` and deliberately does NOT define the
        gate macro, so ``#if USE_NEW`` is dead in pass 1 and live at settle,
        and the inner condition is unevaluable in the build that ships.
        """
        flags, stderr = self._run(self._LIVE_PARENT_GATE, "#define USE_NEW 1\n", capsys)
        assert flags == "-DGATE_SEEN", flags
        assert f"cannot evaluate '#if {_GATE}'" in stderr, stderr
