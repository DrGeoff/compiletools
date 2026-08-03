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
    """

    def _assert_rerun_is_noop(self, extra_argv):
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=extra_argv)
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
        """The comparison is pure: surfacing the messages is the caller's
        job (resubstitute raises). No printing at any drift level."""
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cxx=prior.cxx + ("-O3",))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [])
        assert len(msgs) == 1, "Precondition failed: drift must be reported for this check to be meaningful."
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


@pytest.mark.usefixtures("parsers_reset")
class TestResubstitute:
    """apptools.resubstitute is the only sanctioned substitutions() re-run
    path: snapshot args.flags, re-run, hard-error on drift the INCLUDE
    widening doesn't explain."""

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

    def test_non_idempotent_callback_raises(self):
        import itertools

        counter = itertools.count(1)

        def nonidempotent_callback(cb_args):
            # A per-pass-unique token: seed restore cannot make this
            # converge, so it is exactly the drift class resubstitute
            # must reject.
            token = f"-DCT_TEST_RERUN{next(counter)}"
            for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
                current = getattr(cb_args, slot, "") or ""
                setattr(cb_args, slot, f"{current} {token}".strip())

        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            # Registered AFTER parseargs: ParserContext (inside the helper)
            # wipes the callback registry on entry and exit, and the re-run
            # happens outside any ParserContext anyway.
            apptools.registercallback(nonidempotent_callback)
            try:
                with pytest.raises(RuntimeError, match="CT_TEST_RERUN"):
                    apptools.resubstitute(args)
            finally:
                apptools.resetcallbacks()


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
        passes (the want_libs bug class, closed structurally)."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--pkg-config=nested"], register_link_args=False)

            assert "LDFLAGS" not in args._registered_flag_slots, (
                "Precondition failed: LDFLAGS unexpectedly registered; this test would be vacuous."
            )
            assert hasattr(args, "LDFLAGS"), (
                "Precondition failed: _finalize_flag_state did not materialize LDFLAGS after pass 1."
            )

            seen = {}

            def shape_probe(cb_args):
                # Runs inside pass 2, after _commonsubstitutions but before
                # _finalize_flag_state re-materializes the slot.
                seen["ldflags_present"] = hasattr(cb_args, "LDFLAGS")

            apptools.registercallback(shape_probe)
            try:
                apptools.resubstitute(args)
            finally:
                apptools.resetcallbacks()

            assert seen["ldflags_present"] is False, (
                "Pass 2's pipeline saw the LDFLAGS slot pass 1's pipeline never had: "
                "the seed restore did not delete the materialized slot."
            )
            assert args.LDFLAGS == "", "LDFLAGS must be re-materialized after the pass completes."


def _register_drift_forcing_callback():
    """Same trick as TestResubstitute.test_non_idempotent_callback_raises,
    reused here because compilation_database.main() drives its own
    parseargs + resubstitute internally -- there is no hook point between
    the two, so the callback must already be registered before main() runs
    and stay live across both of its substitutions() passes."""
    counter = itertools.count(1)

    def nonidempotent_callback(cb_args):
        token = f"-DCT_TEST_CDB_RERUN{next(counter)}"
        for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
            current = getattr(cb_args, slot, "") or ""
            setattr(cb_args, slot, f"{current} {token}".strip())

    apptools.registercallback(nonidempotent_callback)


class TestCdbFatalRendering:
    """compilation_database.main's except-RuntimeError branch around its
    apptools.resubstitute call renders the drift error as a fatal message
    (cake's _FATAL_ERROR_RENDERERS contract) at default verbosity and
    re-raises at -vv. Forces the drift with the same non-idempotent-callback
    trick TestResubstitute uses: pass 1 (main's own parseargs) forms the
    baseline with one token, and main's internal resubstitute (pass 2) adds
    a different, per-call-unique token that the seed restore cannot
    reconcile away.
    """

    def _run(self, tmp_config, extra_argv=()):
        with uth.ParserContext():
            # ParserContext.__enter__ already wiped the callback registry;
            # register here so it survives for the duration of main()'s
            # internal parseargs pass AND its resubstitute re-run.
            _register_drift_forcing_callback()
            try:
                return cdb.main(["--config=" + tmp_config, "--auto", *extra_argv])
            finally:
                apptools.resetcallbacks()

    @uth.requires_functional_compiler
    def test_default_verbosity_prints_and_returns_1(self, capsys):
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            pathlib.Path("main.cpp").write_text("int main() { return 0; }\n")
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                result = self._run(temp_config_name)

        assert result == 1
        err = capsys.readouterr().err
        assert "unexplained flag drift" in err, f"Expected drift message on stderr, got: {err!r}"

    @uth.requires_functional_compiler
    def test_verbose_two_propagates_runtimeerror(self):
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            pathlib.Path("main.cpp").write_text("int main() { return 0; }\n")
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                with pytest.raises(RuntimeError, match="unexplained flag drift"):
                    self._run(temp_config_name, extra_argv=("-vv",))


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
