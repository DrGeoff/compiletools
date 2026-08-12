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
from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing
from compiletools.simple_preprocessor import SimplePreprocessor, converging, flush_pending_warnings

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
        assert all(_GATE not in condition for _, _, condition, _ in context.resolved_preprocessor_conditions)

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


class TestADeadOccurrenceDoesNotSilenceALiveOne:
    """One condition text spelled twice in a file, in every liveness combination.

    The retraction has to name the occurrence it belongs to. Keyed on
    ``(filepath, directive_type, condition)`` alone it does not: both
    occurrences share the key, so the dead one's skip deletes the live one's
    record and a genuinely unevaluable condition in the shipped build goes
    unreported. Found by ``evalchannel`` reviewing the key rather than the
    outcome — every arm in the class above uses one occurrence per file, so
    none of them can see it.

    All four combinations are one parametrize on purpose. ``dead_only`` is the
    control that stops the fix from passing by simply never retracting, and
    ``live_only`` is the control that stops it from passing by never recording;
    a change that satisfies ``live_then_dead`` alone breaks one of them.
    """

    _LIVE = f"#if {_GATE}\nMARKER_LIVE;\n#endif\n"
    _DEAD = f"#if 0\n#if {_GATE}\nMARKER_DEAD;\n#endif\n#endif\n"

    @pytest.mark.parametrize(
        "source, expected_reports",
        [
            (_LIVE, 1),
            (_DEAD, 0),
            (_LIVE + _DEAD, 1),
            (_DEAD + _LIVE, 1),
        ],
        ids=["live_only", "dead_only", "live_then_dead", "dead_then_live"],
    )
    def test_a_reachable_unevaluable_condition_is_reported_once(self, source, expected_reports, tmp_path, capsys):
        """Exactly one report per condition per file, and only when it is reachable.

        The count is asserted, not just the presence: the pending store now
        holds one entry per occurrence, so a flush that forgot to collapse them
        would report ``live_then_dead`` twice and still satisfy an ``in`` check.
        """
        file_result, context = _analyze(source, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
        stderr = capsys.readouterr().err
        assert stderr.count("cannot evaluate") == expected_reports, stderr

    def test_two_live_occurrences_hold_two_slots_and_still_report_one_line(self, tmp_path, capsys):
        """The once-per-condition-per-file contract now rests on the flush dedupe.

        Keying the pending store by occurrence is what fixes the collision, and
        it is also what puts this contract at risk: two live unevaluable
        occurrences of one condition occupy two slots where they used to share
        one, so nothing but the dedupe in ``flush_pending_warnings`` keeps the
        output at a single line.  Both halves are asserted, because they fail
        independently — removing the dedupe leaves every other test in this file
        green.

        Raised by ``evalchannel``'s re-probe as the case the occurrence key could
        break, the mirror of the case it fixes.
        """
        file_result, context = _analyze(self._LIVE + self._LIVE, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
            assert len(context.pending_preprocessor_warnings) == 2, context.pending_preprocessor_warnings
        stderr = capsys.readouterr().err
        assert stderr.count("cannot evaluate") == 1, stderr
        assert ":1: cannot evaluate" in stderr, stderr

    def test_the_flush_collapses_occurrences_without_help_from_the_context_memo(self):
        """The flush dedupes on its own, not only by leaning on the memo set.

        The case above cannot see this. ``BuildContext`` always carries
        ``warned_preprocessor_conditions``, and that memo incidentally collapses
        the second occurrence too, so deleting the flush's own dedupe leaves
        every context-driven test in this file green — measured, not assumed.
        ``flush_pending_warnings`` reads the memo with ``getattr(..., None)`` and
        owes the once-per-condition contract when it is absent.

        The memo-carrying context is the control: both paths must print one
        line, so the assertion is about the flush's own collapse rather than
        about which store happened to do the work.
        """
        entries = {
            ("f.cpp", "#if", _GATE, 1): "warn f.cpp:1",
            ("f.cpp", "#if", _GATE, 5): "warn f.cpp:5",
        }
        memoless = SimpleNamespace(pending_preprocessor_warnings=dict(entries))
        assert flush_pending_warnings(memoless) == 1

        with_memo = SimpleNamespace(
            pending_preprocessor_warnings=dict(entries),
            warned_preprocessor_conditions=set(),
        )
        assert flush_pending_warnings(with_memo) == 1

    @pytest.mark.parametrize("live_first", [True, False], ids=["live_then_dead", "dead_then_live"])
    def test_the_live_occurrence_is_the_one_named(self, live_first, tmp_path, capsys):
        """The surviving report must point at the reachable line, not the dead one.

        Counting reports is not enough: an implementation that keeps the wrong
        occurrence's message emits the right number of lines and sends the user
        to a branch that was never compiled. Both orders are checked so the
        assertion is about which occurrence is live, not which comes first.
        """
        source = self._LIVE + self._DEAD if live_first else self._DEAD + self._LIVE
        live_line = 1 if live_first else self._DEAD.count("\n") + 1
        file_result, context = _analyze(source, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
        stderr = capsys.readouterr().err
        assert f":{live_line}: cannot evaluate" in stderr, stderr


class TestARedefinitionDoesNotLetOneOccurrenceVouchForAnother:
    """One condition text spelled twice, with the macro state changed between.

    The liveness class above holds the macro state constant and varies only
    reachability, so it cannot see the resolved-store collision: a condition
    evaluable at one occurrence marked the TEXT resolved, and a later
    occurrence made genuinely unevaluable by an ``#undef`` plus a
    different-arity redefinition was silently discarded — the wrong branch
    shipped with empty stderr, the exact failure class the deferral exists to
    surface. Both orders are checked so the assertion is about which
    occurrence is unevaluable, not which comes first.
    """

    _TWO_ARG = "#define FOO(a,b) (a > b)\n#if FOO(2,1)\nMARKER_A;\n#endif\n"
    _ONE_ARG = "#define FOO(a) (a)\n#if FOO(2,1)\nMARKER_B;\n#endif\n"
    _REDEF = "#undef FOO\n"

    @pytest.mark.parametrize(
        "source, warn_line",
        [
            (_TWO_ARG + _REDEF + _ONE_ARG, 7),
            (_ONE_ARG + _REDEF + _TWO_ARG, 2),
        ],
        ids=["resolvable_then_unevaluable", "unevaluable_then_resolvable"],
    )
    def test_the_arity_mismatched_occurrence_is_reported(self, source, warn_line, tmp_path, capsys):
        """The unevaluable occurrence warns and names its own line.

        The line number is asserted, not just the count: a text-keyed resolved
        store fails the first order outright (zero reports), and a fix that
        recorded the report against the wrong occurrence would pass a bare
        count while sending the user to the branch that evaluated fine.
        """
        file_result, context = _analyze(source, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
        stderr = capsys.readouterr().err
        assert stderr.count("cannot evaluate") == 1, stderr
        assert f":{warn_line}: cannot evaluate '#if FOO(2,1)'" in stderr, stderr

    def test_a_redefinition_leaving_both_evaluable_stays_silent(self, tmp_path, capsys):
        """The must-not-move control: occurrence-keying must not invent reports.

        Same shape — ``#undef`` plus redefinition between two spellings of one
        condition — but the new definition keeps the arity, so both
        occurrences evaluate and neither may warn.
        """
        source = self._TWO_ARG + self._REDEF + "#define FOO(a,b) (a < b)\n#if FOO(2,1)\nMARKER_C;\n#endif\n"
        file_result, context = _analyze(source, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
        assert capsys.readouterr().err == ""


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


class TestAWarmCacheHitStillRetractsAPendingRecord:
    """Every class above drives ``SimplePreprocessor`` directly, bypassing the
    preprocessing cache entirely -- each pass gets its own fresh instance, so
    ``_note_condition_resolved`` always runs. Production never does that: two
    translation units sharing a header go through
    ``preprocessing_cache.get_or_compute_preprocessing``, and a second
    encounter at a macro state some earlier call already computed is served
    from the cache WITHOUT running ``SimplePreprocessor`` again -- so the
    retraction those classes rely on cannot fire on a cache hit unless the
    cache layer replays it itself.

    The repro needs two *separate* ``converging`` regions sharing one
    ``BuildContext`` (mirroring two independent magicflags/headerdeps
    passes) plus a genuinely, permanently unevaluable condition in an
    unrelated file: ``flush_pending_warnings`` short-circuits without
    clearing the resolved store when its pending dict happens to be empty
    (see its ``if not pending: return 0``), so a first convergence whose
    only occurrence resolves cleanly would never exercise the clear. The
    unrelated garbage condition forces a real flush, which is what "an
    intervening flush of a real warning clears the resolved store" refers
    to.
    """

    _GARBAGE_SOURCE = "#if 1 @ 2\nMARKER;\n#endif\n"

    @staticmethod
    def _analyze_sharing(context, source: str, tmpdir: str, name: str):
        """Like the module-level ``_analyze``, but against a caller-supplied
        context so multiple files' results share one cache and one set of
        deferred-warning stores -- the module helper mints a fresh
        ``BuildContext`` per call, which would defeat this test entirely."""
        path = os.path.join(tmpdir, name)
        with open(path, "w") as fh:
            fh.write(source)
        return analyze_file(get_file_hash(path, context), context)

    def test_a_settled_variant_hit_retracts_an_earlier_pending_record(self, tmp_path, capsys):
        gate_source = f"#if {_GATE}\nMARKER;\n#endif\n"
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
        gate_result = self._analyze_sharing(context, gate_source, str(tmp_path), "gate.cpp")
        garbage_result = self._analyze_sharing(context, self._GARBAGE_SOURCE, str(tmp_path), "garbage.cpp")
        macros_result = self._analyze_sharing(context, _MACROS_HEADER, str(tmp_path), "macros.h")

        early_macros = MacroState({}, {}, anchor_root="")
        settled_macros = get_or_compute_preprocessing(
            macros_result, early_macros, verbose=0, context=context
        ).updated_macros

        # First convergence: the gate resolves cleanly at the settled key
        # (populating the variant cache and the resolved store), and an
        # unrelated, permanently-unevaluable garbage condition in a
        # different file gives the exit flush something real to report --
        # which is what makes it actually clear the resolved store instead
        # of short-circuiting.
        with converging(context):
            get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)
            get_or_compute_preprocessing(garbage_result, early_macros, verbose=1, context=context)
        first_flush_stderr = capsys.readouterr().err
        assert "1 @ 2" in first_flush_stderr, first_flush_stderr
        assert "EXTLIB_AT_LEAST" not in first_flush_stderr, first_flush_stderr

        # Second, independent convergence: an early pass (macro state not
        # yet settled) cannot evaluate the gate and records it pending...
        with converging(context):
            get_or_compute_preprocessing(gate_result, early_macros, verbose=1, context=context)
            assert context.pending_preprocessor_warnings, "early pass should record pending"

            # ...then the settled pass hits the SAME variant cache entry the
            # first convergence populated. The condition IS resolved at this
            # macro state -- the cached entry is proof, since it was only
            # ever stored by a SimplePreprocessor run that got past the
            # evaluation without raising -- so this must retract the pending
            # record from the early pass.
            get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)

        second_flush_stderr = capsys.readouterr().err
        assert "cannot evaluate" not in second_flush_stderr, (
            "stale pending record from the early pass survived a warm cache hit that resolved it: "
            f"{second_flush_stderr!r}"
        )

    def test_an_entry_seeded_outside_any_convergence_still_retracts_on_a_warm_hit(self, tmp_path, capsys):
        """The production seeding order: DirectHeaderDeps calls
        get_or_compute_preprocessing OUTSIDE any ``converging`` region, and
        the headerdeps walk runs BEFORE the magicflags convergence over the
        same context caches -- so the settled-state entry is typically
        seeded at convergence depth 0. Occurrence recording must not be
        gated on ``_defer_warnings``: a depth-0-seeded entry that recorded
        nothing replays nothing, and the stale pending record from the
        convergence's own early pass survives exactly as in the original
        finding. (No unrelated garbage warning is needed here: the resolved
        store is empty from the start because the depth-0 seeding pass,
        with deferral off, never wrote to it.)"""
        gate_source = f"#if {_GATE}\nMARKER;\n#endif\n"
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
        gate_result = self._analyze_sharing(context, gate_source, str(tmp_path), "gate.cpp")
        macros_result = self._analyze_sharing(context, _MACROS_HEADER, str(tmp_path), "macros.h")

        early_macros = MacroState({}, {}, anchor_root="")
        settled_macros = get_or_compute_preprocessing(
            macros_result, early_macros, verbose=0, context=context
        ).updated_macros

        # Depth-0 seeding, as the headerdeps walk does it.
        get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)
        assert capsys.readouterr().err == ""

        with converging(context):
            get_or_compute_preprocessing(gate_result, early_macros, verbose=1, context=context)
            assert context.pending_preprocessor_warnings, "early pass should record pending"
            get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)

        stderr = capsys.readouterr().err
        assert "cannot evaluate" not in stderr, (
            f"a depth-0-seeded cache entry replayed no occurrences, stranding the pending record: {stderr!r}"
        )
