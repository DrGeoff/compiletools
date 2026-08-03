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
import os

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


def _parseargs_in_temp_repo(extra_argv=()):
    """Full parseargs pipeline in a temp dir, mirroring TestQuietAppliedOnce.

    Registers add_link_arguments so args.LDFLAGS exists and
    _add_flags_from_pkg_config takes its want_libs branch (same reason
    test_apptools.py's _parseargs_with_pkg_config_conf does it) -- without
    it args.LDFLAGS is never a CAP-registered slot and pkg-config --libs
    output has nowhere to land.
    """
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("idempotency test", argv=argv)
        cdb.CompilationDatabaseCreator.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
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
        assert apptools.warn_unexplained_flag_drift(flags, flags, [], verbose=0) == []

    def test_non_include_addition_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cxx=prior.cxx + ("-O3",))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [], verbose=0)
        assert len(msgs) == 1, f"expected exactly one drifted slot, got: {msgs}"
        assert "cxx" in msgs[0]
        assert "-O3" in msgs[0]

    def test_removed_token_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cpp=prior.cpp[:-1])
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [], verbose=0)
        assert len(msgs) == 1
        assert "cpp" in msgs[0]

    def test_i_pair_is_explained_only_when_path_is_in_include(self):
        prior = _drift_base_flags()
        tail = ("-I", "/ext/root")
        new = dataclasses.replace(prior, cpp=prior.cpp + tail, c=prior.c + tail, cxx=prior.cxx + tail)
        assert apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"], verbose=0) == []
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [], verbose=0)
        assert len(msgs) == 3, (
            f"-I additions for a path NOT in INCLUDE must be reported per slot (cpp, c, cxx), got: {msgs}"
        )

    def test_i_pair_inserted_mid_sequence_is_explained(self):
        prior = _drift_base_flags()
        cpp = prior.cpp[:1] + ("-I", "/ext/root") + prior.cpp[1:]
        new = dataclasses.replace(prior, cpp=cpp)
        assert apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"], verbose=0) == []

    def test_i_pair_removal_is_reported(self):
        prior = _drift_base_flags()
        with_pair = dataclasses.replace(prior, cpp=prior.cpp + ("-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(with_pair, prior, ["/ext/root"], verbose=0)
        assert len(msgs) == 1 and "cpp" in msgs[0]

    def test_i_pair_inserted_before_existing_i_pair_is_explained(self):
        prior = _drift_base_flags()
        cpp_prior = ("-I", "/existing/inc") + prior.cpp
        cpp_new = ("-I", "/ext/root", "-I", "/existing/inc") + prior.cpp
        with_pair = dataclasses.replace(prior, cpp=cpp_prior)
        new = dataclasses.replace(prior, cpp=cpp_new)
        assert apptools.warn_unexplained_flag_drift(with_pair, new, ["/ext/root"], verbose=0) == []

    def test_ld_addition_is_reported_even_in_dash_i_form(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, ld=prior.ld + ("-I", "/ext/root"))
        msgs = apptools.warn_unexplained_flag_drift(prior, new, ["/ext/root"], verbose=0)
        assert len(msgs) == 1
        assert "ld" in msgs[0]

    def test_compiler_identity_change_is_reported(self):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, compiler_identity="swapped-identity")
        msgs = apptools.warn_unexplained_flag_drift(prior, new, [], verbose=0)
        assert len(msgs) == 1
        assert "compiler_identity" in msgs[0]

    def test_warning_prints_to_stderr_only_at_verbose(self, capsys):
        prior = _drift_base_flags()
        new = dataclasses.replace(prior, cxx=prior.cxx + ("-O3",))
        apptools.warn_unexplained_flag_drift(prior, new, [], verbose=0)
        assert capsys.readouterr().err == "", "verbose=0 must not print"
        apptools.warn_unexplained_flag_drift(prior, new, [], verbose=1)
        err = capsys.readouterr().err
        assert "cxx" in err
        assert "-O3" in err


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
