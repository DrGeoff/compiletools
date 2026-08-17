"""A macro cycle only an unsettled pass can see must not be reported.

``_recursive_expand_macros_sz`` and ``_expand_object_macros_recursive_sz`` stop
at an iteration cap and warn that the macro definitions are cyclic.  Inside a
magicflags convergence that verdict belongs to one pass, not to the build: an
early pass expands under a macro state where ``A`` and ``B`` are both defined
and refer to each other, while the settled state expands the same text to a
fixed point in two rounds.  Printing from the early pass tells the user their
definitions are cyclic on a build whose own final answer says otherwise -- the
contradiction the condition-report deferral already covers.

The truncate-and-continue behaviour is unchanged; only the reporting moves.
"""

import os
from types import SimpleNamespace

import stringzilla as sz

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import analyze_file, set_analyzer_args
from compiletools.global_hash_registry import get_file_hash
from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing
from compiletools.simple_preprocessor import (
    EXPANSION_KIND,
    HAS_OPERAND_EXPANSION_KIND,
    SimplePreprocessor,
    converging,
    verdict_root,
)

# The cycle every arm shares. Under _CYCLIC the pair never settles; under
# _SETTLED the same text reaches "1" in two rounds.
_CYCLIC = {sz.Str("A"): sz.Str("B"), sz.Str("B"): sz.Str("A")}
_SETTLED = {sz.Str("A"): sz.Str("1")}

_GATE_SOURCE = "#if A\nMARKER;\n#endif\n"

_CYCLE_TEXT = "recursive macro definition cycle"


