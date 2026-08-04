"""compute_build_state end-to-end over literal inputs."""

import shlex

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import SetEnv, compute_build_state

ALL_SLOTS = frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"})
ORDER = ("gcc", "clang", "debug", "release")


def _full_inputs(**kw):
    kw.setdefault("registered_slots", ALL_SLOTS)
    kw.setdefault("variant_raw", "gcc.debug")
    kw.setdefault("canonical_order", ORDER)
    return BuildInputs(**kw)


def test_pkg_config_block_lands_once_and_everywhere_it_should():
    inputs = _full_inputs(
        cxxflags=("-O2",),
        gitroot="/repo",
        pkg_config_results=(("zlib", PkgConfigResult(cflags=("-I/z",), libs=("-lz",))),),
    )
    state = compute_build_state(inputs)
    assert state.tokens.cxx.count("-I/z") == 1
    assert state.tokens.ld.count("-lz") == 1
    assert "-ffile-prefix-map=/repo=." in state.tokens.cpp  # unified after inject
    assert state.cxxflags == shlex.join(state.tokens.cxx)
    assert state.names.variant == "gcc.debug"
    assert state.names.bindir == "bin/gcc.debug"


def test_recompute_from_equal_inputs_is_equal():
    inputs = _full_inputs(cxxflags=("-O2",), gitroot="/repo")
    assert compute_build_state(inputs) == compute_build_state(inputs)


def test_pkg_config_path_becomes_setenv_effect():
    state = compute_build_state(_full_inputs(pkg_config_path="/pc:/pc2"))
    assert SetEnv(name="PKG_CONFIG_PATH", value="/pc:/pc2") in state.effects


def test_flags_carries_compiler_identity():
    state = compute_build_state(_full_inputs(compiler_identity="id|1|2"))
    assert state.flags.compiler_identity == "id|1|2"
