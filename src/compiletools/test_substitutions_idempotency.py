"""substitutions() must be a fixed point after parseargs.

cake.py legitimately re-runs substitutions() on the same namespace
(second-stage target discovery, //#GIT= external fetch re-run,
compilation_database refresh). Any flag drift between pass 1 and pass 2
forks the object CAS key space: --auto runs (two passes) and
--no-auto runs (one pass) of the same source land on different
macro_state_hash values even though the emitted compile command lines
are identical.

The historical instance: pass 1 ran _unify_cpp_cxx_flags BEFORE
_inject_ffile_prefix_map, so CPPFLAGS never saw the injected
-ffile-prefix-map token; pass 2's unify then promoted it from CXXFLAGS
into CPPFLAGS, and the build-context hash (which hashes CPPFLAGS
tokens) diverged.
"""

import argparse
import dataclasses
import itertools
import os
import pathlib

import pytest

import compiletools.apptools as apptools
import compiletools.compilation_database as cdb
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.flags import Flags
from compiletools.test_build_state_differential import _run_old_pipeline

FLAG_SLOTS = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")


@pytest.fixture
def parsers_reset():
    """Wipe the configargparse parser cache around tests that go through
    ``parseargs`` end-to-end."""
    uth.reset()
    yield
    uth.reset()


def _parseargs_in_temp_repo(extra_argv=(), register_link_args=True):
    """Full parseargs pipeline in a temp dir, mirroring TestQuietAppliedOnce.

    Registers add_link_arguments so args.LDFLAGS exists and
    _add_flags_from_pkg_config takes its want_libs branch (same reason
    test_apptools.py's _parseargs_with_pkg_config_conf does it) -- without
    it args.LDFLAGS is never a CAP-registered slot and pkg-config --libs
    output has nowhere to land. Pass register_link_args=False to build the
    three-slot CAP shape (ct-compilation-database registers no LDFLAGS).
    """
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("idempotency test", argv=argv)
        cdb.CompilationDatabaseCreator.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        if register_link_args:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            return apptools.parseargs(cap, argv, context=BuildContext())


def _old_pipeline_args_in_temp_repo(extra_argv=(), register_link_args=True):
    """Same CAP/config setup as ``_parseargs_in_temp_repo``, but drives the
    resulting namespace through the LEGACY ``substitutions()`` pipeline
    (``test_build_state_differential._run_old_pipeline``) instead of the
    post-swap ``apptools.parseargs`` core.

    ``substitutions()``'s own seed-restore / fixed-point contract
    (``args._substitution_seed``) is only exercised faithfully starting from
    a namespace ``substitutions()`` itself produced on pass 1 -- calling
    ``substitutions()`` directly on a new-core namespace has no seed to
    restore from, so unconditional-append stages (pkg-config) and
    namespace-shape materialization (``_finalize_flag_state``) double up.
    Tests pinning ``substitutions()``'s own re-run contract in isolation
    (as opposed to ``apptools.resubstitute``, which no longer calls
    ``substitutions()`` at all post-Task-9) need this fixture rather than
    ``_parseargs_in_temp_repo``.
    """
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("idempotency test", argv=argv)
        cdb.CompilationDatabaseCreator.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        if register_link_args:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            return _run_old_pipeline(cap, argv, BuildContext())


