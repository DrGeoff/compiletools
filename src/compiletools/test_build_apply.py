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

    def test_get_build_state_or_none_returns_stash_or_none(self):
        """The optional accessor: the stash when populate_args ran, None
        otherwise. It is the primitive get_build_state raises on top of, so
        the two must agree on the populated case by identity."""
        from compiletools.build_apply import get_build_state, get_build_state_or_none

        state = _state()
        args = argparse.Namespace(verbose=0)
        populate_args(args, state)
        assert get_build_state_or_none(args) is state
        assert get_build_state(args) is get_build_state_or_none(args)

        assert get_build_state_or_none(argparse.Namespace(verbose=0)) is None

    def test_no_module_reaches_past_the_build_state_accessors(self):
        """Lint: nothing touches the ``args._build_state`` stash outside the
        accessors -- not by dot-access, not by ``getattr``/``setattr``, not
        through a ``__dict__`` or ``vars()`` subscript.

        ONE predicate over two node kinds, rather than one lint per spelling:
        an ``ast.Attribute`` named ``_build_state`` (dot-access), and an
        ``ast.Constant`` whose value IS the bare stash name (every string
        spelling at once). The constant arm strictly subsumes a
        ``getattr``-shaped regex, which is why there is no longer a second
        lint beside this one: ``args.__dict__["_build_state"]`` and
        ``vars(args)["_build_state"]`` both carry the name as a literal and
        neither is a ``getattr`` call, so the regex scored them clean.

        Backends' ``self._build_state`` is a different object (the instance
        attribute a backend set from ``get_build_state(args)``) and is
        deliberately not matched.

        ``ast``, not text: ``apptools`` has three docstrings that NAME the
        stash. Those are prose about the contract, not uses of it, and only
        a parse separates them -- a docstring is a single Constant holding
        the whole paragraph, never one equal to the bare name.

        Exempt files are checked live, so a stale entry fails here instead
        of silently widening the lint. Within ``build_apply`` the two
        legitimate sites are pinned to the functions that own them, so a
        third use elsewhere in that same file is still caught.
        """
        import ast
        import pathlib

        # ``populate_args`` is the writer and ``get_build_state_or_none`` the
        # sole reader; ``stub_build_state`` plants real name values on a
        # MagicMock args, whose auto-created stash cannot be reached through
        # an accessor that only reads.
        exempt = {
            "build_apply.py": "populate_args (writer) + get_build_state_or_none (reader)",
            "testhelper.py": "stub_build_state, MagicMock planter",
        }
        build_apply_owners = ("populate_args", "get_build_state_or_none")

        def stash_sites(tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "_build_state":
                    if isinstance(node.value, ast.Name) and node.value.id == "self":
                        continue
                    yield node.lineno
                elif isinstance(node, ast.Constant) and node.value == "_build_state":
                    yield node.lineno

        srcdir = pathlib.Path(__file__).parent
        sites: dict[str, list[str]] = {}
        trees = {}
        for path in sorted(srcdir.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            trees[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno in stash_sites(trees[path.name]):
                sites.setdefault(path.name, []).append(f"{path.name}:{lineno}")

        unexpected = {name: hits for name, hits in sites.items() if name not in exempt}
        assert not unexpected, f"read args._build_state through build_apply's accessors instead; found: {unexpected}"

        stale = [f"{name} ({why})" for name, why in exempt.items() if name not in sites]
        assert not stale, f"exemption no longer needed, drop it: {stale}"

        # The build_apply exemption is per-file, so confirm each of its hits
        # really is inside an owning accessor rather than merely sharing the file.
        owned = [
            range(node.lineno, (node.end_lineno or node.lineno) + 1)
            for node in ast.walk(trees["build_apply.py"])
            if isinstance(node, ast.FunctionDef) and node.name in build_apply_owners
        ]
        assert len(owned) == len(build_apply_owners), f"expected {build_apply_owners} in build_apply.py, found {owned}"
        strays = [
            lineno for lineno in stash_sites(trees["build_apply.py"]) if not any(lineno in span for span in owned)
        ]
        assert not strays, f"build_apply.py touches the stash outside {build_apply_owners} at lines {strays}"

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


class TestConfigurePkgConfigErrors:
    def test_namespace_without_the_option_leaves_an_armed_policy_alone(self):
        """A parser that never registered ``--pkg-config-errors`` carries no
        policy, so applying its namespace must not disarm strict mode.

        The policy is process-global. A second ``parseargs`` over a
        base-arguments-only namespace used to reset it to the ``warn``
        default nobody asked for, and every later probe in that process
        silently degraded to a warning.
        """
        from compiletools.apptools_pkgconfig import get_pkg_config_errors, set_pkg_config_errors
        from compiletools.build_apply import configure_pkg_config_errors

        set_pkg_config_errors("error")
        configure_pkg_config_errors(argparse.Namespace())
        assert get_pkg_config_errors() == "error"

    def test_parsed_value_is_applied(self):
        from compiletools.apptools_pkgconfig import get_pkg_config_errors
        from compiletools.build_apply import configure_pkg_config_errors

        configure_pkg_config_errors(argparse.Namespace(pkg_config_errors="error"))
        assert get_pkg_config_errors() == "error"

    def test_an_explicit_warn_value_still_disarms(self):
        """Only the absent attribute is a no-op; a parsed ``warn`` is a
        policy the user's config asked for and still applies."""
        from compiletools.apptools_pkgconfig import get_pkg_config_errors, set_pkg_config_errors
        from compiletools.build_apply import configure_pkg_config_errors

        set_pkg_config_errors("error")
        configure_pkg_config_errors(argparse.Namespace(pkg_config_errors="warn"))
        assert get_pkg_config_errors() == "warn"
