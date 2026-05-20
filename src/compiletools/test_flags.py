"""Tests for compiletools.flags.Flags dataclass (TOKEN-5)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import types

import configargparse
import pytest

import compiletools.apptools
import compiletools.configutils
import compiletools.testhelper as uth
import compiletools.utils as utils
from compiletools.build_context import BuildContext
from compiletools.flags import Flags


def _make_args(**kwargs) -> types.SimpleNamespace:
    """Build a bare args namespace with the given attributes."""
    return types.SimpleNamespace(**kwargs)


def test_flags_from_args_reads_tokens():
    args = _make_args(
        CPPFLAGS_tokens=["-O2", "-Wall"],
        CFLAGS_tokens=[],
        CXXFLAGS_tokens=[],
        LDFLAGS_tokens=[],
        CXX="",
    )
    flags = Flags.from_args(args)
    assert flags.cpp == ("-O2", "-Wall")


def test_flags_from_args_populates_compiler_identity():
    args = _make_args(
        CPPFLAGS_tokens=[],
        CFLAGS_tokens=[],
        CXXFLAGS_tokens=[],
        LDFLAGS_tokens=[],
        CXX=sys.executable,
    )
    flags = Flags.from_args(args)
    assert flags.compiler_identity != ""


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


def test_args_has_flags_attribute_after_parseargs(tmp_path):
    """parseargs must populate args.flags as a Flags instance whose
    cpp slot mirrors args.CPPFLAGS_tokens (tuple-vs-list aware)."""
    uth.delete_existing_parsers()
    compiletools.apptools.resetcallbacks()
    try:
        temp_config_name = uth.create_temp_config(str(tmp_path))
        argv = ["--config=" + temp_config_name]
        config_files = compiletools.configutils.config_files_from_variant(argv=argv, exedir=uth.cakedir())

        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="test_args_has_flags_attribute_after_parseargs",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.apptools.add_common_arguments(cap)
        compiletools.apptools.add_link_arguments(cap)
        args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())
    finally:
        uth.delete_existing_parsers()
        compiletools.apptools.resetcallbacks()

    assert hasattr(args, "flags"), "parseargs must populate args.flags"
    assert isinstance(args.flags, Flags)
    assert args.flags.cpp == tuple(args.CPPFLAGS_tokens)
    assert args.flags.c == tuple(args.CFLAGS_tokens)
    assert args.flags.cxx == tuple(args.CXXFLAGS_tokens)
    assert args.flags.ld == tuple(args.LDFLAGS_tokens)
    # Sanity: utils import keeps lint happy and confirms the helper is reachable.
    assert utils.split_command_cached("-O2") == ["-O2"]


def test_check_flag_string_drift_clean(tmp_path):
    """check_flag_string_drift is a no-op when args.{*FLAGS} match the
    snapshot taken at parseargs end."""
    uth.delete_existing_parsers()
    compiletools.apptools.resetcallbacks()
    try:
        temp_config_name = uth.create_temp_config(str(tmp_path))
        argv = ["--config=" + temp_config_name]
        config_files = compiletools.configutils.config_files_from_variant(argv=argv, exedir=uth.cakedir())
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="test_check_flag_string_drift_clean",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.apptools.add_common_arguments(cap)
        compiletools.apptools.add_link_arguments(cap)
        args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())
    finally:
        uth.delete_existing_parsers()
        compiletools.apptools.resetcallbacks()
    # No mutation -> no raise.
    compiletools.apptools.check_flag_string_drift(args)


def test_check_flag_string_drift_detects_mutation():
    """check_flag_string_drift raises RuntimeError if any *FLAGS string
    has been mutated after the snapshot was recorded."""
    args = types.SimpleNamespace(
        CPPFLAGS="-O2",
        CFLAGS="-O2",
        CXXFLAGS="-O2",
        LDFLAGS="",
        _flag_string_snapshot=(("CPPFLAGS", "-O2"), ("CFLAGS", "-O2"), ("CXXFLAGS", "-O2"), ("LDFLAGS", "")),
    )
    compiletools.apptools.check_flag_string_drift(args)  # baseline ok
    args.CXXFLAGS = "-O2 -DLATE_MUTATION"
    with pytest.raises(RuntimeError, match="CXXFLAGS mutated after parseargs end"):
        compiletools.apptools.check_flag_string_drift(args)


def test_check_flag_string_drift_no_snapshot_is_noop():
    """If args has no _flag_string_snapshot (e.g. args constructed
    without going through parseargs), the check is a no-op."""
    args = types.SimpleNamespace(CPPFLAGS="-O2")
    # Should not raise even though there's no snapshot.
    compiletools.apptools.check_flag_string_drift(args)


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
