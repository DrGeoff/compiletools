"""Publish/clean scoping for cake's top-level bin directory.

``ct-cake --clean`` must remove exactly what ``_copyexes`` published:
never other variants' trees, user files under ``bin/``, or (for
dot-prefixed / bare-dot bindirs) the workspace itself.
"""

import os
import subprocess

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe the global configargparse parser cache around every test."""
    uth.reset()
    yield
    uth.reset()


def _write_config(tmpdir):
    config_name = uth.create_temp_config(tmpdir)
    uth.create_temp_ct_conf(tempdir=tmpdir, defaultvariant=os.path.basename(config_name)[:-5])
    return config_name


def _make_cake(config_name, filenames, extra_argv=()):
    """Build a Cake with parsed args, mirroring cake.main()'s parser setup."""
    argv = [
        "--exemarkers=main",
        "--testmarkers=unittest.hpp",
        "--config=" + config_name,
        *extra_argv,
    ]
    uth.reset()
    cap = compiletools.apptools.create_parser("test clean topbindir", argv=argv)
    compiletools.cake.Cake.add_arguments(cap)
    compiletools.cake.Cake.registercallback()
    args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())
    args.output = None
    args.filename = list(filenames)
    args.static = None
    args.dynamic = None
    cake = compiletools.cake.Cake(args)
    cake._createctobjs()
    assert cake.namer is not None
    return cake


def _write_source(relpath):
    os.makedirs(os.path.dirname(relpath), exist_ok=True)
    with open(relpath, "w") as f:
        f.write("int main() { return 0; }\n")


def _fake_build_and_publish(cake):
    """Touch executable artefacts at the namer's paths and publish them."""
    for srcexe in cake.namer.all_executable_pathnames():
        os.makedirs(os.path.dirname(srcexe), exist_ok=True)
        with open(srcexe, "w") as f:
            f.write("fake")
        os.chmod(srcexe, 0o755)
    cake._copyexes()


def _tree(root):
    paths = set()
    for r, dirs, files in os.walk(root):
        for name in list(dirs) + list(files):
            paths.add(os.path.relpath(os.path.join(r, name), root))
    return paths


