"""ct-filelist --auto: discovery reuses the shared
findtargets.discover_targets_and_reanchor driver, so its target set matches
what ct-cake --auto would build."""

import os
import subprocess
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


def test_auto_exclude_drops_a_discovered_target(capsys: Any) -> None:
    """The excluded file is a discovered TEST here, so dropping it also
    drops ct-filelist's "every file beside a test" extras rule -- which is
    what makes the exclusion observable in the output at all."""
    sample_dir = example_path("simple")
    _run_filelist(sample_dir, ["--auto", "--auto-exclude=test_*.c"])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert os.path.realpath(os.path.join(sample_dir, "helloworld_cpp.cpp")) in listed
    assert os.path.realpath(os.path.join(sample_dir, "test_cflags.c")) not in listed


def test_explicit_target_and_auto_do_not_both_apply(capsys: Any) -> None:
    """Same gate as cake.py: an explicit target suppresses discovery, so
    the output stays scoped to what the caller asked for."""
    sample_dir = example_path("simple")
    _run_filelist(sample_dir, ["--auto", "helloworld_cpp.cpp"])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert listed == {os.path.realpath(os.path.join(sample_dir, "helloworld_cpp.cpp"))}


@pytest.fixture
def fixpoint_repo(tmp_path):
    """A repo whose round-one discovery loads a conf that CHANGES round-two
    discovery: appbeta/main.cpp is an exe under the gitroot's
    ``exemarkers = [main]``, and appbeta/ct.conf -- reachable only once that
    target is discovered -- excludes its own directory.

    A single discovery pass therefore lists appbeta/main.cpp and the
    re-anchoring fixpoint does not, which is what makes ct-filelist's use of
    the shared driver observable from the output alone. The exclusion is
    scoped to appbeta so appalpha stays discovered and an empty output
    cannot pass the test by accident.

    An exclusion, not the ``exemarkers`` swap that discriminates the same
    fixpoint in test_subproject_conf_discovery: a subproject's exemarkers
    value REPLACES the project-tier one for the whole run, so round two
    would discover nothing at all and take the appalpha control with it."""
    root = tmp_path / "monorepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ct.conf").write_text("exemarkers = [main]\ntestmarkers = unit_test.hpp\n")

    appalpha = root / "appalpha"
    appalpha.mkdir()
    (appalpha / "main.cpp").write_text("int main() { return 0; }\n")

    appbeta = root / "appbeta"
    appbeta.mkdir()
    (appbeta / "ct.conf").write_text("append-AUTO-EXCLUDE = ${CONF_DIR}\n")
    # Distinct bodies: the global hash registry refuses to reverse-look-up a
    # content hash shared by two tracked files.
    (appbeta / "main.cpp").write_text("int main() { return 1; }\n")
    return root


def test_auto_honours_a_subproject_conf_discovered_mid_pass(fixpoint_repo, capsys: Any) -> None:
    """ct-filelist must go through discover_targets_and_reanchor, not run
    one discovery pass. Replacing the driver call in filelist.main with a
    single FindTargets(...).process(args) leaves appbeta/main.cpp in the
    output and fails this test; every other test in this file survives that
    mutation, so this is the one that pins the wiring."""
    _run_filelist(fixpoint_repo, ["--auto"])

    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert os.path.realpath(str(fixpoint_repo / "appalpha" / "main.cpp")) in listed
    assert os.path.realpath(str(fixpoint_repo / "appbeta" / "main.cpp")) not in listed


def test_filelist_parser_keeps_its_own_style_choices() -> None:
    """Registering the discovery arguments must not import ct-findtargets'
    incompatible --style (choices null/flat/indent/args, default indent)."""
    cap = compiletools.apptools.create_parser("filelist style", argv=[])
    compiletools.filelist.Filelist.add_arguments(cap)
    args = cap.parse_args([])
    assert args.style == "flat"
    assert args.auto is True


@pytest.fixture
def sibling_repo(tmp_path):
    """A repo whose test file has a sibling nothing includes.

    ct-filelist adds every file beside a discovered test, so the sibling
    reaches the output through that sweep alone -- not as a target and not
    as a header dependency -- which is what makes the sweep's treatment of
    --auto-exclude observable."""
    root = tmp_path / "siblingrepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ct.conf").write_text("exemarkers = [main]\ntestmarkers = unit_test.hpp\n")

    app = root / "app"
    app.mkdir()
    (app / "test_widget.cpp").write_text('#include "unit_test.hpp"\nint main() { return 0; }\n')
    (app / "unit_test.hpp").write_text("#pragma once\n")
    (app / "generated_table.inc").write_text("// nothing includes this\n")
    return root


def test_a_test_file_sibling_is_listed_when_nothing_excludes_it(sibling_repo, capsys: Any) -> None:
    _run_filelist(sibling_repo, ["--auto"])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert os.path.realpath(str(sibling_repo / "app" / "generated_table.inc")) in listed


def test_auto_exclude_reaches_the_files_swept_in_beside_a_test(sibling_repo, capsys: Any) -> None:
    """The sweep is a consequence of discovery, so a pattern that governs
    discovery has to govern it too; otherwise an excluded file returns to
    the list through a neighbour."""
    _run_filelist(sibling_repo, ["--auto", "--auto-exclude=generated_table.inc"])
    listed = {line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert os.path.realpath(str(sibling_repo / "app" / "test_widget.cpp")) in listed
    assert os.path.realpath(str(sibling_repo / "app" / "generated_table.inc")) not in listed
