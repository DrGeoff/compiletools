"""Spec / regression tests for the ${CONF_DIR} placeholder + conf-file
provenance side channel.

${CONF_DIR} expands at conf-parse time to the absolute directory of the
conf file the value originated from. The provenance side channel records
(value, source_file, lineno) per parsed entry, exposed via
parser.get_conf_file_provenance().

See src/compiletools/CLAUDE.md (architecture) and README.ct-config.rst
(user-facing) for the full background. Fixture: examples-features/
conf_dir_relative_pkgconfig/.
"""

from __future__ import annotations

import argparse
import os

import pytest

import compiletools.apptools as apptools
import compiletools.examples_registry as er
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _clear_apptools_cache():
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


def _conf_dir() -> str:
    """Absolute path to the conf-d directory of the shared example."""
    return os.path.join(er.example_path("conf_dir_relative_pkgconfig"), "ct.conf.d")


def _parse_with_flavor_conf(flavor: str, third_cwd: str, monkeypatch) -> argparse.Namespace:
    """Parse the example's ``flavor-<X>.conf`` from ``third_cwd``.

    ``third_cwd`` is the analog of the cas-pchdir/<variant>/<hash>/
    directory that ct-cake-spawned subprocesses run from — neither the
    conf file's directory nor any ancestor of it. The whole point of
    ``${CONF_DIR}`` is to make the resulting PKG_CONFIG_PATH entries
    independent of this cwd.

    ``--no-git-root`` short-circuits ``find_git_root`` so the test is
    insensitive to whether the temp dir happens to live inside a git
    checkout.
    """
    monkeypatch.chdir(third_cwd)
    monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

    flavor_conf = os.path.join(_conf_dir(), f"flavor-{flavor}.conf")
    argv = ["--config", flavor_conf, "--no-git-root"]

    with uth.ParserContext():
        cap = apptools.create_parser("conf-dir-placeholder-test", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        return apptools.parseargs(cap, argv, context=BuildContext())


# ---------------------------------------------------------------------------
# Option 1: ${CONF_DIR} expansion at parse time.
# ---------------------------------------------------------------------------


def test_conf_dir_placeholder_expands_to_conf_file_directory(tmp_path, monkeypatch):
    """``prepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig-a`` in
    ``<example>/ct.conf.d/flavor-a.conf`` must produce
    ``<example>/ct.conf.d/pkgconfig-a`` on ``args.prepend_pkg_config_path``,
    regardless of where the parsing process happens to live.

    The load-bearing assertion. Fails today (the literal ``${CONF_DIR}``
    passes through unexpanded), passes after the fix.
    """
    args = _parse_with_flavor_conf("a", str(tmp_path), monkeypatch)

    expected_anchored = os.path.join(_conf_dir(), "pkgconfig-a")
    assert os.path.isdir(expected_anchored), (
        f"fixture invariant broken: {expected_anchored} missing on disk"
    )

    entries = list(args.prepend_pkg_config_path)
    assert any(os.path.realpath(e) == os.path.realpath(expected_anchored) for e in entries), (
        f"${{CONF_DIR}} in {_conf_dir()}/flavor-a.conf must expand to "
        f"{_conf_dir()!r} so the prepend-PKG-CONFIG-PATH entry surfaces as "
        f"{expected_anchored!r}, but got entries: {entries!r}. The fix "
        f"injection point is _AccumulatingConfigFileParser.parse() "
        f"(apptools.py:2940)."
    )


def test_unexpanded_conf_dir_placeholder_does_not_survive(tmp_path, monkeypatch):
    """No entry on ``args.prepend_pkg_config_path`` may contain the
    literal ``${CONF_DIR}`` token. Companion to the expansion test:
    splitting it makes the diff-on-failure self-explaining (unexpanded
    placeholder vs missing entry vs wrong-anchored entry are different
    failure modes that point at different fix bugs)."""
    args = _parse_with_flavor_conf("a", str(tmp_path), monkeypatch)

    entries = list(args.prepend_pkg_config_path)
    assert not any("${CONF_DIR}" in e for e in entries), (
        f"Literal ${{CONF_DIR}} survived onto args.prepend_pkg_config_path: "
        f"{entries!r}. Expansion in _AccumulatingConfigFileParser.parse() "
        f"is missing or wrong."
    )


def test_conf_dir_placeholder_expansion_is_independent_of_consumer_cwd(
    tmp_path, monkeypatch
):
    """The whole point: the resolved entries must be identical no matter
    which cwd the parser was invoked from. Parse the same conf from two
    different cwds and assert the results match exactly.

    Guards against a regression into a per-cwd-expansion scheme (e.g.,
    expanding ``${CONF_DIR}`` to a cwd-relative path instead of an
    absolute one)."""
    third_a = tmp_path / "third-a"
    third_b = tmp_path / "third-b"
    third_a.mkdir()
    third_b.mkdir()

    args_from_a = _parse_with_flavor_conf("a", str(third_a), monkeypatch)
    entries_from_a = sorted(args_from_a.prepend_pkg_config_path)

    args_from_b = _parse_with_flavor_conf("a", str(third_b), monkeypatch)
    entries_from_b = sorted(args_from_b.prepend_pkg_config_path)

    assert entries_from_a == entries_from_b, (
        f"args.prepend_pkg_config_path differed across consumer cwds; the "
        f"${{CONF_DIR}} expansion must be cwd-independent. "
        f"From {third_a}: {entries_from_a!r}; from {third_b}: {entries_from_b!r}"
    )


# ---------------------------------------------------------------------------
# Option 1: end-to-end through _setup_pkg_config_overrides.
# ---------------------------------------------------------------------------


def test_pkg_config_path_env_contains_anchored_absolute_after_parseargs(
    tmp_path, monkeypatch
):
    """Once ``parseargs`` has run ``_setup_pkg_config_overrides``,
    ``os.environ['PKG_CONFIG_PATH']`` is the source of truth for every
    pkg-config subprocess in the build. With the fix in place, the
    ``${CONF_DIR}``-expanded absolute path must be present so pkg-config
    finds ``flavored.pc`` from any cwd (including the
    cas-pchdir/<variant>/<hash>/ cwd of spawned compiles)."""
    _parse_with_flavor_conf("b", str(tmp_path), monkeypatch)

    raw = os.environ.get("PKG_CONFIG_PATH", "")
    assert raw, "parseargs should have populated PKG_CONFIG_PATH"

    entries = raw.split(os.pathsep)
    expected_anchored = os.path.join(_conf_dir(), "pkgconfig-b")

    assert not any("${CONF_DIR}" in e for e in entries), (
        f"Literal ${{CONF_DIR}} survived into PKG_CONFIG_PATH: {raw!r}"
    )
    assert any(os.path.realpath(e) == os.path.realpath(expected_anchored) for e in entries), (
        f"PKG_CONFIG_PATH should contain the anchored absolute path "
        f"{expected_anchored!r}. Got entries: {entries!r}"
    )


def test_each_flavor_resolves_to_its_own_pkgconfig_dir(tmp_path, monkeypatch):
    """Symmetry: --config=flavor-a.conf must put pkgconfig-a on
    PKG_CONFIG_PATH; --config=flavor-b.conf must put pkgconfig-b on it.
    Guards against any future ``${CONF_DIR}`` implementation that
    accidentally resolves all conf files against the same shared
    directory (e.g., the highest-priority conf's dir, or the first
    one in the stream)."""
    expected = {
        "a": os.path.join(_conf_dir(), "pkgconfig-a"),
        "b": os.path.join(_conf_dir(), "pkgconfig-b"),
    }
    for flavor, expected_dir in expected.items():
        third_cwd = tmp_path / f"third-{flavor}"
        third_cwd.mkdir()
        _parse_with_flavor_conf(flavor, str(third_cwd), monkeypatch)

        raw = os.environ.get("PKG_CONFIG_PATH", "")
        entries = raw.split(os.pathsep) if raw else []
        assert any(os.path.realpath(e) == os.path.realpath(expected_dir) for e in entries), (
            f"flavor-{flavor}.conf should put {expected_dir!r} on "
            f"PKG_CONFIG_PATH; got {entries!r}"
        )


# ---------------------------------------------------------------------------
# Option 1: behavioural invariants of the placeholder.
# ---------------------------------------------------------------------------


def _parse_inline_conf(conf_path: str, monkeypatch, tmp_path) -> argparse.Namespace:
    """Parse a single bespoke conf file from a neutral third cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
    argv = ["--config", conf_path, "--no-git-root"]
    with uth.ParserContext():
        cap = apptools.create_parser("conf-dir-placeholder-inline", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        return apptools.parseargs(cap, argv, context=BuildContext())


def test_conf_dir_placeholder_works_for_non_path_keys(tmp_path, monkeypatch):
    """The placeholder is generic — not pkg-config-specific. A user must
    be able to write ``append-CXXFLAGS = -I${CONF_DIR}/include`` and
    have the include-dir resolve correctly. Locks in the generality
    that makes Option 1 (placeholder) preferable to Option 2 (auto-
    anchor `*-PATH`-suffixed keys only)."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("append-CXXFLAGS = -I${CONF_DIR}/include\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_inline_conf(str(conf), monkeypatch, other_cwd)

    expanded = f"-I{conf_dir}/include"
    flat = " ".join(args.append_cxxflags) if isinstance(args.append_cxxflags, list) else str(args.append_cxxflags)
    assert expanded in flat, (
        f"${{CONF_DIR}} in append-CXXFLAGS must expand. Expected token "
        f"{expanded!r} in append_cxxflags={args.append_cxxflags!r}"
    )
    assert "${CONF_DIR}" not in flat, (
        f"Literal ${{CONF_DIR}} survived in append_cxxflags={args.append_cxxflags!r}"
    )


def test_conf_dir_placeholder_handles_multiple_occurrences_in_one_value(
    tmp_path, monkeypatch
):
    """Multiple ``${CONF_DIR}`` tokens in the same value must all expand.
    Guards against an implementation that uses ``.replace`` with
    ``count=1`` or similar."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text(
        "append-CXXFLAGS = -I${CONF_DIR}/include -L${CONF_DIR}/lib\n"
    )

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_inline_conf(str(conf), monkeypatch, other_cwd)

    flat = " ".join(args.append_cxxflags) if isinstance(args.append_cxxflags, list) else str(args.append_cxxflags)
    assert f"-I{conf_dir}/include" in flat, flat
    assert f"-L{conf_dir}/lib" in flat, flat
    assert "${CONF_DIR}" not in flat, flat


def test_bare_relative_paths_are_not_auto_anchored(tmp_path, monkeypatch):
    """Option 1 is opt-in: bare relative paths in conf files do NOT get
    auto-anchored to the conf dir. Users opt in via ``${CONF_DIR}``. This
    test pins that principle so a future change can't quietly switch us
    onto Option 2 semantics (auto-anchor `*-PATH` keys) without an
    intentional decision.

    The bare relative survives as a bare literal — same as today and
    same as after the fix. The PKG_CONFIG_PATH entry pkg-config sees is
    cwd-relative, which is broken from a non-conf-dir cwd, but that
    matches the CLI's own behavior for ``--prepend-PKG-CONFIG-PATH=
    relative/path``."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("prepend-PKG-CONFIG-PATH = ct.conf.d/pkgconfig-c\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()

    args = _parse_inline_conf(str(conf), monkeypatch, other_cwd)
    entries = list(args.prepend_pkg_config_path)

    bare_literal = os.path.join("ct.conf.d", "pkgconfig-c")
    assert bare_literal in entries, (
        f"Bare relative paths in conf files must survive verbatim under "
        f"Option 1 semantics (no auto-anchor magic). Expected the literal "
        f"{bare_literal!r} on args.prepend_pkg_config_path, got: {entries!r}. "
        f"If this assertion fails because the entry got auto-anchored, "
        f"someone implemented Option 2 instead of Option 1 — check the "
        f"design decision before proceeding."
    )
    auto_anchored = str(conf_dir / "ct.conf.d" / "pkgconfig-c")
    assert auto_anchored not in entries, (
        f"Bare relative was auto-anchored to {auto_anchored!r}. That is "
        f"Option 2 semantics, not Option 1. Entries: {entries!r}"
    )


def test_segment_header_in_user_comment_does_not_poison_conf_dir(tmp_path, monkeypatch):
    """Segment-header recognition rejects user comments that match the
    syntactic shape but name a non-existent path.

    A user comment ``# --- /some/fictional/path/conf.conf ---`` must not
    silently swap the parser's conf-dir for subsequent ``${CONF_DIR}``
    expansions. The header is only honored when its named path exists
    on disk.

    Uses two conf files so the multi-conf concatenation path runs (the
    single-file path bypasses segment-header emission entirely)."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    first = conf_dir / "first.conf"
    first.write_text(
        "# --- /some/fictional/path/conf.conf ---\n"
        "prepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig\n"
    )
    second = conf_dir / "second.conf"
    second.write_text("# placeholder so multi-conf concatenation runs\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

    argv = ["--config", str(first), "--config", str(second), "--no-git-root"]
    with uth.ParserContext():
        cap = apptools.create_parser("segment-header-poison", argv=argv)
        apptools.add_common_arguments(cap, argv=argv)
        args = apptools.parseargs(cap, argv, context=BuildContext())

    expected = str(conf_dir / "pkgconfig")
    entries = list(args.prepend_pkg_config_path)
    assert any(os.path.realpath(e) == os.path.realpath(expected) for e in entries), (
        f"User comment poisoned conf_dir; expected {expected!r} on "
        f"prepend_pkg_config_path; got {entries!r}"
    )
    flat = " ".join(entries)
    assert "/some/fictional/path" not in flat, (
        f"User-authored # --- /path --- comment was honored as a real "
        f"segment header; got: {entries!r}"
    )


# ---------------------------------------------------------------------------
# Option 5-lite: conf-file provenance side channel.
# ---------------------------------------------------------------------------

# The side channel is exposed via a parser-introspection method whose
# exact name lives in the implementation. To keep these tests honest
# without coupling them to a tentative name, we look the method up by
# convention on the parsed parser stash (args._parser).
PROVENANCE_GETTER_NAME = "get_conf_file_provenance"


def _provenance_for(args: argparse.Namespace) -> dict:
    """Pull the provenance dict off args._parser; pytest.fail with a
    pointed message if the implementation didn't expose it."""
    parser = getattr(args, "_parser", None)
    if parser is None:
        pytest.fail(
            "args._parser is None — apptools.parseargs is expected to "
            "stash the parser for provenance lookups."
        )
    getter = getattr(parser, PROVENANCE_GETTER_NAME, None)
    if getter is None:
        pytest.fail(
            f"Parser is missing {PROVENANCE_GETTER_NAME}() — the "
            f"provenance side channel from _AccumulatingConfigFileParser "
            f"is not wired through to consumers."
        )
    return getter()


def test_provenance_records_source_file_for_each_conf_value(tmp_path, monkeypatch):
    """For every key that came from a conf file, the provenance side
    channel must record at least one ``(value, source_file, lineno)``
    entry. Lets ``-vv`` diagnostics in
    ``_setup_pkg_config_overrides_locked`` and ``ct-config`` attribute
    every entry on ``PKG_CONFIG_PATH`` back to the conf file (and line)
    that contributed it."""
    args = _parse_with_flavor_conf("b", str(tmp_path), monkeypatch)
    prov = _provenance_for(args)

    flavor_b_conf = os.path.join(_conf_dir(), "flavor-b.conf")
    expected_value_substr = "pkgconfig-b"

    entries = prov.get("prepend-PKG-CONFIG-PATH") or prov.get("prepend_pkg_config_path") or []
    assert entries, (
        f"Provenance dict has no entry for prepend-PKG-CONFIG-PATH. "
        f"Keys present: {sorted(prov.keys())!r}"
    )
    matched = [
        e for e in entries
        if expected_value_substr in str(e[0])
        and os.path.realpath(e[1]) == os.path.realpath(flavor_b_conf)
    ]
    assert matched, (
        f"No provenance entry for value containing {expected_value_substr!r} "
        f"sourced from {flavor_b_conf!r}. All entries for "
        f"prepend-PKG-CONFIG-PATH: {entries!r}"
    )
    for value, source_file, lineno in matched:
        assert isinstance(lineno, int) and lineno >= 1, (
            f"lineno must be a positive int (1-based), got {lineno!r} "
            f"for ({value!r}, {source_file!r}, ...)"
        )


def test_provenance_records_expanded_value_not_literal_placeholder(
    tmp_path, monkeypatch
):
    """The provenance entry should record the **expanded** value (the
    one downstream consumers see on args), not the pre-expansion literal
    with ``${CONF_DIR}`` still in it. That way a -vv dump can be
    grep'd directly against PKG_CONFIG_PATH entries."""
    args = _parse_with_flavor_conf("a", str(tmp_path), monkeypatch)
    prov = _provenance_for(args)

    entries = prov.get("prepend-PKG-CONFIG-PATH") or prov.get("prepend_pkg_config_path") or []
    values = [str(e[0]) for e in entries]
    assert not any("${CONF_DIR}" in v for v in values), (
        f"Provenance entries contain unexpanded ${{CONF_DIR}}: {values!r}"
    )


def test_provenance_records_each_list_element_separately(tmp_path, monkeypatch):
    """When a conf-file value is a JSON list (e.g.,
    ``exemarkers = [main, _start, entry]``), the provenance side channel
    must record ONE entry per element, all tagged with the same source
    file and line number."""
    conf_dir = tmp_path / "axis-confs"
    conf_dir.mkdir()
    conf = conf_dir / "extras.conf"
    conf.write_text("exemarkers = [main, _start, entry]\n")

    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    args = _parse_inline_conf(str(conf), monkeypatch, other_cwd)

    prov = _provenance_for(args)
    all_entries = prov.get("exemarkers") or []
    # Bundled ct.conf may also set exemarkers; filter to entries sourced
    # from the conf this test wrote so the invariant under check (one
    # provenance entry per list element, all sharing source_file:lineno)
    # is exercised in isolation.
    conf_real = os.path.realpath(str(conf))
    entries = [e for e in all_entries if os.path.realpath(e[1]) == conf_real]
    values = [str(e[0]) for e in entries]
    assert "main" in values and "_start" in values and "entry" in values, (
        f"Expected list-element provenance entries for 'main', '_start', "
        f"'entry' sourced from {conf_real!r}; got {entries!r} "
        f"(all entries: {all_entries!r})"
    )
    # All three entries from this conf must share the same source file
    # and line number.
    sources = {(os.path.realpath(e[1]), e[2]) for e in entries}
    assert len(sources) == 1, (
        f"Expected all list-element entries from {conf_real!r} to share "
        f"source_file:lineno; got distinct sources: {sources!r}"
    )