def _analyzer_args():
    return SimpleNamespace(
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


def _analyze(source: str, tmpdir: str, name: str = "gate.cpp"):
    """Run the production analyzer over ``source`` and return (result, context)."""
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(source)
    context = BuildContext()
    set_analyzer_args(_analyzer_args(), context)
    return analyze_file(get_file_hash(path, context), context), context


def _analyze_sharing(context, source: str, tmpdir: str, name: str):
    """Analyze against a caller-supplied context so several files share caches."""
    path = os.path.join(tmpdir, name)
    with open(path, "w") as fh:
        fh.write(source)
    return analyze_file(get_file_hash(path, context), context)


class TestACappedExpansionIsHeldUntilTheStateSettles:
    """Two passes over one file inside one ``converging`` region -- the shape
    magicflags produces, with the flush at region exit as the moment of truth.
    """

    def test_the_early_pass_reports_nothing_and_the_flush_reports_once(self, tmp_path, capsys):
        """Deferral proper: the cap is hit inside the region, and the line the
        user sees arrives only when the region exits."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            mid_region = capsys.readouterr().out
            assert _CYCLE_TEXT not in mid_region, f"reported from inside the convergence: {mid_region!r}"
            assert context.pending_preprocessor_expansion_warnings, "the cap hit recorded nothing to flush"

        flushed = capsys.readouterr().out
        assert flushed.count(_CYCLE_TEXT) == 1, flushed
        assert "_recursive_expand_macros_sz" in flushed, flushed

    def test_a_later_pass_that_expands_cleanly_leaves_nothing_to_report(self, tmp_path, capsys):
        """The silent control. The settled state expands ``A`` to ``1`` in two
        rounds, so the early pass's record is retracted and the convergence
        exits without contradicting the flags it goes on to emit."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            assert context.pending_preprocessor_expansion_warnings, "pass 1 recorded nothing to retract"
            SimplePreprocessor(_SETTLED, verbose=1).process_structured(file_result, context)

        flushed = capsys.readouterr().out
        assert _CYCLE_TEXT not in flushed, f"a superseded pass's cycle report survived the flush: {flushed!r}"

    def test_a_named_partitions_clean_expansion_retracts_the_unattributed_record(self, tmp_path, capsys):
        """The cross-partition half, matching what the condition reports get.

        An unattributed walk (cake's pre-fetch scan runs outside any
        verdict_root span) hits the cap under a partial macro state; the
        build's own settled pass expands the same site cleanly but records
        into the target's NAMED partition. The unattributed record is a
        provisional approximation by construction, so the settled resolution
        must retract it -- otherwise the flush prints a cycle warning the
        build's own flags contradict."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            assert context.pending_preprocessor_expansion_warnings, "the unattributed cap hit recorded nothing"
            with verdict_root(context, "target.cpp"):
                SimplePreprocessor(_SETTLED, verbose=1).process_structured(file_result, context)

        flushed = capsys.readouterr().out
        assert _CYCLE_TEXT not in flushed, (
            f"an unattributed cap record survived a named partition's clean expansion: {flushed!r}"
        )

    def test_a_pass_that_hits_the_cap_after_a_clean_one_does_not_re_record(self, tmp_path, capsys):
        """Passes are not ordered best-last. Once an occurrence has expanded to
        a fixed point, a later pass hitting the cap on it must not re-record --
        the same interleaving protection ``resolved_preprocessor_conditions``
        provides for the condition reports."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_SETTLED, verbose=1).process_structured(file_result, context)
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            assert not context.pending_preprocessor_expansion_warnings, (
                "a cap hit after a clean expansion of the same occurrence re-recorded it"
            )

        flushed = capsys.readouterr().out
        assert _CYCLE_TEXT not in flushed, flushed

    def test_a_cycle_the_settled_state_still_has_is_reported_once(self, tmp_path, capsys):
        """The must-not-move half: both passes hit the cap, so the report is
        the build's own answer and has to survive. One line, not two."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)

        flushed = capsys.readouterr().out
        assert flushed.count(_CYCLE_TEXT) == 1, flushed

    def test_the_stores_are_empty_once_the_convergence_has_flushed(self, tmp_path, capsys):
        """Both stores describe one convergence; a key surviving into the next
        would silence a report that convergence goes on to owe."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)
            SimplePreprocessor(_SETTLED, verbose=1).process_structured(file_result, context)

        assert capsys.readouterr().out.count(_CYCLE_TEXT) == 0
        assert not context.pending_preprocessor_expansion_warnings
        assert not context.resolved_preprocessor_expansions


class TestOutsideAConvergenceTheReportIsImmediate:
    """Depth 0 means no pass will follow, so there is nothing to supersede the
    verdict and nothing to defer. ct-headertree and ct-filelist drive the
    preprocessor this way, as does magicflags' own settled-state expander.
    """

    def test_a_capped_expansion_at_depth_zero_prints_where_it_happens(self, tmp_path, capsys):
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        SimplePreprocessor(_CYCLIC, verbose=1).process_structured(file_result, context)

        printed = capsys.readouterr().out
        assert _CYCLE_TEXT in printed, printed
        assert not context.pending_preprocessor_expansion_warnings, "a depth-0 run deferred instead of printing"

    def test_a_direct_expander_call_prints_without_any_context(self, capsys):
        """magicflags expands its settled-state magic-flag values on an
        instance that never sees a context, outside its ``converging`` region."""
        SimplePreprocessor(_CYCLIC, verbose=1)._recursive_expand_macros_sz(sz.Str("A"))
        assert _CYCLE_TEXT in capsys.readouterr().out

    def test_the_report_stays_gated_on_verbose(self, tmp_path, capsys):
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            SimplePreprocessor(_CYCLIC, verbose=0).process_structured(file_result, context)
            assert not context.pending_preprocessor_expansion_warnings, "a quiet run recorded a report to flush"

        assert _CYCLE_TEXT not in capsys.readouterr().out


class TestTheKeyDiscriminatesWhatItHasTo:
    """An occurrence key that is too coarse retracts a report it does not own.
    The two failure modes with a shared spelling are covered here.
    """

    @staticmethod
    def _bound(macros, file_result, context) -> SimplePreprocessor:
        """A preprocessor with its stores bound to ``context``, as a live pass
        leaves it -- so a helper can then be driven directly."""
        preprocessor = SimplePreprocessor(macros, verbose=1)
        preprocessor.process_structured(file_result, context)
        return preprocessor

    def test_the_two_helpers_do_not_retract_each_other(self, tmp_path, capsys):
        """Both helpers expand ``A`` at the same site. A clean run of one is no
        evidence about the other: they expand by different rules (the __has_*
        one deliberately skips defined()/__has_* handling)."""
        file_result, context = _analyze(_GATE_SOURCE, str(tmp_path))
        with converging(context):
            capped = self._bound(_CYCLIC, file_result, context)
            capped._expand_object_macros_recursive_sz(sz.Str("A"))
            assert context.pending_preprocessor_expansion_warnings, "the operand cap hit recorded nothing"

            clean = self._bound(_SETTLED, file_result, context)
            clean._recursive_expand_macros_sz(sz.Str("A"))

        flushed = capsys.readouterr().out
        assert "_expand_object_macros_recursive_sz" in flushed, (
            f"the expression helper's clean run retracted the operand helper's report: {flushed!r}"
        )

    def test_a_clean_expansion_does_not_retract_the_conditions_own_report(self, tmp_path, capsys):
        """A ``#if``'s controlling expression is expanded before it is
        evaluated, so an expansion event and a condition event can name the
        same file, directive text and line. They are different claims -- "this
        text reaches a fixed point" says nothing about "this condition is
        evaluable" -- and the separate stores are what keeps them apart. Route
        the expansion events through the condition stores instead and the
        clean expansion here marks the condition resolved, so the report the
        pass genuinely owes is never recorded at all."""
        # A function-like gate whose definition this pass has not seen: the
        # text expands cleanly (there is nothing to substitute) and the
        # condition is unevaluable.
        source = "#if EXTLIB_AT_LEAST(2, 0)\nMARKER;\n#endif\n"
        file_result, context = _analyze(source, str(tmp_path))
        with converging(context):
            SimplePreprocessor({}, verbose=1).process_structured(file_result, context)
            assert context.pending_preprocessor_warnings, "the unevaluable condition recorded nothing"
            assert context.resolved_preprocessor_expansions, "the clean expansion of the same text recorded nothing"

        captured = capsys.readouterr()
        assert "cannot evaluate" in captured.err, captured.err


class TestAWarmCacheHitAlsoRetractsACappedExpansion:
    """Production reaches a settled macro state through the preprocessing
    cache as often as through a live rerun: two translation units sharing a
    header, or DirectHeaderDeps' include-list tier in front of it, mean the
    only settled-state evaluation a convergence performs can be a warm hit
    that never runs SimplePreprocessor at all. The occurrence replay is what
    lets such a hit retract the convergence's own early record.
    """

    def test_a_settled_variant_hit_retracts_an_earlier_capped_record(self, tmp_path, capsys):
        context = BuildContext()
        set_analyzer_args(_analyzer_args(), context)
        gate_result = _analyze_sharing(context, _GATE_SOURCE, str(tmp_path), "gate.cpp")

        cyclic_macros = MacroState({}, dict(_CYCLIC), anchor_root="")
        settled_macros = MacroState({}, dict(_SETTLED), anchor_root="")

        # Depth-0 seeding, as Hunter's headerdeps walk does it before the
        # magicflags convergence over the same context caches.
        get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)
        assert _CYCLE_TEXT not in capsys.readouterr().out

        with converging(context):
            get_or_compute_preprocessing(gate_result, cyclic_macros, verbose=1, context=context)
            assert context.pending_preprocessor_expansion_warnings, "the early pass recorded nothing to retract"
            get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)

        flushed = capsys.readouterr().out
        assert _CYCLE_TEXT not in flushed, (
            f"a warm hit of the settled state replayed nothing, stranding the early record: {flushed!r}"
        )

    def test_a_depth_zero_warm_hit_does_not_pre_resolve_the_next_convergence(self, tmp_path, capsys):
        """The mirror: a warm hit outside any convergence must leave both
        stores alone, exactly as a live depth-0 run does. A replay that writes
        the resolved store there hands the NEXT convergence a key that silences
        a cycle it genuinely still has."""
        context = BuildContext()
        set_analyzer_args(_analyzer_args(), context)
        gate_result = _analyze_sharing(context, _GATE_SOURCE, str(tmp_path), "gate.cpp")

        cyclic_macros = MacroState({}, dict(_CYCLIC), anchor_root="")
        settled_macros = MacroState({}, dict(_SETTLED), anchor_root="")

        get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)
        get_or_compute_preprocessing(gate_result, settled_macros, verbose=1, context=context)
        assert not context.resolved_preprocessor_expansions, (
            "a depth-0 warm hit wrote the resolved store; the next convergence inherits it"
        )
        capsys.readouterr()

        with converging(context):
            get_or_compute_preprocessing(gate_result, cyclic_macros, verbose=1, context=context)

        flushed = capsys.readouterr().out
        assert _CYCLE_TEXT in flushed, (
            f"the owed cycle report was suppressed by resolved-store pollution from a depth-0 hit: {flushed!r}"
        )

    def test_the_recorded_occurrence_names_the_helper_that_produced_it(self, tmp_path):
        """The occurrence tuple is the replay's whole vocabulary, so its shape
        is a contract: kind, directive type, expansion input, line."""
        context = BuildContext()
        set_analyzer_args(_analyzer_args(), context)
        gate_result = _analyze_sharing(context, _GATE_SOURCE, str(tmp_path), "gate.cpp")

        result = get_or_compute_preprocessing(
            gate_result, MacroState({}, dict(_SETTLED), anchor_root=""), verbose=1, context=context
        )
        assert ("expansion", "if", "A", 0) in result.effects.condition_occurrences, result.effects.condition_occurrences
        assert EXPANSION_KIND == "expansion" and HAS_OPERAND_EXPANSION_KIND == "has_operand_expansion"
