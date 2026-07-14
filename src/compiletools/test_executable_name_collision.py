"""Executable-layout collision coverage.

Two executable sources with the same basename in different directories must
NOT map to the same ``executable_pathname`` — ``BuildGraph.add_rule`` is
keyed by output with last-write-wins semantics, so a shared path silently
drops one link (or publish) rule: one binary is never produced, one test
suite never runs, exit code stays 0.

The fix mirrors the source directory under bindir
(``appalpha/main.cpp`` → ``bin/<variant>/appalpha/main``), with a backstop
``_check_executable_collisions`` that raises for residual collisions
mirroring cannot separate (``main.cpp`` + ``main.c`` in one directory).
"""

import os

import pytest

import compiletools.testhelper as uth
from compiletools.build_backend import BuildBackend
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.test_namer import _make_namer


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe the global configargparse parser cache around every test."""
    uth.reset()
    yield
    uth.reset()


def test_same_basename_different_dirs_get_distinct_executable_pathnames():
    """Two subprojects' main.cpp must land at distinct mirrored paths.

    Under ``--no-git-root`` the anchor is the cwd; ``/repo/...`` sources
    live outside it, so the full mount-stripped path mirrors under bindir.
    """
    _args, namer = _make_namer("TestExeCollision")

    exe_alpha = namer.executable_pathname("/repo/appalpha/main.cpp")
    exe_beta = namer.executable_pathname("/repo/appbeta/main.cpp")

    assert exe_alpha != exe_beta, (
        f"Distinct executable sources must not share an output path: "
        f"appalpha/main.cpp and appbeta/main.cpp both map to {exe_alpha!r}"
    )
    assert exe_alpha == "bin/gcc.debug/repo/appalpha/main"
    assert exe_beta == "bin/gcc.debug/repo/appbeta/main"


def test_control_build_graph_add_rule_last_write_wins():
    """Pins the documented last-write-wins collapse of BuildGraph.add_rule.

    This dict semantics is relied on by phony/mkdir rules; the collision
    fix works upstream (distinct outputs + backstop check), not here.
    """
    graph = BuildGraph()
    rule_alpha = BuildRule(
        output="bin/gcc.debug/main",
        inputs=["/repo/appalpha/main.o"],
        command=["g++", "-o", "bin/gcc.debug/main", "/repo/appalpha/main.o"],
        rule_type="link",
    )
    rule_beta = BuildRule(
        output="bin/gcc.debug/main",
        inputs=["/repo/appbeta/main.o"],
        command=["g++", "-o", "bin/gcc.debug/main", "/repo/appbeta/main.o"],
        rule_type="link",
    )
    graph.add_rule(rule_alpha)
    graph.add_rule(rule_beta)

    assert len(graph) == 1, "same-output rules collapse into one"
    survivor = graph.get_rule("bin/gcc.debug/main")
    assert survivor is not None
    assert survivor.inputs == ["/repo/appbeta/main.o"], "last writer wins"


def test_control_distinct_basenames_get_distinct_pathnames():
    """Distinct basenames get distinct targets (as they always did)."""
    _args, namer = _make_namer("TestExeDistinct")

    exe_alpha = namer.executable_pathname("/repo/appalpha/alpha_main.cpp")
    exe_beta = namer.executable_pathname("/repo/appbeta/beta_main.cpp")

    assert exe_alpha != exe_beta
    assert os.path.basename(exe_alpha) == "alpha_main"
    assert os.path.basename(exe_beta) == "beta_main"


class _ConcreteBackend(BuildBackend):
    """Minimal instantiable backend for exercising planning helpers."""

    @staticmethod
    def name():
        return "test-collision"

    @staticmethod
    def build_filename():
        return "Collisionfile"

    def generate(self, graph, output=None):
        raise NotImplementedError

    def _execute_build(self, target):
        raise NotImplementedError


def _make_backend(tmpdir, **arg_overrides):
    args = uth.make_backend_args(tmpdir, **arg_overrides)
    sources = list(args.filename or []) + list(args.tests or [])
    hunter = uth.make_mock_hunter(
        sources=sources,
        per_file_magicflags={s: {} for s in sources},
    )
    backend = _ConcreteBackend.__new__(_ConcreteBackend)
    backend.args = args
    backend.hunter = hunter
    backend.namer = uth.make_mock_namer(args)
    backend.context = BuildContext()
    backend._anchor_root = ""
    return backend


def test_check_executable_collisions_raises_naming_both_sources(tmp_path):
    """Backstop for collisions mirroring cannot separate.

    ``main.cpp`` and ``main.c`` in ONE directory still share
    ``<bindir>/<dir>/main``; the pre-link check must raise and name both
    offending sources instead of silently dropping one link rule.
    """
    backend = _make_backend(
        str(tmp_path),
        filename=["/proj/app/main.cpp"],
        tests=["/proj/app/main.c"],
    )

    with pytest.raises(ValueError) as excinfo:
        backend._check_executable_collisions()

    message = str(excinfo.value)
    assert "/proj/app/main.cpp" in message
    assert "/proj/app/main.c" in message


def test_check_executable_collisions_passes_for_distinct_outputs(tmp_path):
    backend = _make_backend(
        str(tmp_path),
        filename=["/proj/app/alpha.cpp"],
        tests=["/proj/app/beta.cpp"],
    )
    backend._check_executable_collisions()  # must not raise


def test_publish_rule_order_only_dep_is_the_output_dir(tmp_path):
    """Mirrored publish targets need their own directory as the order-only
    dep, not the flat base bindir."""
    backend = _make_backend(str(tmp_path))
    bindir = backend.args.bindir

    user_path = os.path.join(bindir, "appalpha", "main")
    rule = backend._build_publish_rule("/cas/ab/main_abcd.exe", user_path)

    assert rule.order_only_deps == [os.path.join(bindir, "appalpha")]


def test_link_rule_emits_per_library_search_dirs(tmp_path):
    """Libraries mirror too, so a single ``-L<bindir>`` no longer finds
    them; the link argv must carry each library's own directory."""
    backend = _make_backend(str(tmp_path), filename=["/src/main.cpp"])
    bindir = backend.args.bindir
    lib_output = os.path.join(bindir, "applib", "libdep.a")

    rules = backend._create_link_rule("/src/main.cpp", library_outputs=[lib_output])
    link_cmd = rules[0].command
    assert link_cmd is not None

    assert f"-L{os.path.join(bindir, 'applib')}" in link_cmd
    assert "-ldep" in link_cmd


