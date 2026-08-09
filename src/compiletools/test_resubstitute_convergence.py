"""apptools.resubstitute must converge with a fresh single pass.

cake and ct-compilation-database legitimately re-run the build-state
computation on one namespace (--auto second-stage discovery, //#GIT=
external fetch, the CDB refresh). resubstitute is the only sanctioned
re-run path: re-gather + recompute over the pure core, a fixed point by
construction. Any divergence between a widened re-run and a fresh
single-pass parse over equivalent inputs forks the object CAS key space
(flag-slot hashing is argv-shaped), so convergence is pinned here as a
first-class contract with a fresh-parse oracle.
"""

import os
import pathlib

import pytest

import compiletools.apptools as apptools
import compiletools.compilation_database as cdb
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.build_apply import get_build_state
from compiletools.build_context import BuildContext
from compiletools.build_inputs import gather_inputs


@pytest.fixture
def parsers_reset():
    """Wipe the configargparse parser cache around tests that go through
    ``parseargs`` end-to-end."""
    uth.reset()
    yield
    uth.reset()


def _parseargs_in_temp_repo(extra_argv=(), register_link_args=True):
    """Full parseargs pipeline in a temp dir.

    Registers add_link_arguments so args.LDFLAGS is a CAP-registered slot
    and pkg-config --libs output has somewhere to land. Pass
    register_link_args=False to build the three-slot CAP shape
    (ct-compilation-database registers no LDFLAGS).
    """
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("resubstitute test", argv=argv)
        cdb.CompilationDatabaseCreator.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        if register_link_args:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            return apptools.parseargs(cap, argv, context=BuildContext())


