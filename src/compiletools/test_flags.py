"""Tests for compiletools.flags.Flags dataclass (TOKEN-5)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys

import configargparse
import pytest

import compiletools.apptools
import compiletools.configutils
import compiletools.testhelper as uth
import compiletools.utils as utils
from compiletools.build_context import BuildContext
from compiletools.flags import Flags


@pytest.fixture
def parsers_reset():
    """Wipe the global configargparse parser cache + apptools callbacks
    around tests that go through ``parseargs`` end-to-end."""
    uth.delete_existing_parsers()
    yield
    uth.delete_existing_parsers()


def _parseargs_with_temp_config(tmp_path, description):
    """Build the standard test parser, run parseargs end-to-end, return args."""
    temp_config_name = uth.create_temp_config(str(tmp_path))
    argv = ["--config=" + temp_config_name]
    config_files = compiletools.configutils.config_files_from_variant(argv=argv, exedir=uth.cakedir())
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        description=description,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        default_config_files=config_files,
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.apptools.add_common_arguments(cap)
    compiletools.apptools.add_link_arguments(cap)
    return compiletools.apptools.parseargs(cap, argv, context=BuildContext())


@pytest.mark.parametrize(
    ("cxx_flags", "expected"),
    [
        pytest.param(("-O2", "-DFOO", "-Wall", "-Werror"), ["-O2", "-Werror"], id="strip-d-and-diagnostic"),
        # Exact diagnostic-only tokens must not eat longer flag names with the
        # same prefix.
        pytest.param(("-pipefoo", "-pipe"), ["-pipefoo"], id="keep-pipe-prefix-lookalike"),
        # Same boundary as -pipefoo: the exact -v rule must not strip a
        # hypothetical future -vN flag.
        pytest.param(("-vN", "-v"), ["-vN"], id="keep-v-prefix-lookalike"),
        pytest.param(("-O2", "-fdiagnostics-color=auto"), ["-O2"], id="drop-diagnostics-color-value"),
        # Detached -D FOO and -U BAR forms must be stripped as pairs, not just
        # as lone option tokens.
        pytest.param(("-O2", "-D", "FOO", "-U", "BAR", "-Wall"), ["-O2"], id="strip-detached-d-and-u"),
        # -Werror can change build outcome, so it remains hash-relevant even
        # though ordinary warning flags are diagnostic-only.
        pytest.param(("-Werror=return-type", "-Wall"), ["-Werror=return-type"], id="keep-werror-value"),
    ],
)
def test_flags_hash_relevant(cxx_flags, expected):
    flags = Flags(cxx=cxx_flags)
    assert flags.hash_relevant("cxx") == expected


@pytest.mark.parametrize(
    ("cpp_flags", "expected"),
    [
        pytest.param(("-I/a", "-O2"), {"/a"}, id="attached"),
        pytest.param(("-I", "/a", "-O2"), {"/a"}, id="detached"),
        pytest.param(("-isystem", "/a"), set(), id="ignore-isystem"),
    ],
)
def test_flags_existing_include_paths(cpp_flags, expected):
    flags = Flags(cpp=cpp_flags)
    assert flags.existing_include_paths("cpp") == expected


def test_flags_append_include_adds_when_missing_returns_new():
    flags = Flags(cpp=("-O2",))
    updated = flags.append_include("/new", slots=("cpp",))
    assert updated.cpp == ("-O2", "-I", "/new")
    # Original is unchanged (frozen).
    assert flags.cpp == ("-O2",)


@pytest.mark.parametrize(
    "cpp_flags",
    [
        pytest.param(("-I/existing",), id="attached"),
        pytest.param(("-I", "/existing"), id="detached"),
    ],
)
def test_flags_append_include_skips_when_present_returns_self(cpp_flags):
    flags = Flags(cpp=cpp_flags)
    updated = flags.append_include("/existing", slots=("cpp",))
    assert updated is flags


def test_flags_append_include_default_slots_all_three():
    flags = Flags()
    updated = flags.append_include("/x")
    assert updated.cpp == ("-I", "/x")
    assert updated.c == ("-I", "/x")
    assert updated.cxx == ("-I", "/x")
    assert updated.ld == ()


def test_flags_is_frozen_and_hashable():
    """Flags must be hashable so it can be used as a dict key or set
    member; frozen so consumers cannot mutate the underlying tuples."""
    a = Flags(cpp=("-O2",))
    b = Flags(cpp=("-O2",))
    assert hash(a) == hash(b)
    assert a == b
    assert {a, b} == {a}
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.cpp = ("-O0",)  # type: ignore[misc]


@pytest.mark.usefixtures("parsers_reset")
def test_parseargs_stashes_flags_and_leaves_slots_raw(tmp_path):
    """parseargs must stash a BuildState whose flags is a Flags instance,
    and must NOT write derived flag attrs onto the namespace — the raw
    args.{*FLAGS} strings stay gather's input, so a token the pure core
    derives (unify copies cxx into cpp) is present in the state but
    absent from the raw attr."""
    from compiletools.build_apply import get_build_state

    args = _parseargs_with_temp_config(tmp_path, "test_parseargs_stashes_flags_and_leaves_slots_raw")

    flags = get_build_state(args).flags
    assert isinstance(flags, Flags)
    assert flags.cpp, "expected the unified cpp slot to carry tokens"
    assert not hasattr(args, "flags"), "populate_args must not write args.flags"
    assert not hasattr(args, "CPPFLAGS_tokens"), "populate_args must not write *_tokens"
    # Sanity: utils import keeps lint happy and confirms the helper is reachable.
    assert utils.split_command_cached("-O2") == ["-O2"]


def test_compiler_identity_distinguishes_two_real_binaries(tmp_path):
    """End-to-end: compiler_identity() must produce distinct strings for
    two distinct binaries on disk. The MacroState-level test exercises
    this only with hand-constructed identity strings; this one drives the
    actual helper against two real files so a regression in
    realpath/size/mtime composition would fail here.
    """
    a = tmp_path / "compiler_a"
    b = tmp_path / "compiler_b"
    # Pick any executable on PATH as the source; sys.executable is always
    # available. Copy it twice so the two paths differ but both are real
    # executables that pass shutil.which-style resolution.
    src = sys.executable
    shutil.copy2(src, a)
    shutil.copy2(src, b)
    os.chmod(a, 0o755)
    os.chmod(b, 0o755)
    # Force distinct mtimes so the identity strings differ even on
    # filesystems with coarse mtime granularity.
    os.utime(a, (1_700_000_000, 1_700_000_000))
    os.utime(b, (1_700_000_001, 1_700_000_001))

    id_a = compiletools.apptools.compiler_identity(str(a))
    id_b = compiletools.apptools.compiler_identity(str(b))
    assert id_a != ""
    assert id_b != ""
    assert id_a != id_b, f"distinct binaries must yield distinct identity: {id_a!r} == {id_b!r}"
