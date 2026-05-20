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
import os

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


def test_dollar_escape_protects_literal_dollar(tmp_path, monkeypatch):
    """`$$HOME` in a conf value must produce literal `$HOME` in the
    output — HOME is not expanded.

    Sentinel-swap implementation: os.path.expandvars does NOT honor $$
    natively, so the helper must protect $$ before calling expandvars
    and restore after."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("append-CXXFLAGS = -DDOLLAR=$$HOME\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    values = args.append_cxxflags if isinstance(args.append_cxxflags, list) else [args.append_cxxflags]
    flat = " ".join(values)
    assert "-DDOLLAR=$HOME" in flat, flat
    assert "/test/home" not in flat, flat


def test_dollar_escape_multiple_in_one_value(tmp_path, monkeypatch):
    """Multiple `$$` tokens in one value must all restore to literal `$`,
    independent of any real $VAR expansion in the same value."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("append-CXXFLAGS = -DA=$$ -DB=$$HOME -DC=$HOME\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_conf(str(conf), other_cwd, monkeypatch)
    values = args.append_cxxflags if isinstance(args.append_cxxflags, list) else [args.append_cxxflags]
    flat = " ".join(values)
    assert "-DA=$" in flat, flat
    assert "-DB=$HOME" in flat, flat
    assert "-DC=/test/home" in flat, flat


# ---------------------------------------------------------------------------
# Provenance side channel: pre-expansion literal as 4th tuple element.
# ---------------------------------------------------------------------------

PROVENANCE_GETTER_NAME = "get_conf_file_provenance"


def _provenance_for(args: argparse.Namespace) -> dict:
    """Pull the provenance dict off args._parser; pytest.fail with a
    pointed message if the implementation didn't expose it. Mirrors the
    same helper in test_conf_dir_placeholder.py."""
    parser = getattr(args, "_parser", None)
    if parser is None:
        pytest.fail("args._parser is None")
    getter = getattr(parser, PROVENANCE_GETTER_NAME, None)
    if getter is None:
        pytest.fail(f"Parser missing {PROVENANCE_GETTER_NAME}()")
    return getter()


def test_provenance_tuple_includes_pre_expansion_literal(tmp_path, monkeypatch):
    """Each provenance entry is a 4-tuple
    (expanded_value, source_file, lineno, pre_expansion_literal). When
    expansion happened, literal != expanded; when nothing was expanded,
    literal == expanded. This lets -vv diagnostics show 'why did $HOME
    resolve to /tmp on the CI host'."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = $HOME/x\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    args = _parse_conf(str(conf), other_cwd, monkeypatch)

    prov = _provenance_for(args)
    entries = prov.get("cas-objdir") or []
    assert entries, f"no provenance for cas-objdir; keys: {sorted(prov.keys())!r}"
    entry = entries[-1]
    assert len(entry) == 4, f"expected 4-tuple, got {len(entry)}-tuple: {entry!r}"
    expanded, source_file, lineno, literal = entry
    assert expanded == "/test/home/x", expanded
    assert literal == "$HOME/x", literal
    assert os.path.realpath(source_file) == os.path.realpath(str(conf))
    assert isinstance(lineno, int) and lineno >= 1


def test_provenance_literal_equals_expanded_when_no_expansion(tmp_path, monkeypatch):
    """When the conf value has no $ or ~ to expand, the 4th tuple
    element equals the 1st — same string. Lets a consumer cheaply test
    'did expansion happen here' via expanded != literal."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("cas-objdir = /absolute/no/expansion\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    args = _parse_conf(str(conf), other_cwd, monkeypatch)

    prov = _provenance_for(args)
    entries = prov.get("cas-objdir") or []
    assert entries, f"no provenance for cas-objdir; keys: {sorted(prov.keys())!r}"
    entry = entries[-1]
    assert len(entry) == 4, entry
    expanded, _source, _lineno, literal = entry
    assert expanded == "/absolute/no/expansion", expanded
    assert literal == expanded, (literal, expanded)


def test_provenance_literal_for_list_value(tmp_path, monkeypatch):
    """For an accumulated list (append-CXXFLAGS), each element has its
    own 4-tuple with its own pre-expansion literal."""
    monkeypatch.setenv("HOME", "/test/home")
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(
        "append-CXXFLAGS = -I$HOME/a\n"
        "append-CXXFLAGS = -I/bare/b\n"
    )

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    args = _parse_conf(str(conf), other_cwd, monkeypatch)

    prov = _provenance_for(args)
    entries = prov.get("append-CXXFLAGS") or []
    assert len(entries) >= 2, entries
    expanded_to_literal = {str(e[0]): str(e[3]) for e in entries if len(e) == 4}
    assert expanded_to_literal.get("-I/test/home/a") == "-I$HOME/a", expanded_to_literal
    assert expanded_to_literal.get("-I/bare/b") == "-I/bare/b", expanded_to_literal
