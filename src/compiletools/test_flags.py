"""Tests for compiletools.flags.Flags dataclass (TOKEN-5)."""

from __future__ import annotations

import sys
import types

import configargparse

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
    assert flags.cpp == ["-O2", "-Wall"]


def test_flags_from_args_falls_back_to_string_split():
    args = _make_args(CPPFLAGS="-O2 -Wall", CFLAGS="", CXXFLAGS="", LDFLAGS="", CXX="")
    flags = Flags.from_args(args)
    assert flags.cpp == ["-O2", "-Wall"]


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


def test_flags_hash_relevant_strips_d_and_diagnostic():
    flags = Flags(cxx=["-O2", "-DFOO", "-Wall", "-Werror"])
    assert flags.hash_relevant("cxx") == ["-O2", "-Werror"]


def test_flags_existing_include_paths_attached():
    flags = Flags(cpp=["-I/a", "-O2"])
    assert flags.existing_include_paths("cpp") == {"/a"}


def test_flags_existing_include_paths_detached():
    flags = Flags(cpp=["-I", "/a", "-O2"])
    assert flags.existing_include_paths("cpp") == {"/a"}


def test_flags_existing_include_paths_ignores_isystem():
    flags = Flags(cpp=["-isystem", "/a"])
    assert flags.existing_include_paths("cpp") == set()


def test_flags_append_include_adds_when_missing():
    flags = Flags(cpp=["-O2"])
    flags.append_include("/new", slots=("cpp",))
    assert flags.cpp == ["-O2", "-I", "/new"]


def test_flags_append_include_skips_when_present_attached():
    flags = Flags(cpp=["-I/existing"])
    flags.append_include("/existing", slots=("cpp",))
    assert flags.cpp == ["-I/existing"]


def test_flags_append_include_skips_when_present_detached():
    flags = Flags(cpp=["-I", "/existing"])
    flags.append_include("/existing", slots=("cpp",))
    assert flags.cpp == ["-I", "/existing"]


def test_flags_append_include_default_slots_all_three():
    flags = Flags()
    flags.append_include("/x")
    assert flags.cpp == ["-I", "/x"]
    assert flags.c == ["-I", "/x"]
    assert flags.cxx == ["-I", "/x"]
    assert flags.ld == []


def test_args_has_flags_attribute_after_parseargs(tmp_path):
    """parseargs must populate args.flags as a Flags instance whose
    cpp slot mirrors args.CPPFLAGS_tokens."""
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
    assert args.flags.cpp == args.CPPFLAGS_tokens
    assert args.flags.c == args.CFLAGS_tokens
    assert args.flags.cxx == args.CXXFLAGS_tokens
    assert args.flags.ld == args.LDFLAGS_tokens
    # Sanity: utils import keeps lint happy and confirms the helper is reachable.
    assert utils.split_command_cached("-O2") == ["-O2"]