def test_xml_path_flattens_mirrored_exe_paths(tmp_path):
    """JUnit XML files are keyed on the bindir-relative exe path so
    mirrored test exes get distinct XML files in one flat xml dir."""
    backend = _make_backend(str(tmp_path))
    backend.args.test_xml_dir = os.path.join(str(tmp_path), "xml")
    backend.args.variant = "gcc.debug"
    bindir = backend.args.bindir

    xml_alpha = backend._xml_path_for(os.path.join(bindir, "appalpha", "main"))
    xml_beta = backend._xml_path_for(os.path.join(bindir, "appbeta", "main"))

    assert xml_alpha != xml_beta
    assert os.path.dirname(xml_alpha) == os.path.dirname(xml_beta)
    assert os.path.basename(xml_alpha) == "appalpha_main.xml"
    assert os.path.basename(xml_beta) == "appbeta_main.xml"


def test_target_name_for_mirrored_outputs(tmp_path):
    """cmake/bazel target names derive from the bindir-relative path with
    ``__`` joining path components; root-level outputs keep bare names."""
    backend = _make_backend(str(tmp_path))
    bindir = backend.args.bindir

    name_alpha = backend._target_name_for(os.path.join(bindir, "appalpha", "main"))
    name_beta = backend._target_name_for(os.path.join(bindir, "appbeta", "main"))
    name_root = backend._target_name_for(os.path.join(bindir, "standalone"))

    assert name_alpha == "appalpha__main"
    assert name_beta == "appbeta__main"
    assert name_root == "standalone"


def test_target_name_for_raises_on_aliased_names(tmp_path):
    """The ``__`` join can alias (``a__b/main`` vs ``a/b__main``); the
    helper must raise naming both outputs rather than silently merging
    two native-tool targets."""
    backend = _make_backend(str(tmp_path))
    bindir = backend.args.bindir

    first = os.path.join(bindir, "a__b", "main")
    second = os.path.join(bindir, "a", "b__main")
    backend._target_name_for(first)

    with pytest.raises(ValueError) as excinfo:
        backend._target_name_for(second)

    message = str(excinfo.value)
    assert first in message
    assert second in message