@pytest.mark.usefixtures("parsers_reset")
class TestResubstitute:
    """re-gather + recompute: fixed point by construction, converging with
    a fresh single-pass parse over equivalent inputs."""

    def test_include_widening_rerun_does_not_raise(self):
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)
            args.INCLUDE = (args.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(args)
            cpp_tokens = get_build_state(args).flags.cpp
            assert newdir in cpp_tokens, (
                f"Precondition failed: the re-run did not land the -I pair, so the "
                f"no-raise outcome would be vacuous. cpp slot: {cpp_tokens}"
            )

    def test_include_widening_converges_with_fresh_single_pass(self):
        """The --auto (two-pass: parse, then widen INCLUDE and resubstitute)
        vs --no-auto (one pass, INCLUDE known up front) convergence:
        re-gather + recompute must land on exactly the same Flags a
        fresh single-pass parseargs would compute over equivalent inputs."""
        with uth.TempDirContext():
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)

            widened = _parseargs_in_temp_repo()
            widened.INCLUDE = (widened.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(widened)

            fresh = _parseargs_in_temp_repo(extra_argv=[f"--append-INCLUDE={newdir}"])

            widened_flags = get_build_state(widened).flags
            fresh_flags = get_build_state(fresh).flags
            assert widened_flags == fresh_flags, (
                f"--auto (widen + resubstitute) diverged from a fresh single-pass parse:\n"
                f"  widened: {widened_flags}\n  fresh:   {fresh_flags}"
            )

    def test_include_widening_converges_with_fresh_single_pass_under_separate_flags(self):
        """Same convergence oracle under --separate-flags-CPP-CXX. This
        mode is the one where the untouched raw slot is load-bearing:
        with stage_unify skipped, args.CPPFLAGS can still hold the raw
        _UNSUPPLIED_USE_CXXFLAGS sentinel post-parse (populate_args never
        materializes a concrete string over it), and gather must map that
        sentinel to unsupplied on every re-run, or the widened run's
        cppflags would permanently diverge from a fresh single pass'."""
        with uth.TempDirContext():
            newdir = os.path.join(os.getcwd(), "external_inc")
            os.makedirs(newdir)

            widened = _parseargs_in_temp_repo(extra_argv=["--separate-flags-CPP-CXX"])
            widened.INCLUDE = (widened.INCLUDE + " " + newdir).strip()
            apptools.resubstitute(widened)

            fresh = _parseargs_in_temp_repo(extra_argv=["--separate-flags-CPP-CXX", f"--append-INCLUDE={newdir}"])

            widened_flags = get_build_state(widened).flags
            fresh_flags = get_build_state(fresh).flags
            assert widened_flags == fresh_flags, (
                f"--auto (widen + resubstitute) diverged from a fresh single-pass parse "
                f"under --separate-flags-CPP-CXX:\n  widened: {widened_flags}\n  fresh:   {fresh_flags}"
            )

    def test_auto_rerun_with_overlapping_pkg_config_converges(self, tmp_path, monkeypatch):
        """Two pkg-config packages sharing a Cflags/Libs token: the core
        dedups the shared tokens at the END of the pipeline (stage_dedup,
        blessed divergence D5), and the re-run must reproduce pass 1's
        already-deduped result exactly."""
        for name, lib in (("ctresub9alpha", "-lctresub9alpha"), ("ctresub9beta", "-lctresub9beta")):
            (tmp_path / f"{name}.pc").write_text(
                f"Name: {name}\nDescription: resubstitute overlap pin\nVersion: 1.0.0\n"
                "Cflags: -DCTRESUB9_COMMON\n"
                f"Libs: -L/usr/local/lib -lctresub9common {lib}\n"
            )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--pkg-config=ctresub9alpha ctresub9beta"])
            flags = get_build_state(args).flags
            assert flags.c.count("-DCTRESUB9_COMMON") == 1, (
                "Precondition failed: pass 1's core did not dedup the shared cflags token."
            )
            assert flags.ld.count("-lctresub9common") == 1, (
                "Precondition failed: pass 1's core did not dedup the shared libs token."
            )

            apptools.resubstitute(args)

            flags = get_build_state(args).flags
            assert flags.c.count("-DCTRESUB9_COMMON") == 1
            assert flags.ld.count("-lctresub9common") == 1
            assert flags.ld.count("-L/usr/local/lib") == 1

    def test_rerun_when_nothing_changed_is_the_identity(self):
        """resubstitute on an unmodified namespace reproduces the Flags
        exactly — f(x) == f(x), no seed choreography required."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo()
            first_flags = get_build_state(args).flags
            apptools.resubstitute(args)
            assert get_build_state(args).flags == first_flags, (
                "resubstitute must converge to the same flags when nothing input-affecting changed."
            )

    def test_rerun_does_not_redecrement_verbose(self):
        """parseargs folds --quiet into args.verbose exactly once and
        latches _quiet_applied; the re-gather a resubstitute performs
        must honour the latch rather than subtracting quiet again. The
        historical regression was cake's --auto second stage running
        with a doubly-decremented verbosity."""
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--quiet", "--quiet"])
            settled = args.verbose
            assert args.quiet == 2, "Precondition failed: --quiet --quiet must count to 2 or the latch is untested."

            apptools.resubstitute(args)

            assert args.verbose == settled, "resubstitute must not mutate the already-latched verbosity."
            inputs = gather_inputs(args, args._context)
            assert inputs.verbose == settled, (
                f"Re-gather subtracted quiet a second time: expected verbose {settled}, got {inputs.verbose}."
            )


@pytest.mark.usefixtures("parsers_reset")
class TestThreeSlotCapRerun:
    """A CAP that registers no LDFLAGS (ct-compilation-database) must stay
    a three-slot tool across a re-run: want_libs is a schema question
    (inputs.registered_slots), so pkg-config --libs can never land in a
    slot the CAP never registered, no matter how many re-runs occur."""

    def test_rerun_without_registered_ldflags_does_not_raise(self, pkgconfig_env):
        with uth.TempDirContext():
            args = _parseargs_in_temp_repo(extra_argv=["--pkg-config=nested"], register_link_args=False)

            state = get_build_state(args)
            assert "LDFLAGS" not in state.registered_slots, (
                "Precondition failed: LDFLAGS unexpectedly registered; this test would be vacuous."
            )
            assert any("testpkg1" in tok for tok in state.flags.c), (
                "Precondition failed: pkg-config --cflags did not land, so the re-run would be vacuous."
            )
            assert state.flags.ld == (), "Precondition failed: pass 1 must not land --libs in the unregistered slot."

            apptools.resubstitute(args)

            assert get_build_state(args).flags.ld == (), (
                "Re-run landed pkg-config --libs in the unregistered LDFLAGS slot."
            )


class TestCdbAutoConverges:
    """compilation_database's --auto refresh routes through
    apptools.resubstitute (test_cdb_rerun_site_uses_resubstitute pins the
    call site) and must converge cleanly even when pkg-config packages
    share Cflags/Libs tokens (the D5 shape)."""

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
    def test_default_verbosity_succeeds(self, tmp_path, monkeypatch):
        self._write_overlapping_pc_files(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            pathlib.Path("main.cpp").write_text("int main() { return 0; }\n")
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                result = self._run(temp_config_name)

        assert result == 0

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


def test_cdb_rerun_site_uses_resubstitute():
    """compilation_database's --auto refresh must go through the sanctioned
    re-run path (re-gather + recompute) rather than hand-rolling its own
    refresh. AST-based so comments/docstrings mentioning either name
    cannot trip it."""
    import ast

    import compiletools.compilation_database

    source = pathlib.Path(compiletools.compilation_database.__file__).read_text()
    called = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        # Attribute form (apptools.resubstitute(...)) and bare-name form
        # (from ... import X; X(...)) both count.
        if isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert "resubstitute" in called, "compilation_database no longer routes its re-run through apptools.resubstitute"
