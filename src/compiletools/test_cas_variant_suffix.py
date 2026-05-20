"""Spec / regression tests for variant-suffix auto-append on
``cas-*dir`` paths.

Contract: ``args.cas_objdir``, ``args.cas_pchdir``, ``args.cas_pcmdir``,
and ``args.cas_exedir`` always end in ``/<args.variant>``. A user value
that already ends in ``/<args.variant>`` is left alone (idempotent).
This frees the user from having to bake the variant token into every
``cas-*dir`` entry they keep across machines.

Tests always pass ``--variant=<pin>`` so the assertions are
deterministic regardless of the host's ct.conf default.
"""

from __future__ import annotations

import argparse
import os

import pytest

import compiletools.apptools as apptools
import compiletools.configutils as configutils
import compiletools.testhelper as uth
from compiletools.apptools import add_output_directory_arguments
from compiletools.build_context import BuildContext

PINNED_VARIANT = "test.suffix.variant"


@pytest.fixture(autouse=True)
def _clear_apptools_cache():  # pyright: ignore[reportUnusedFunction]
    apptools.clear_cache()
    uth.delete_existing_parsers()
    apptools.resetcallbacks()
    yield
    apptools.clear_cache()
    uth.delete_existing_parsers()
    apptools.resetcallbacks()


def _parse_with_variant(conf_path: str, third_cwd, monkeypatch, variant_token: str) -> argparse.Namespace:
    """Parse a single --config=<conf_path> with an explicit pinned variant.

    The pinned variant insulates the test from whatever ``variant = ...``
    the host's ct.conf happens to set."""
    monkeypatch.chdir(str(third_cwd))
    monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
    argv = ["--config", conf_path, "--no-git-root", f"--variant={variant_token}"]
    with uth.ParserContext():
        cap = apptools.create_parser("cas-variant-suffix-test", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        variant = configutils.extract_variant(argv=argv)
        add_output_directory_arguments(cap, variant)
        return apptools.parseargs(cap, argv, context=BuildContext())


@pytest.mark.parametrize(
    "key,attr",
    [
        ("cas-objdir", "cas_objdir"),
        ("cas-pchdir", "cas_pchdir"),
        ("cas-pcmdir", "cas_pcmdir"),
        ("cas-exedir", "cas_exedir"),
    ],
)
def test_user_supplied_cas_dir_gets_variant_appended(tmp_path, monkeypatch, key, attr):
    """When the user supplies a bare ``cas-*dir`` path, the resolved
    value ends in ``/<variant>`` so the four CAS layers stay separated
    per variant without the user having to bake the token in by hand."""
    pool = tmp_path / "shared-pool"
    pool.mkdir()
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(f"{key} = {pool}\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_with_variant(str(conf), other_cwd, monkeypatch, PINNED_VARIANT)
    expected = os.path.join(str(pool), PINNED_VARIANT)
    assert getattr(args, attr) == expected, getattr(args, attr)


@pytest.mark.parametrize(
    "key,attr",
    [
        ("cas-objdir", "cas_objdir"),
        ("cas-pchdir", "cas_pchdir"),
        ("cas-pcmdir", "cas_pcmdir"),
        ("cas-exedir", "cas_exedir"),
    ],
)
def test_idempotent_when_user_path_already_ends_in_variant(tmp_path, monkeypatch, key, attr):
    """If the user-supplied path already ends in ``/<variant>``, do
    NOT append a second copy. Lets a user who already had a
    variant-suffixed path in their conf migrate to the auto-append
    contract with no edit needed."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    user_path = tmp_path / "shared-pool" / PINNED_VARIANT
    user_path.mkdir(parents=True)
    conf.write_text(f"{key} = {user_path}\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_with_variant(str(conf), other_cwd, monkeypatch, PINNED_VARIANT)
    assert getattr(args, attr) == str(user_path), getattr(args, attr)


def test_trailing_slash_does_not_double_slash(tmp_path, monkeypatch):
    """A user value with a trailing ``/`` must produce a clean single-
    slash result. Guards against the naive ``value + '/' + variant``
    implementation."""
    pool = tmp_path / "shared-pool"
    pool.mkdir()
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(f"cas-objdir = {pool}/\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_with_variant(str(conf), other_cwd, monkeypatch, PINNED_VARIANT)
    expected = os.path.join(str(pool), PINNED_VARIANT)
    assert args.cas_objdir == expected, args.cas_objdir
    assert "//" not in args.cas_objdir, args.cas_objdir


@pytest.mark.parametrize("attr", ["cas_objdir", "cas_pchdir", "cas_pcmdir", "cas_exedir"])
def test_default_path_is_not_double_variant_suffixed(tmp_path, monkeypatch, attr):
    """Regression: when no user-supplied ``cas-*dir`` value is given,
    the helper must not double-suffix the default. Defaults already
    incorporate the variant (either as ``cas-objdir/<variant>`` when
    gitroot is detected, or as ``bin/<variant>/obj`` when not), so the
    auto-append step must be a no-op for them."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("# nothing here\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_with_variant(str(conf), other_cwd, monkeypatch, PINNED_VARIANT)
    value = getattr(args, attr)
    assert not value.endswith(os.sep.join([PINNED_VARIANT, PINNED_VARIANT])), value


def test_partial_variant_match_does_not_suppress_append(tmp_path, monkeypatch):
    """A user value whose final path segment merely *contains* the
    variant token but is not equal to it must still get the variant
    appended. Guards against a naive ``endswith(variant)`` check that
    would skip ``/pool/test.suffix.variant_old`` (a sibling cache
    directory) even though it's a distinct path."""
    pool = tmp_path / "shared-pool" / (PINNED_VARIANT + "_old")
    pool.mkdir(parents=True)
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(f"cas-objdir = {pool}\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_with_variant(str(conf), other_cwd, monkeypatch, PINNED_VARIANT)
    expected = os.path.join(str(pool), PINNED_VARIANT)
    assert args.cas_objdir == expected, args.cas_objdir
