"""ct-filelist --auto: discovery reuses the shared
findtargets.discover_targets_and_reanchor driver, so its target set matches
what ct-cake --auto would build."""

import os
from typing import Any

import pytest

import compiletools.apptools
import compiletools.filelist
import compiletools.findtargets
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path


@pytest.fixture(autouse=True)
def _reset_parser_state():
    uth.reset()
    yield
    uth.reset()


def _run_filelist(cwd, argv):
    with uth.DirectoryContext(str(cwd)):
        with uth.ParserContext():
            assert compiletools.filelist.main(list(argv)) == 0


def _discovered_targets(cwd):
    """The exes and tests ct-cake --auto would build in *cwd*."""
    with uth.DirectoryContext(str(cwd)):
        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("filelist auto oracle", argv=[])
            compiletools.findtargets.add_arguments(cap)
            context = BuildContext()
            args = compiletools.apptools.parseargs(cap, [], context=context)
            exes, tests = compiletools.findtargets.FindTargets(args, context=context)()
            return sorted(exes + tests)


def test_auto_lists_every_source_ct_cake_auto_would_build(capsys: Any) -> None:
    sample_dir = example_path("simple")
    expected = _discovered_targets(sample_dir)
    assert expected, "oracle found no targets; the example dir moved"

    _run_filelist(sample_dir, ["--auto"])

    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert set(expected) <= listed


def test_bare_invocation_discovers(capsys: Any) -> None:
    """--auto is on by default, matching ct-cake and
    ct-compilation-database: a bare ct-filelist used to print nothing and
    now lists the targets it would build."""
    sample_dir = example_path("simple")
    _run_filelist(sample_dir, [])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert set(_discovered_targets(sample_dir)) <= listed


def test_no_auto_keeps_a_bare_invocation_silent(capsys: Any) -> None:
    """The opt-out for packaging scripts that pass an explicit file list
    and must not pay for a filesystem walk."""
    _run_filelist(example_path("simple"), ["--no-auto"])
    assert capsys.readouterr().out.strip() == ""


def test_explicit_target_and_auto_do_not_both_apply(capsys: Any) -> None:
    """Same gate as cake.py: an explicit target suppresses discovery, so
    the output stays scoped to what the caller asked for."""
    sample_dir = example_path("simple")
    _run_filelist(sample_dir, ["--auto", "helloworld_cpp.cpp"])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert listed == {os.path.realpath(os.path.join(sample_dir, "helloworld_cpp.cpp"))}


def test_filelist_parser_keeps_its_own_style_choices() -> None:
    """Registering the discovery arguments must not import ct-findtargets'
    incompatible --style (choices null/flat/indent/args, default indent)."""
    cap = compiletools.apptools.create_parser("filelist style", argv=[])
    compiletools.filelist.Filelist.add_arguments(cap)
    args = cap.parse_args([])
    assert args.style == "flat"
    assert args.auto is True
