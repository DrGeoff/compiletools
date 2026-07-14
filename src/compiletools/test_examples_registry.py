"""Tests for compiletools.examples_registry."""

from __future__ import annotations

import os

import pytest

from compiletools import examples_registry as er


@pytest.mark.parametrize(
    ("registry", "expected_size"),
    [
        pytest.param(er.EXAMPLES_E2E, 43, id="examples-end-to-end"),
        pytest.param(er.EXAMPLES_FEATURES, 27, id="examples-features"),
    ],
)
def test_registry_size(registry, expected_size):
    """Spec pins the expected number of entries in each examples bucket."""
    assert len(registry) == expected_size


def test_registries_are_disjoint():
    """No example may live in both buckets."""
    assert er.EXAMPLES_E2E.isdisjoint(er.EXAMPLES_FEATURES)


@pytest.mark.parametrize(
    ("name", "bucket"),
    [
        pytest.param("calculator", "examples-end-to-end", id="examples-end-to-end"),
        pytest.param("cycle", "examples-features", id="examples-features"),
    ],
)
def test_known_member_resolves(name, bucket):
    p = er.example_path(name)
    assert p.endswith(os.path.join(bucket, name))
    assert os.path.isabs(p)


def test_unknown_name_raises_keyerror():
    with pytest.raises(KeyError, match="examples_registry"):
        er.example_path("definitely_not_a_real_example")


@pytest.mark.parametrize(
    ("relpath", "expected_suffix"),
    [
        pytest.param(
            "simple/helloworld_cpp.cpp",
            os.path.join("examples-end-to-end", "simple", "helloworld_cpp.cpp"),
            id="relative-path",
        ),
        pytest.param("pkgs", os.path.join("examples-features", "pkgs"), id="bare-name"),
    ],
)
def test_example_file_resolves(relpath, expected_suffix):
    p = er.example_file(relpath)
    assert p.endswith(expected_suffix)


@pytest.mark.parametrize(
    ("directory", "suffix"),
    [
        pytest.param(er.e2e_dir, "examples-end-to-end", id="examples-end-to-end"),
        pytest.param(er.features_dir, "examples-features", id="examples-features"),
    ],
)
def test_dir_returns_examples_bucket(directory, suffix):
    path = directory()
    assert path.endswith(suffix)
    assert os.path.isabs(path)


def test_drift_guard_filesystem_matches_registries():
    """Every directory under examples-features/ and examples-end-to-end/
    appears in exactly one of the two registries; conversely, every
    registry entry corresponds to a real directory."""
    e2e_on_disk = {p for p in os.listdir(er.e2e_dir()) if os.path.isdir(os.path.join(er.e2e_dir(), p))}
    features_on_disk = {p for p in os.listdir(er.features_dir()) if os.path.isdir(os.path.join(er.features_dir(), p))}

    # Disk → registry: every directory must be registered.
    missing_e2e = e2e_on_disk - er.EXAMPLES_E2E
    missing_features = features_on_disk - er.EXAMPLES_FEATURES
    assert not missing_e2e, f"directories in examples-end-to-end/ not in EXAMPLES_E2E: {sorted(missing_e2e)}"
    assert not missing_features, (
        f"directories in examples-features/ not in EXAMPLES_FEATURES: {sorted(missing_features)}"
    )

    # Registry → disk: every registered name must exist on disk in
    # the right bucket.
    stale_e2e = er.EXAMPLES_E2E - e2e_on_disk
    stale_features = er.EXAMPLES_FEATURES - features_on_disk
    assert not stale_e2e, f"EXAMPLES_E2E entries with no matching directory: {sorted(stale_e2e)}"
    assert not stale_features, f"EXAMPLES_FEATURES entries with no matching directory: {sorted(stale_features)}"
