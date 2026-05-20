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


def test_brace_form_HOME_expands(tmp_path, monkeypatch):
    """${HOME} brace form must expand the same as $HOME."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = ${HOME}/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    assert args.cas_objdir == "/test/home/x", args.cas_objdir


def test_conf_dir_expanded_before_env(tmp_path, monkeypatch):
    """${CONF_DIR} must expand before $HOME so a value like
    `${CONF_DIR}/$HOME/x` produces /<confdir>/<expanded-home>/x.

    Regression guard on pipeline step order — flipping the steps would
    leave ${CONF_DIR} as a literal that os.path.expandvars then sees as
    ${CONF}_DIR/<something> and either expands or leaves alone in
    surprising ways.

    HOME is set to a relative-style token (no leading /) so that
    ${CONF_DIR}/$HOME/inc produces a clean single-slash path.  The key
    assertion is that ${CONF_DIR} was substituted (giving the conf-file
    directory as a prefix), not that any particular slash-normalisation
    rule is applied."""
    monkeypatch.setenv("HOME", "test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("append-CXXFLAGS = -I${CONF_DIR}/$HOME/inc\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    flat = " ".join(args.append_cxxflags) if isinstance(args.append_cxxflags, list) else str(args.append_cxxflags)
    expected = f"-I{conf_dir}/test/home/inc"
    assert expected in flat, f"expected {expected!r} in {flat!r}"


def test_undefined_env_var_stays_literal(tmp_path, monkeypatch):
    """A reference to a variable not in os.environ stays literal —
    matches os.path.expandvars semantics and preserves today's behaviour
    for genuinely undefined vars."""
    monkeypatch.delenv("DEFINITELY_UNSET_xyz_42", raising=False)
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = $DEFINITELY_UNSET_xyz_42/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    assert args.cas_objdir == "$DEFINITELY_UNSET_xyz_42/x", args.cas_objdir


def test_list_value_expands_each_element(tmp_path, monkeypatch):
    """Accumulated list values (append-CXXFLAGS) expand each element
    independently. Locks in the recursion in _expand_env_and_user
    (mirrors how _expand_conf_dir handles lists)."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(
        "append-CXXFLAGS = -I$HOME/a\n"
        "append-CXXFLAGS = -I$HOME/b\n"
    )

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    values = args.append_cxxflags if isinstance(args.append_cxxflags, list) else [args.append_cxxflags]
    flat = " ".join(values)
    assert "-I/test/home/a" in flat, flat
    assert "-I/test/home/b" in flat, flat
    assert "$HOME" not in flat, flat


def test_bare_relative_not_touched_by_env_expansion(tmp_path, monkeypatch):
    """A bare relative path that contains no $ or ~ must pass through
    verbatim — the env-expansion helper does not auto-anchor against
    conf-dir or cwd. Symmetric to
    test_bare_relative_paths_are_not_auto_anchored in
    test_conf_dir_placeholder.py."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = relative/subdir/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    assert args.cas_objdir == "relative/subdir/x", args.cas_objdir