def test_clean_leaves_other_variant_tree_intact():
    """Cleaning variant 1 must not delete variant 2's tree under bin/
    nor variant 2's published copies."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")
        _write_source("appbeta/main2.cpp")

        cake2 = _make_cake(config, [os.path.join(tmpdir, "appbeta", "main2.cpp")], ["--bindir=bin/v2"])
        _fake_build_and_publish(cake2)
        assert os.path.exists(os.path.join("bin", "v2", "appbeta", "main2"))
        assert os.path.exists(os.path.join("bin", "appbeta", "main2"))

        cake1 = _make_cake(config, [os.path.join(tmpdir, "appalpha", "main.cpp")], ["--bindir=bin/v1"])
        _fake_build_and_publish(cake1)
        assert os.path.exists(os.path.join("bin", "appalpha", "main"))

        cake1._clean_topbindir()

        assert not os.path.exists(os.path.join("bin", "appalpha", "main"))
        assert os.path.exists(os.path.join("bin", "v2", "appbeta", "main2")), (
            "clean of variant 1 must leave variant 2's build tree intact"
        )
        assert os.path.exists(os.path.join("bin", "appbeta", "main2")), (
            "clean of variant 1 must leave variant 2's published copy intact"
        )


def test_user_file_under_bin_survives_clean():
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")

        cake = _make_cake(config, [os.path.join(tmpdir, "appalpha", "main.cpp")], ["--bindir=bin/v1"])
        _fake_build_and_publish(cake)

        with open(os.path.join("bin", "README"), "w") as f:
            f.write("user file\n")

        cake._clean_topbindir()

        assert os.path.exists(os.path.join("bin", "README"))
        assert not os.path.exists(os.path.join("bin", "appalpha", "main"))


def test_dot_prefixed_bindir_publishes_under_bin_and_clean_spares_workspace():
    """--bindir=./bin/<v> normalizes to bin/<v> at parse time, so publish
    lands under bin/ (not ./) and clean never walks the workspace root."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")

        cake = _make_cake(config, [os.path.join(tmpdir, "appalpha", "main.cpp")], ["--bindir=./bin/v1"])
        assert cake.args.bindir == os.path.join("bin", "v1"), (
            f"bindir must be normalized at parse time, got {cake.args.bindir!r}"
        )
        assert cake.namer is not None
        assert cake.namer.topbindir() == "bin" + os.sep

        with open("keepme.txt", "w") as f:
            f.write("sentinel\n")
        os.makedirs(os.path.join(".git"), exist_ok=True)
        with open(os.path.join(".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")

        _fake_build_and_publish(cake)
        assert os.path.exists(os.path.join("bin", "appalpha", "main"))
        assert not os.path.exists(os.path.join("appalpha", "main")), (
            "publish must land under bin/, not the workspace root"
        )

        cake._clean_topbindir()

        assert os.path.exists("keepme.txt")
        assert os.path.exists(os.path.join(".git", "HEAD"))
        assert os.path.exists(os.path.join("appalpha", "main.cpp"))
        assert not os.path.exists(os.path.join("bin", "appalpha", "main"))


def test_bindir_dot_clean_spares_workspace():
    """--bindir=. makes dest == src; clean removes only the published
    artefacts, never workspace files or .git."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")

        cake = _make_cake(config, [os.path.join(tmpdir, "appalpha", "main.cpp")], ["--bindir=."])
        assert cake.args.bindir == "."

        with open("keepme.txt", "w") as f:
            f.write("sentinel\n")
        os.makedirs(os.path.join(".git"), exist_ok=True)
        with open(os.path.join(".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")

        _fake_build_and_publish(cake)
        assert os.path.exists(os.path.join("appalpha", "main"))

        cake._clean_topbindir()

        assert os.path.exists("keepme.txt")
        assert os.path.exists(os.path.join(".git", "HEAD"))
        assert os.path.exists(os.path.join("appalpha", "main.cpp"))
        assert not os.path.exists(os.path.join("appalpha", "main"))


def test_clean_removes_exactly_published_set():
    """Publish/clean round-trip: clean removes the published dests plus
    the mirror dirs it emptied — nothing else — and bin/ itself survives."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")
        with open("solo.cpp", "w") as f:
            f.write("int main() { return 0; }\n")

        cake = _make_cake(
            config,
            [os.path.join(tmpdir, "appalpha", "main.cpp"), os.path.join(tmpdir, "solo.cpp")],
            ["--bindir=bin/v1"],
        )
        _fake_build_and_publish(cake)

        before = _tree(tmpdir)
        cake._clean_topbindir()
        after = _tree(tmpdir)

        removed = before - after
        expected_removed = {
            os.path.join("bin", "appalpha", "main"),
            os.path.join("bin", "appalpha"),
            os.path.join("bin", "solo"),
        }
        assert removed == expected_removed, f"clean removed {removed}, expected exactly {expected_removed}"
        assert os.path.isdir("bin")
        assert os.path.exists(os.path.join("bin", "v1", "appalpha", "main"))
        assert os.path.exists(os.path.join("bin", "v1", "solo"))


@uth.requires_functional_compiler
@pytest.mark.parametrize("clean_flag", ["--clean", "--realclean"])
def test_real_cake_clean_with_bindir_dot_spares_workspace(clean_flag):
    """End-to-end regression for the --bindir=. workspace wipe: run the real
    ct-cake build then the real --clean/--realclean flow (cake.main, which
    exercises BuildBackend.clean()/realclean() BEFORE _clean_topbindir) in a
    sandboxed git repo, and assert the workspace — sources, user files,
    .git — survives with only build artifacts removed."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        config = _write_config(tmpdir)
        _write_source("appalpha/main.cpp")
        with open("keepme.txt", "w") as f:
            f.write("sentinel\n")
        subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True)

        argv = [
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--config=" + config,
            "--bindir=.",
            "--no-auto",
            os.path.join(tmpdir, "appalpha", "main.cpp"),
        ]
        with uth.ParserContext():
            assert compiletools.cake.main(list(argv)) == 0
        published = os.path.join("appalpha", "main")
        assert os.path.exists(published), "build should publish next to the source for --bindir=."

        uth.reset()
        with uth.ParserContext():
            assert compiletools.cake.main(list(argv) + [clean_flag]) == 0

        assert os.path.exists("keepme.txt"), f"{clean_flag} deleted user files in the workspace"
        assert os.path.isdir(".git"), f"{clean_flag} deleted .git"
        assert os.path.exists(os.path.join("appalpha", "main.cpp")), f"{clean_flag} deleted sources"
        assert not os.path.exists(published), f"{clean_flag} should remove the published executable"
