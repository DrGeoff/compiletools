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
    def test_legacy_surface_is_complete(self):
        state = _state(cxxflags=("-O2",), gitroot="/repo")
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert state.cxxflags == args.CXXFLAGS
        assert list(args.CXXFLAGS_tokens) == list(state.tokens.cxx)
        assert args.flags == state.flags
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

    def test_flag_string_snapshot_matches_written_strings(self):
        state = _state(cxxflags=("-O2",))
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        snapshot = dict(args._flag_string_snapshot)
        assert snapshot["CXXFLAGS"] == args.CXXFLAGS

    def test_flag_string_snapshot_only_covers_registered_slots(self):
        state = _state(cxxflags=("-O2",), registered_slots=frozenset({"CXXFLAGS"}))
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        snapshot = dict(args._flag_string_snapshot)
        assert set(snapshot) == {"CXXFLAGS"}
        # all four slots are still materialized as raw strings/tokens for
        # downstream consumers even though only CXXFLAGS is registered.
        assert state.cppflags == args.CPPFLAGS
        assert list(args.CPPFLAGS_tokens) == list(state.tokens.cpp)