@pytest.mark.usefixtures("parsers_reset")
class TestSubstitutionsIdempotent:
    def test_rerun_is_fixed_point_for_all_flag_slots(self):
        """Applying substitutions() a second time must not change any raw
        flag string, any *_tokens list, or args.flags. TempDirContext's cwd
        is its own gitroot fallback, so _inject_ffile_prefix_map fires."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            gitroot = os.getcwd()
            assert f"-ffile-prefix-map={gitroot}=." in args.CXXFLAGS, (
                "Precondition failed: prefix-map injection did not fire; this test would pass vacuously."
            )
            first_raw = {slot: getattr(args, slot) for slot in FLAG_SLOTS}
            first_tokens = {slot: list(getattr(args, f"{slot}_tokens")) for slot in FLAG_SLOTS}
            first_flags = args.flags

            apptools.substitutions(args, verbose=0)

            for slot in FLAG_SLOTS:
                assert getattr(args, slot) == first_raw[slot], (
                    f"substitutions() re-run changed args.{slot}:\n"
                    f"  pass 1: {first_raw[slot]!r}\n"
                    f"  pass 2: {getattr(args, slot)!r}"
                )
                assert list(getattr(args, f"{slot}_tokens")) == first_tokens[slot], (
                    f"substitutions() re-run changed args.{slot}_tokens"
                )
            assert args.flags == first_flags, (
                f"substitutions() re-run changed args.flags:\n  pass 1: {first_flags}\n  pass 2: {args.flags}"
            )

    def test_pass1_cppflags_carries_injected_prefix_map(self):
        """The fix must converge on the --auto (two-pass) key space: the
        injected -ffile-prefix-map token belongs in CPPFLAGS after pass 1
        (unified with CXXFLAGS), not only after a second pass."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            gitroot = os.getcwd()
            token = f"-ffile-prefix-map={gitroot}=."
            assert token in args.CPPFLAGS_tokens, (
                f"Pass 1 CPPFLAGS lacks the injected prefix-map token.\n"
                f"  CPPFLAGS_tokens: {args.CPPFLAGS_tokens}\n"
                f"  CXXFLAGS_tokens: {args.CXXFLAGS_tokens}"
            )
            assert args.CPPFLAGS == args.CXXFLAGS, (
                "CPPFLAGS and CXXFLAGS must stay unified after injection (--separate-flags-CPP-CXX not set)."
            )

    def test_fixed_point_guard_raises_on_non_idempotent_flag_state(self):
        """The guard at the end of substitutions() must reject a flag state
        the normalization tail would still change — i.e. the exact pre-fix
        shape: prefix-map token in CXXFLAGS but not in the unified CPPFLAGS,
        so a re-run's unify would promote it."""
        from types import SimpleNamespace

        args = SimpleNamespace(
            CPPFLAGS="-O2",
            CFLAGS="-O2 -ffile-prefix-map=/repo=.",
            CXXFLAGS="-O2 -ffile-prefix-map=/repo=.",
            LDFLAGS="",
            ffile_prefix_map_target=".",
        )
        with pytest.raises(RuntimeError, match="CPPFLAGS"):
            apptools.assert_flag_normalization_fixed_point(args)

    def test_fixed_point_guard_passes_on_converged_flag_state(self):
        """The converged shape (token unified into CPPFLAGS) must pass, and
        the guard must not mutate the namespace it checks."""
        from types import SimpleNamespace

        unified = "-O2 -ffile-prefix-map=/repo=."
        args = SimpleNamespace(
            CPPFLAGS=unified,
            CFLAGS=unified,
            CXXFLAGS=unified,
            LDFLAGS="",
            ffile_prefix_map_target=".",
        )
        apptools.assert_flag_normalization_fixed_point(args)
        assert unified == args.CPPFLAGS
        assert unified == args.CXXFLAGS

    def test_separate_flags_mode_keeps_cppflags_clean(self):
        """Under --separate-flags-CPP-CXX the unify step is skipped, so the
        prefix-map token must stay out of CPPFLAGS on every pass."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--separate-flags-CPP-CXX"])
            gitroot = os.getcwd()
            token = f"-ffile-prefix-map={gitroot}=."
            assert token in args.CXXFLAGS_tokens
            assert token not in args.CPPFLAGS_tokens
            apptools.substitutions(args, verbose=0)
            assert token not in args.CPPFLAGS_tokens, (
                "substitutions() re-run leaked the prefix-map token into CPPFLAGS under --separate-flags-CPP-CXX."
            )


@pytest.mark.usefixtures("parsers_reset")
class TestPkgConfigSlotsAreFixedPoints:
    """A substitutions() re-run must not re-append pkg-config --cflags/--libs.

    Pre-fix behavior: _add_flags_from_pkg_config appends unconditionally and
    the only dedup covering CFLAGS/LDFLAGS runs before it, so pass 2 doubles
    both slots (all four under --separate-flags-CPP-CXX). The doubled CFLAGS
    reaches _get_build_context_hash and forks the object CAS key space
    between --auto and --no-auto builds; the doubled LDFLAGS forks the
    cas-exedir link key via args.flags.ld.

    This is a property of substitutions()'s OWN seed-restore contract
    (args._substitution_seed), exercised here in isolation via
    _old_pipeline_args_in_temp_repo -- post-Task-9 apptools.parseargs no
    longer installs that seed (only apptools.resubstitute's re-gather path
    runs on production namespaces), so these tests drive the namespace
    through the legacy reference pipeline directly.
    """

    def _assert_rerun_is_noop(self, extra_argv):
        with uth.TempDirContext():
            args = _old_pipeline_args_in_temp_repo(extra_argv=extra_argv)
            assert "-ltestpkg1" in args.LDFLAGS, (
                "Precondition failed: pkg-config --libs did not land; the fixed-point assertions would be vacuous."
            )
            assert "testpkg1" in args.CFLAGS, "Precondition failed: pkg-config --cflags did not land in CFLAGS."
            first_raw = {slot: getattr(args, slot) for slot in FLAG_SLOTS}
            first_tokens = {slot: list(getattr(args, f"{slot}_tokens")) for slot in FLAG_SLOTS}
            first_flags = args.flags

            apptools.substitutions(args, verbose=0)

            for slot in FLAG_SLOTS:
                assert getattr(args, slot) == first_raw[slot], (
                    f"substitutions() re-run changed args.{slot}:\n"
                    f"  pass 1: {first_raw[slot]!r}\n"
                    f"  pass 2: {getattr(args, slot)!r}"
                )
                assert list(getattr(args, f"{slot}_tokens")) == first_tokens[slot], (
                    f"substitutions() re-run changed args.{slot}_tokens"
                )
            assert args.flags == first_flags, (
                f"substitutions() re-run changed args.flags:\n  pass 1: {first_flags}\n  pass 2: {args.flags}"
            )

    def test_unified_mode(self, pkgconfig_env):
        self._assert_rerun_is_noop(["--pkg-config=nested"])

    def test_separate_flags_mode(self, pkgconfig_env):
        self._assert_rerun_is_noop(["--pkg-config=nested", "--separate-flags-CPP-CXX"])


def _drift_base_flags():
    """Synthetic prior-snapshot Flags for the drift-comparison unit tests."""
    return Flags(
        cpp=("-O2", "-Wall"),
        c=("-O2", "-Wall"),
        cxx=("-O2", "-Wall"),
        ld=("-lm",),
        compiler_identity="test-identity",
    )


class TestWarnUnexplainedFlagDrift:
    """apptools.warn_unexplained_flag_drift complements the in-substitutions
    fixed-point guard (which replays only the normalization tail): cake calls
    it after a legitimate substitutions() re-run to catch any OTHER step that
    drifted args.flags, beyond the sanctioned INCLUDE-widening -I additions."""

    def test_equal_snapshots_report_no_drift(self):
        flags = _drift_base_flags()
        assert apptools.warn_unexplained_flag_drift(flags, flags, []) == []

    def test_non_include_addition_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cxx=prior.cxx + ("-O3",))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 1, f"expected exactly one drifted slot, got: {msgs}"
        assert "cxx" in msgs[0]
        assert "-O3" in msgs[0]

    def test_removed_token_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cpp=prior.cpp[:-1])
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 1
        assert "cpp" in msgs[0]

    def test_i_pair_is_explained_only_when_path_is_in_include(self):
        prior = _drift_base_flags()
        tail = ("-I", "/ext/root")
        new = dataclasses.replace(prior, cpp=prior.cpp + tail, c=prior.c + tail, cxx=prior.cxx + tail)
        assert apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"]) == []
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 3, (
            f"-I additions for a path NOT in INCLUDE must be reported per slot (cpp, c, cxx), got: {msgs}"
        )

    def test_i_pair_inserted_mid_sequence_is_explained(self):
        prior = _drift_base_flags()
        cpp = prior.cpp[:1] + ("-I", "/ext/root") + prior.cpp[1:]
        new = dataclasses.replace(prior, cpp=cpp)
        assert apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"]) == []

    def test_duplicate_i_pair_readd_is_reported(self):
        """Legitimate INCLUDE widening goes through
        dedup_include_paths_to_append, which never re-adds a path already
        present -- so a second copy of an existing -I pair is drift, not
        explained widening, even when the path IS in INCLUDE."""
        prior = _drift_base_flags()
        with_pair = dataclasses.replace(prior, cpp=prior.cpp + ("-I", "/ext/root"))
        doubled = dataclasses.replace(prior, cpp=prior.cpp + ("-I", "/ext/root", "-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(with_pair, doubled, ["/ext/root"])
        assert len(msgs) == 1 and "cpp" in msgs[0], (
            f"A re-added duplicate -I pair must be reported as unexplained drift, got: {msgs}"
        )

    def test_double_insertion_of_new_path_is_reported(self):
        """dedup_include_paths_to_append also never emits the same NEW path
        twice in one widening, so two insertions of a path absent from prior
        are drift (e.g. two non-idempotent steps each emitting the pair),
        not explained widening."""
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cpp=prior.cpp + ("-I", "/ext/root", "-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"])
        assert len(msgs) == 1 and "cpp" in msgs[0], (
            f"A doubly-inserted -I pair must be reported as unexplained drift, got: {msgs}"
        )

    def test_i_pair_removal_is_reported(self):
        prior = _drift_base_flags()
        with_pair = dataclasses.replace(prior, cpp=prior.cpp + ("-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(with_pair, prior, ["/ext/root"])
        assert len(msgs) == 1 and "cpp" in msgs[0]

    def test_i_pair_inserted_before_existing_i_pair_is_explained(self):
        prior = _drift_base_flags()
        cpp_prior = ("-I", "/existing/inc") + prior.cpp
        cpp_new = ("-I", "/ext/root", "-I", "/existing/inc") + prior.cpp
        with_pair = dataclasses.replace(prior, cpp=cpp_prior)
        new = dataclasses.replace(prior, cpp=cpp_new)
        assert apptools.warn_unexplained_flag_drift(with_pair, new, ["/ext/root"]) == []

    def test_ld_addition_is_reported_even_in_dash_i_form(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, ld=prior.ld + ("-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"])
        assert len(msgs) == 1
        assert "ld" in msgs[0]

    def test_compiler_identity_change_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, compiler_identity="swapped-identity")
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 1
        assert "compiler_identity" in msgs[0]

    def test_comparison_never_prints(self, capsys):
        """The comparison is pure: surfacing the messages would be a
        caller's job, but no production caller remains post-Task-9
        (apptools.resubstitute no longer invokes it). No printing at any
        drift level."""
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cxx=prior.cxx + ("-O3",))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 1, "Precondition failed: drift must be reported for this check to be meaningful."
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


@pytest.mark.usefixtures("parsers_reset")
class TestResubstitute:
    """apptools.resubstitute is the only sanctioned re-run path. Swap Task
    9 rewrote it as re-gather + recompute (no more legacy substitutions()
    replay), which is a fixed point BY CONSTRUCTION -- the RuntimeError
    drift guard the pre-Task-9 version raised is retired."""

    def test_include_widening_rerun_does_not_raise(self):
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)
            args.INCLUDE = (args.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(args)
            assert newdir in args.flags.cpp, (
                f"Precondition failed: the re-run did not land the -I pair, so the "
                f"no-raise outcome would be vacuous. cpp slot: {args.flags.cpp}"
            )

    def test_include_widening_converges_with_fresh_single_pass(self):
        """The --auto (two-pass: parse, then widen INCLUDE and resubstitute)
        vs --no-auto (one pass, INCLUDE known up front) convergence, now
        pinned as a first-class contract: re-gather + recompute must land
        on exactly the same args.flags a fresh single-pass parseargs would
        compute over equivalent inputs."""
        with uth.TempDirContext():
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)

            widened = _parseargs_in_temp_repo()
            widened.INCLUDE = (widened.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(widened)

            fresh = _parseargs_in_temp_repo(extra_argv=[f"--append-INCLUDE={newdir}"])

            assert widened.flags == fresh.flags, (
                f"--auto (widen + resubstitute) diverged from a fresh single-pass parse:\n"
                f"  widened: {widened.flags}\n  fresh:   {fresh.flags}"
            )

    def test_include_widening_converges_with_fresh_single_pass_under_separate_flags(self):
        """Same convergence oracle as
        test_include_widening_converges_with_fresh_single_pass, under
        --separate-flags-CPP-CXX. This mode is the one where the
        _resubstitute_seed's content (not just its restore-before-re-gather
        timing) is load-bearing: with stage_unify skipped, CPPFLAGS can
        still carry the raw _UNSUPPLIED_USE_CXXFLAGS sentinel at seed time,
        and the seed must hand that sentinel back on every re-run rather
        than a since-materialized concrete string, or the widened run's
        cppflags would permanently diverge from a fresh single pass'."""
        with uth.TempDirContext():
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)

            widened = _parseargs_in_temp_repo(extra_argv=["--separate-flags-CPP-CXX"])
            widened.INCLUDE = (widened.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(widened)

            fresh = _parseargs_in_temp_repo(extra_argv=["--separate-flags-CPP-CXX", f"--append-INCLUDE={newdir}"])

            assert widened.flags == fresh.flags, (
                f"--auto (widen + resubstitute) diverged from a fresh single-pass parse "
                f"under --separate-flags-CPP-CXX:\n  widened: {widened.flags}\n  fresh:   {fresh.flags}"
            )

    def test_auto_rerun_with_overlapping_pkg_config_converges(self, tmp_path, monkeypatch):
        """The blocker Task 8 flagged (task-8-report.md Concern #1a): two
        pkg-config packages sharing a Cflags/Libs token. Pass 1 (the new
        core) dedups the shared tokens at the END of the pipeline
        (stage_dedup, D5); the pre-Task-9 resubstitute replayed the LEGACY
        substitutions() pipeline, whose dedup-then-append ordering
        re-derives the OLD duplicated form and raised RuntimeError on the
        (spurious) drift. resubstitute no longer replays that pipeline, so
        the re-run reproduces pass 1's already-deduped result exactly."""
        for name, lib in (("ctresub9alpha", "-lctresub9alpha"), ("ctresub9beta", "-lctresub9beta")):
            (tmp_path / f"{name}.pc").write_text(
                f"Name: {name}\nDescription: resubstitute overlap pin\nVersion: 1.0.0\n"
                "Cflags: -DCTRESUB9_COMMON\n"
                f"Libs: -L/usr/local/lib -lctresub9common {lib}\n"
            )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--pkg-config=ctresub9alpha ctresub9beta"])
            assert args.CFLAGS_tokens.count("-DCTRESUB9_COMMON") == 1, (
                "Precondition failed: pass 1's core did not dedup the shared cflags token."
            )
            assert args.LDFLAGS_tokens.count("-lctresub9common") == 1, (
                "Precondition failed: pass 1's core did not dedup the shared libs token."
            )

            apptools.resubstitute(args)

            assert args.CFLAGS_tokens.count("-DCTRESUB9_COMMON") == 1
            assert args.LDFLAGS_tokens.count("-lctresub9common") == 1
            assert args.LDFLAGS_tokens.count("-L/usr/local/lib") == 1

    def test_legacy_callback_registry_no_longer_affects_resubstitute(self):
        """RETIRED CONTRACT (was test_non_idempotent_callback_raises):
        pre-Task-9, appending a non-idempotent hook to the legacy
        _substitutioncallbacks registry made resubstitute raise, because it
        replayed substitutions() and that registry drives substitutions().
        Task 9's resubstitute is re-gather + recompute and never calls
        substitutions(), so the registry cannot inject drift into it any
        more -- proven here by registering the exact hook that used to
        force a RuntimeError and observing it has no effect."""
        counter = itertools.count(1)

        def nonidempotent_callback(args):
            token = f"-DCT_TEST_RERUN{next(counter)}"
            for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
                current = getattr(args, slot, "") or ""
                setattr(args, slot, f"{current} {token}".strip())

        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            first_flags = args.flags
            # Direct registry append: apptools.registercallback is a
            # deprecation error now, and this hook exists purely to prove
            # it is inert against resubstitute.
            apptools._substitutioncallbacks.append(nonidempotent_callback)
            try:
                apptools.resubstitute(args)
            finally:
                apptools.resetcallbacks()

            assert "CT_TEST_RERUN" not in args.CXXFLAGS, (
                "resubstitute must not drive the legacy _substitutioncallbacks registry."
            )
            assert args.flags == first_flags, (
                "resubstitute must converge to the same flags when nothing input-affecting changed."
            )


@pytest.mark.usefixtures("parsers_reset")
class TestThreeSlotCapRerun:
    """A CAP that registers no LDFLAGS (ct-compilation-database) must stay
    a three-slot tool across a re-run.

    _finalize_flag_state materializes args.LDFLAGS = "" after pass 1, so a
    hasattr-based want_libs in _add_flags_from_pkg_config flips False->True
    on the re-run and lands pkg-config --libs in a slot the first pass never
    touched -- drift resubstitute rejects.
    """

    def test_rerun_without_registered_ldflags_does_not_raise(self, pkgconfig_env):
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--pkg-config=nested"], register_link_args=False)

            assert "LDFLAGS" not in args._registered_flag_slots, (
                "Precondition failed: LDFLAGS unexpectedly registered; this test would be vacuous."
            )
            assert "testpkg1" in args.CFLAGS, (
                "Precondition failed: pkg-config --cflags did not land, so the re-run would be vacuous."
            )
            assert args.LDFLAGS == "", "Precondition failed: pass 1 must not land --libs in the unregistered slot."

            apptools.resubstitute(args)

            assert args.LDFLAGS == "", "Re-run landed pkg-config --libs in the unregistered LDFLAGS slot."

    def test_rerun_pipeline_sees_pass1_namespace_shape(self, pkgconfig_env):
        """The seed restore must also restore namespace SHAPE: slots
        _finalize_flag_state materialized after pass 1 (LDFLAGS = "" for a
        three-slot CAP) must be absent again while pass 2's pipeline runs,
        so hasattr-based applicability decisions cannot flip between
        passes (the want_libs bug class, closed structurally).

        This is a property of the LEGACY substitutions() pipeline's own
        seed restore, not of apptools.resubstitute -- Task 9 rewrote
        resubstitute as re-gather + recompute, which never calls
        substitutions() and reads _registered_flag_slots directly (the
        hasattr-shape question is unrepresentable in the new core). Builds
        the namespace via _old_pipeline_args_in_temp_repo (a genuine pass-1
        substitutions() run, so args._substitution_seed exists) and calls
        substitutions() directly so the probe still exercises the
        semantics it names."""
        with uth.TempDirContext():
            args = _old_pipeline_args_in_temp_repo(extra_argv=["--pkg-config=nested"], register_link_args=False)

            assert "LDFLAGS" not in args._registered_flag_slots, (
                "Precondition failed: LDFLAGS unexpectedly registered; this test would be vacuous."
            )
            assert hasattr(args, "LDFLAGS"), (
                "Precondition failed: _finalize_flag_state did not materialize LDFLAGS after pass 1."
            )

            seen = {}

            def shape_probe(args):
                # Runs inside pass 2, after _commonsubstitutions but before
                # _finalize_flag_state re-materializes the slot.
                seen["ldflags_present"] = hasattr(args, "LDFLAGS")

            apptools._substitutioncallbacks.append(shape_probe)
            try:
                apptools.substitutions(args, verbose=0)
            finally:
                apptools.resetcallbacks()

            assert seen["ldflags_present"] is False, (
                "Pass 2's pipeline saw the LDFLAGS slot pass 1's pipeline never had: "
                "the seed restore did not delete the materialized slot."
            )
            assert args.LDFLAGS == "", "LDFLAGS must be re-materialized after the pass completes."


class TestCdbAutoConverges:
    """compilation_database's --auto refresh routes through
    apptools.resubstitute (test_cdb_rerun_site_uses_resubstitute pins the
    call site). Before Task 9, two pkg-config packages sharing a
    Cflags/Libs token hard-errored on this exact path: resubstitute
    replayed the legacy substitutions() pipeline, whose dedup-then-append
    ordering could not reproduce the new core's end-of-pipeline dedup (D5
    surfacing as re-run drift; task-8-report.md Concern #1a). resubstitute
    is now re-gather + recompute, so the re-run always reproduces pass 1's
    core exactly -- there is no drift left for the except-RuntimeError
    branch in compilation_database.main to render."""

    def _write_overlapping_pc_files(self, tmp_path):
        for name, lib in (("ctcdb9alpha", "-lctcdb9alpha"), ("ctcdb9beta", "-lctcdb9beta")):
            (tmp_path / f"{name}.pc").write_text(
                f"Name: {name}\nDescription: cdb overlap pin\nVersion: 1.0.0\n"
                "Cflags: -DCTCDB9_COMMON\n"
                f"Libs: -L/usr/local/lib -lctcdb9common {lib}\n"
            )

    def _run(self, tmp_config, extra_argv=()):
        with uth.ParserContext():
            return cdb.main(
                [
                    "--config=" + tmp_config,
                    "--auto",
                    "--pkg-config=ctcdb9alpha ctcdb9beta",
                    *extra_argv,
                ]
            )

    @uth.requires_functional_compiler
    def test_default_verbosity_succeeds(self, tmp_path, monkeypatch, capsys):
        self._write_overlapping_pc_files(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            pathlib.Path("main.cpp").write_text("int main() { return 0; }\n")
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                result = self._run(temp_config_name)

        assert result == 0
        err = capsys.readouterr().err
        assert "unexplained flag drift" not in err, f"Unexpected drift message on stderr: {err!r}"

    @uth.requires_functional_compiler
    def test_verbose_two_also_succeeds(self, tmp_path, monkeypatch):
        self._write_overlapping_pc_files(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            pathlib.Path("main.cpp").write_text("int main() { return 0; }\n")
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                result = self._run(temp_config_name, extra_argv=("-vv",))

        assert result == 0


class TestExtendIncludesUsingGitRootIdempotent:
    def test_second_call_does_not_duplicate_gitroot(self):
        """INCLUDE is excluded from the substitutions() seed (it is a
        legitimate between-pass input), so its self-append needs its own
        already-present check: without one, every re-run appends the same
        gitroot again."""
        ns = argparse.Namespace(INCLUDE="", git_root=True, filename=[__file__], verbose=0)
        apptools._extend_includes_using_git_root(ns)
        once = ns.INCLUDE
        assert once.strip(), "Precondition failed: no gitroot was appended on the first call."
        apptools._extend_includes_using_git_root(ns)
        assert once == ns.INCLUDE, (
            f"Second call duplicated gitroot entries:\n  once:  {once!r}\n  twice: {ns.INCLUDE!r}"
        )


def test_cdb_rerun_site_uses_resubstitute():
    """compilation_database's --auto refresh must go through the sanctioned
    re-run path so it gets the same drift guard as cake's re-run site; a
    bare substitutions() call re-runs the pipeline with no drift check.
    AST-based so comments/docstrings mentioning either name cannot trip it."""
    import ast

    import compiletools.compilation_database

    source = pathlib.Path(compiletools.compilation_database.__file__).read_text()
    called = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        # Attribute form (apptools.resubstitute(...)) and bare-name form
        # (from ... import substitutions; substitutions(...)) both count.
        if isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "resubstitute" in called, "compilation_database no longer routes its re-run through apptools.resubstitute"
    assert "substitutions" not in called, (
        "compilation_database re-runs substitutions() directly, bypassing the drift guard"
    )
