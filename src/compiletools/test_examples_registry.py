"""Tests for compiletools.examples_registry."""

from __future__ import annotations

import os

import pytest

from compiletools import examples_registry as er


def test_e2e_registry_size():
    """Spec pins 34 entries in examples-end-to-end."""
    assert len(er.EXAMPLES_E2E) == 34


def test_features_registry_size():
    """Spec pins 23 entries in examples-features."""
    assert len(er.EXAMPLES_FEATURES) == 23


def test_registries_are_disjoint():
    """No example may live in both buckets."""
    assert er.EXAMPLES_E2E.isdisjoint(er.EXAMPLES_FEATURES)


def test_e2e_known_member_resolves():
    p = er.example_path("calculator")
    assert p.endswith(os.path.join("examples-end-to-end", "calculator"))
    assert os.path.isabs(p)


def test_features_known_member_resolves():
    p = er.example_path("cycle")
    assert p.endswith(os.path.join("examples-features", "cycle"))
    assert os.path.isabs(p)


def test_unknown_name_raises_keyerror():
    with pytest.raises(KeyError, match="examples_registry"):
        er.example_path("definitely_not_a_real_example")


def test_example_file_joins_relative_path():
    """example_file('simple/helloworld.cpp') resolves through the bucket."""
    p = er.example_file("simple/helloworld_cpp.cpp")
    assert p.endswith(os.path.join("examples-end-to-end", "simple", "helloworld_cpp.cpp"))


def test_example_file_with_bare_name():
    """example_file('pkgs') (no slash) resolves to the bucket dir itself."""
    p = er.example_file("pkgs")
    assert p.endswith(os.path.join("examples-features", "pkgs"))


def test_e2e_dir_returns_examples_end_to_end():
    assert er.e2e_dir().endswith("examples-end-to-end")
    assert os.path.isabs(er.e2e_dir())


def test_features_dir_returns_examples_features():
    assert er.features_dir().endswith("examples-features")
    assert os.path.isabs(er.features_dir())
