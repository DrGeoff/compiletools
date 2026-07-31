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

import os

import pytest

import compiletools.apptools as apptools
import compiletools.compilation_database as cdb
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext

FLAG_SLOTS = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")


@pytest.fixture
def parsers_reset():
    """Wipe the configargparse parser cache around tests that go through
    ``parseargs`` end-to-end."""
    uth.reset()
    yield
    uth.reset()


def _parseargs_in_temp_repo(extra_argv=()):
    """Full parseargs pipeline in a temp dir, mirroring TestQuietAppliedOnce."""
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("idempotency test", argv=argv)
        cdb.CompilationDatabaseCreator.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
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
