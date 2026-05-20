"""Spec / regression tests for $VAR / ${VAR} / ~ expansion in conf-file
values, plus the $$ escape and provenance pre-expansion-literal
recording.

Pipeline (each value, at conf-parse time):
  1. ${CONF_DIR} substitution (existing _expand_conf_dir).
  2. $$ -> NUL sentinel.
  3. $VAR / ${VAR} via os.path.expandvars.
  4. ~ / ~user via os.path.expanduser.
  5. NUL sentinel -> literal $.

See docs/superpowers/specs/2026-05-20-conf-env-expansion-design.md for
the full contract. Mirrors the shape of test_conf_dir_placeholder.py.
"""

from __future__ import annotations

import argparse

import pytest

import compiletools.apptools as apptools
import compiletools.configutils as configutils
import compiletools.testhelper as uth
from compiletools.apptools import add_output_directory_arguments
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _clear_apptools_cache():  # pyright: ignore[reportUnusedFunction]
    """Reset configargparse parser state and apptools caches around every
    test so PKG_CONFIG_PATH mutations from neighbouring tests can't leak
    in (or out)."""
    apptools.clear_cache()
    uth.delete_existing_parsers()
    apptools.resetcallbacks()
    yield
    apptools.clear_cache()
    uth.delete_existing_parsers()
    apptools.resetcallbacks()


def _parse_conf(conf_path: str, third_cwd, monkeypatch) -> argparse.Namespace:
    """Parse a single --config=<conf_path> from third_cwd.

    --no-git-root short-circuits find_git_root so the test is
    insensitive to whether the temp dir lives inside a git checkout.

    add_output_directory_arguments registers --cas-objdir (and siblings)
    so the tests can assert on args.cas_objdir directly."""
    monkeypatch.chdir(str(third_cwd))
    monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
    argv = ["--config", conf_path, "--no-git-root"]
    with uth.ParserContext():
        cap = apptools.create_parser("conf-env-expansion-test", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        variant = configutils.extract_variant(argv=argv)
        add_output_directory_arguments(cap, variant)
        return apptools.parseargs(cap, argv, context=BuildContext())


def test_dollar_HOME_expands_in_conf_value(tmp_path, monkeypatch):
    """`cas-objdir = $HOME/x` must resolve to os.environ["HOME"] + "/x"
    on args.cas_objdir."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = $HOME/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    assert args.cas_objdir == "/test/home/x", args.cas_objdir


def test_tilde_expands_in_conf_value(tmp_path, monkeypatch):
    """`cas-objdir = ~/x` must resolve via os.path.expanduser."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = ~/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    assert args.cas_objdir == "/test/home/x", args.cas_objdir
