"""Frozen value types for the functional build-state core."""

import dataclasses

import pytest

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import TokenState


def test_token_state_is_frozen_and_defaults_empty():
    ts = TokenState()
    assert ts.cpp == () and ts.c == () and ts.cxx == () and ts.ld == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ts.cpp = ("-O2",)  # type: ignore[misc]


def test_build_inputs_is_frozen():
    inputs = BuildInputs(registered_slots=frozenset({"CPPFLAGS"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.verbose = 3  # type: ignore[misc]


def test_build_inputs_replace_produces_new_value():
    inputs = BuildInputs(registered_slots=frozenset({"CPPFLAGS"}), include_paths=("/a",))
    widened = dataclasses.replace(inputs, include_paths=("/a", "/b"))
    assert inputs.include_paths == ("/a",)
    assert widened.include_paths == ("/a", "/b")


def test_pkg_config_result_equality_is_structural():
    assert PkgConfigResult(cflags=("-I/x",), libs=("-lz",)) == PkgConfigResult(cflags=("-I/x",), libs=("-lz",))
