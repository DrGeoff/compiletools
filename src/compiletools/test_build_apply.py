import argparse
import dataclasses
import os

from compiletools.build_apply import apply_effects, populate_args
from compiletools.build_context import BuildContext
from compiletools.build_inputs import BuildInputs
from compiletools.build_state import EnsureLinkerSymlinkDir, compute_build_state

ALL_SLOTS = frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"})


def _state(**kw):
    kw.setdefault("registered_slots", ALL_SLOTS)
    kw.setdefault("variant_raw", "gcc.debug")
    kw.setdefault("canonical_order", ("gcc", "debug"))
    return compute_build_state(BuildInputs(**kw))


class TestApplyEffects:
    def test_setenv_writes_and_saves_original(self, monkeypatch):
        monkeypatch.setenv("PKG_CONFIG_PATH", "/old")
        context = BuildContext()
        apply_effects(_state(pkg_config_path="/new"), context)
        assert os.environ["PKG_CONFIG_PATH"] == "/new"
        assert context._original_pkg_config_path == "/old"

    def test_setenv_saves_true_sentinel_when_previously_unset(self, monkeypatch):
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        context = BuildContext()
        apply_effects(_state(pkg_config_path="/new"), context)
        assert os.environ["PKG_CONFIG_PATH"] == "/new"
        assert context._original_pkg_config_path is True

    def test_rerun_does_not_clobber_saved_original(self, monkeypatch):
        monkeypatch.setenv("PKG_CONFIG_PATH", "/old")
        context = BuildContext()
        apply_effects(_state(pkg_config_path="/new1"), context)
        apply_effects(_state(pkg_config_path="/new2"), context)
        assert os.environ["PKG_CONFIG_PATH"] == "/new2"
        assert context._original_pkg_config_path == "/old"

    def test_no_effects_is_noop(self, monkeypatch):
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        apply_effects(_state(), BuildContext())
        assert "PKG_CONFIG_PATH" not in os.environ

    def test_ensure_linker_symlink_dir_resolves_target_via_which(self, tmp_path, monkeypatch):
        wild_path = str(tmp_path / "usr" / "bin" / "wild")
        monkeypatch.setattr("shutil.which", lambda name: wild_path if name == "wild" else None)
        directory = str(tmp_path / ".ct-wild-ld")
        effect = EnsureLinkerSymlinkDir(directory=directory, link_name="ld", target="wild")
        state = dataclasses.replace(compute_build_state(BuildInputs(registered_slots=ALL_SLOTS)), effects=(effect,))
        apply_effects(state, BuildContext())
        link = os.path.join(directory, "ld")
        assert os.path.islink(link)
        assert os.readlink(link) == wild_path

    def test_ensure_linker_symlink_dir_skips_when_target_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        directory = str(tmp_path / ".ct-wild-ld")
        effect = EnsureLinkerSymlinkDir(directory=directory, link_name="ld", target="wild")
        state = dataclasses.replace(compute_build_state(BuildInputs(registered_slots=ALL_SLOTS)), effects=(effect,))
        apply_effects(state, BuildContext())
        assert not os.path.exists(directory)

    def test_ensure_linker_symlink_dir_idempotent_when_link_already_present(self, tmp_path, monkeypatch):
        wild_path = str(tmp_path / "usr" / "bin" / "wild")
        monkeypatch.setattr("shutil.which", lambda name: wild_path if name == "wild" else None)
        directory = str(tmp_path / ".ct-wild-ld")
        os.makedirs(directory)
        link = os.path.join(directory, "ld")
        os.symlink("/somewhere/else", link)
        effect = EnsureLinkerSymlinkDir(directory=directory, link_name="ld", target="wild")
        state = dataclasses.replace(compute_build_state(BuildInputs(registered_slots=ALL_SLOTS)), effects=(effect,))
        apply_effects(state, BuildContext())
        # apply_effects only creates the link if missing; a pre-existing link
        # (even pointing elsewhere) is left alone -- matches the brief's
        # "create ... if missing" contract (not the original's readlink-repair).
        assert os.readlink(link) == "/somewhere/else"


class TestPopulateArgs:
    def test_flag_slots_are_never_written(self):
        """The flag surface is state-only: populate_args must not create
        or overwrite any raw slot attr, so gather re-reads the same base
        on every pass and no record/restore machinery exists."""
        state = _state(cxxflags=("-DDERIVED",), gitroot="/repo")
        args = argparse.Namespace(verbose=0, CXXFLAGS="-O2 raw")
        populate_args(args, state)
        assert args.CXXFLAGS == "-O2 raw"
        assert not hasattr(args, "CPPFLAGS")
        assert not hasattr(args, "CXXFLAGS_tokens")
        assert not hasattr(args, "flags")
        assert not hasattr(args, "_raw_flag_slots")
        assert not hasattr(args, "_flag_string_snapshot")
        assert not hasattr(args, "_registered_flag_slots")

    def test_name_attrs_are_written(self):
        state = _state(cxxflags=("-O2",), gitroot="/repo")
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert args.variant == "gcc.debug"
        assert args.bindir == "bin/gcc.debug"

    def test_cas_dir_attrs_use_real_dest_names(self):
        state = _state(
            cas_objdir_raw="/cas/obj", cas_pchdir_raw="/cas/pch", cas_pcmdir_raw="/cas/pcm", cas_exedir_raw="/cas/exe"
        )
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert args.cas_objdir == state.names.cas_objdir
        assert args.cas_pchdir == state.names.cas_pchdir
        assert args.cas_pcmdir == state.names.cas_pcmdir
        assert args.cas_exedir == state.names.cas_exedir

    def test_round_trip_through_gather_keeps_ldflags_unregistered(self):
        """want_libs loop closure: a populate_args'd namespace fed back to
        gather_inputs must report LDFLAGS unregistered — structural now,
        since populate_args never materializes slot attrs."""
        from compiletools.build_inputs import gather_inputs

        state = _state(registered_slots=frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"}))
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert not hasattr(args, "LDFLAGS")
        inputs = gather_inputs(args, BuildContext())
        assert "LDFLAGS" not in inputs.registered_slots

    def test_stashes_build_state_on_namespace(self):
        """Consumer surface: populate_args stashes the BuildState itself so
        consumers read args._build_state via get_build_state. Refreshed on
        every call: consumers must see the CURRENT pass's state after a
        re-run."""
        state1 = _state(cxxflags=("-DPASS1",))
        args = argparse.Namespace(verbose=0)
        populate_args(args, state1)
        assert args._build_state is state1
        state2 = _state(cxxflags=("-DPASS2",))
        populate_args(args, state2)
        assert args._build_state is state2

    def test_get_build_state_accessor_and_missing_error(self):
        """get_build_state returns the stash; on a namespace that never went
        through populate_args it raises a named error pointing at the
        fixture gap (not a bare AttributeError)."""
        import pytest

        from compiletools.build_apply import get_build_state

        state = _state()
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert get_build_state(args) is state

        bare = argparse.Namespace(verbose=0)
        with pytest.raises(RuntimeError, match="populate_args"):
            get_build_state(bare)

    def test_finalize_flag_state_routes_through_populate_args(self):
        """testhelper.finalize_flag_state must not hand-mirror
        populate_args' namespace writes: it builds a synthetic state and
        routes it through the REAL populate_args (one writer). The stash
        round-trips the fixture's own values and the raw slot attrs are
        untouched."""
        import compiletools.testhelper as uth
        from compiletools.build_apply import get_build_state

        args = argparse.Namespace(verbose=0, CXXFLAGS="-O2", CFLAGS="", CPPFLAGS="-DFIX")
        uth.finalize_flag_state(args)
        state = get_build_state(args)
        assert state.cxxflags == "-O2"
        assert state.cppflags == "-DFIX"
        assert state.registered_slots == frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"})
        assert args.CXXFLAGS == "-O2"
        assert not hasattr(args, "CXXFLAGS_tokens")

    def test_repopulate_from_untouched_raw_slots_is_idempotent(self):
        """The re-run loop closure without any record: because the slots
        are never overwritten, gather on a populated namespace reads the
        same raw values as the first pass and produces equal inputs."""
        from compiletools.build_inputs import gather_inputs

        args = argparse.Namespace(verbose=0, CXXFLAGS="-O2 -Wall", CPPFLAGS="unsupplied")
        first = gather_inputs(args, BuildContext())
        populate_args(args, _state(cxxflags=("-DDERIVED",)))
        second = gather_inputs(args, BuildContext())
        assert first.cxxflags == second.cxxflags == ("-O2", "-Wall")
        assert first.cppflags is None and second.cppflags is None
