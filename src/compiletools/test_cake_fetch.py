"""Integration tests for ct-cake's //#GIT= external-fetch step (Task 11).

These exercise the real ``Cake._fetch_and_register_externals`` code path:
collect targets -> ``fetch.fetch_externals`` -> append to ``args.INCLUDE`` ->
re-run ``substitutions`` -> assert the frozen ``args.flags`` picked up the
external's include dirs and that the populate-once/freeze invariant holds.

All git operations stay local via ``file://`` bare repos (no network), under a
neutralised git environment. A functional C++ compiler is required because the
fetch fixpoint drives headerdeps, which probes the compiler for built-ins.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.fetch import FetchError
from compiletools.testhelper import requires_functional_compiler


def _git_env() -> dict:
    """Deterministic git environment with no ambient-config bleed."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "ct-test",
            "GIT_AUTHOR_EMAIL": "ct-test@example.com",
            "GIT_COMMITTER_NAME": "ct-test",
            "GIT_COMMITTER_EMAIL": "ct-test@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": env.get("HOME", "/tmp"),
        }
    )
    return env


def _git(cwd: str, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.STDOUT, text=True, env=_git_env()).strip()


def _make_bare_with_files(root: str, name: str, files: dict[str, str]) -> dict:
    """Create a bare git repo named *name* whose initial commit holds *files*."""
    work = os.path.join(root, name + "-work")
    bare = os.path.join(root, name + ".git")
    os.makedirs(work)
    _git(root, "init", "-q", "-b", "master", work)
    for rel, content in files.items():
        dest = os.path.join(work, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh:
            fh.write(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    sha = _git(work, "rev-parse", "HEAD")
    _git(root, "clone", "-q", "--bare", work, bare)
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/master")
    return {"url": "file://" + bare, "bare": bare, "work": work, "sha": sha}


def _make_main_repo(root: str, main_source: str) -> str:
    """Create a git repo at <root>/main holding main.cpp, return its path."""
    main_repo = os.path.join(root, "main")
    os.makedirs(main_repo)
    _git(main_repo, "init", "-q", "-b", "master", ".")
    with open(os.path.join(main_repo, "main.cpp"), "w") as fh:
        fh.write(main_source)
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-q", "-m", "main")
    return main_repo


def _build_cake_args(main_repo: str, extra_argv: list[str], context: BuildContext):
    """Parse the real ct-cake parser surface, pinning a functional compiler.

    A temp config in *main_repo* fixes CXX/CC to the detected functional
    compiler and ``-std=c++20`` so the run doesn't inherit the bundled
    default variant's newer ``-std=`` (which would need a newer toolchain).
    """
    config = uth.create_temp_config(main_repo)
    argv = ["--config", config, "--exemarkers=main", "--testmarkers=unittest.hpp", *extra_argv]
    cap = compiletools.apptools.create_parser("test-cake-fetch", argv=argv)
    compiletools.cake.Cake.add_arguments(cap)
    compiletools.cake.Cake.registercallback()
    return compiletools.apptools.parseargs(cap, argv, context=context)


@requires_functional_compiler
def test_fetch_step_registers_external_include_dirs(monkeypatch) -> None:
    """End-to-end: a //#GIT= main gets the external cloned and its include dir
    folded into the frozen args.flags, with no flag-string drift."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )

        monkeypatch.chdir(main_repo)
        # Neutralise ambient git config for the in-process git calls fetch makes.
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        context = BuildContext()
        args = _build_cake_args(main_repo, ["--filename", "main.cpp", "--externals-dir", externals_dir], context)

        # No flag drift right after parseargs.
        compiletools.apptools.check_flag_string_drift(args)

        cake = compiletools.cake.Cake(args, context=context)
        changed = cake._fetch_and_register_externals()
        assert changed is True

        # (1) The external got cloned under externals_dir.
        clone = os.path.join(externals_dir, "extlib")
        assert os.path.isfile(os.path.join(clone, "include", "extlib.h"))

        # INCLUDE now carries the external's include dir; re-run substitutions
        # exactly as process() does to redistribute it into the frozen flags.
        assert os.path.join(clone, "include") in args.INCLUDE.split()
        compiletools.apptools.substitutions(args, verbose=0)

        # (2) args.flags (frozen) carries -I entries pointing at the external.
        cxx_tokens = list(args.flags.cxx)
        joined = " ".join(cxx_tokens)
        assert os.path.join(clone, "include") in joined
        assert clone in joined

        # (3) The populate-once/freeze invariant still holds.
        compiletools.apptools.check_flag_string_drift(args)


@requires_functional_compiler
def test_fetch_step_no_targets_is_noop(monkeypatch) -> None:
    """With no on-disk targets the fetch step is a no-op (returns False)."""
    with tempfile.TemporaryDirectory() as root:
        main_repo = _make_main_repo(root, "int main() { return 0; }\n")
        monkeypatch.chdir(main_repo)
        context = BuildContext()
        # Point at a non-existent target so the file filter drops it.
        args = _build_cake_args(main_repo, ["--filename", "does-not-exist.cpp"], context)
        cake = compiletools.cake.Cake(args, context=context)
        assert cake._fetch_and_register_externals() is False


@requires_functional_compiler
def test_fetch_step_no_fetch_offline_surfaces_fetcherror(monkeypatch) -> None:
    """--no-fetch with a missing external surfaces a clean FetchError."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )

        monkeypatch.chdir(main_repo)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        context = BuildContext()
        args = _build_cake_args(
            main_repo, ["--filename", "main.cpp", "--externals-dir", externals_dir, "--no-fetch"], context
        )
        cake = compiletools.cake.Cake(args, context=context)

        with pytest.raises(FetchError) as excinfo:
            cake._fetch_and_register_externals()
        assert "extlib" in str(excinfo.value)
        # Nothing was cloned.
        assert not os.path.exists(os.path.join(externals_dir, "extlib"))


@requires_functional_compiler
def test_filelist_does_not_clone_missing_external(monkeypatch) -> None:
    """--filelist is a read-only query: it MUST NOT trigger a live network
    clone of a not-yet-present //#GIT= external. Instead it runs the fetch
    step under offline (no_fetch) semantics -- present externals are used,
    a missing one fails fast with a FetchError rather than cloning."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )

        monkeypatch.chdir(main_repo)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        context = BuildContext()
        # Note: no --no-fetch on the command line; --filelist alone must be
        # enough to keep the fetch step offline.
        args = _build_cake_args(
            main_repo, ["--filename", "main.cpp", "--externals-dir", externals_dir, "--filelist"], context
        )
        cake = compiletools.cake.Cake(args, context=context)

        with pytest.raises(FetchError) as excinfo:
            cake._fetch_and_register_externals()
        assert "extlib" in str(excinfo.value)
        # The read-only query performed no network clone.
        assert not os.path.exists(os.path.join(externals_dir, "extlib"))


@requires_functional_compiler
def test_filelist_uses_present_external_without_network(monkeypatch, capsys) -> None:
    """--filelist with an already-present external produces a complete file
    list (including the external's implied source) and never hits the
    network -- proven by deleting the bare origin before the run."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(
            root,
            "extlib",
            {
                "extlib.h": "#pragma once\nint extfn();\n",
                "extlib.cpp": '#include "extlib.h"\nint extfn() { return 0; }\n',
            },
        )
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )

        # Pre-clone the external so it is already present on disk, then delete
        # the bare origin: any attempt to reach the network would now fail.
        clone = os.path.join(externals_dir, "extlib")
        _git(root, "clone", "-q", ext["bare"], clone)
        shutil.rmtree(ext["bare"])

        monkeypatch.chdir(main_repo)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        context = BuildContext()
        args = _build_cake_args(
            main_repo, ["--filename", "main.cpp", "--externals-dir", externals_dir, "--filelist"], context
        )
        cake = compiletools.cake.Cake(args, context=context)
        cake.process()

        out = capsys.readouterr().out
        # The external's implementation source shows up in the file list.
        assert os.path.join(clone, "extlib.cpp") in out


@requires_functional_compiler
def test_single_static_lib_picks_up_external_implied_source(monkeypatch) -> None:
    """A single-source static-lib target whose header pulls in an external
    that provides the matching implementation .cpp must, after the fetch
    step widens the include path, re-discover that implementation source and
    add it to args.static (the FINDING 5 ordering gap)."""
    with tempfile.TemporaryDirectory() as root:
        # External provides a co-located header + implementation so the
        # implied-source (foo.h -> foo.cpp) rule can reach the .cpp once the
        # external root is on the include path.
        ext = _make_bare_with_files(
            root,
            "extlib",
            {
                "extlib.h": "#pragma once\nint extfn();\n",
                "extlib.cpp": '#include "extlib.h"\nint extfn() { return 0; }\n',
            },
        )
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)

        # Single-source static-lib seed: its header include pulls in the
        # external, whose implied source is only reachable post-fetch.
        main_repo = os.path.join(root, "main")
        os.makedirs(main_repo)
        _git(main_repo, "init", "-q", "-b", "master", ".")
        with open(os.path.join(main_repo, "mylib.cpp"), "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint mylibfn() {{ return extfn(); }}\n')
        _git(main_repo, "add", "-A")
        _git(main_repo, "commit", "-q", "-m", "main")

        monkeypatch.chdir(main_repo)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        context = BuildContext()
        args = _build_cake_args(main_repo, ["--static", "mylib.cpp", "--externals-dir", externals_dir], context)
        cake = compiletools.cake.Cake(args, context=context)
        cake._discover_targets()

        clone = os.path.join(externals_dir, "extlib")
        # The external's implementation source was folded into the static-lib
        # source set despite the seed being a single file at discovery start.
        ext_impl = os.path.join(clone, "extlib.cpp")
        static_realpaths = {os.path.realpath(p) for p in args.static}
        assert os.path.realpath(ext_impl) in static_realpaths


@requires_functional_compiler
def test_main_fetcherror_returns_nonzero_and_writes_stderr(monkeypatch, capsys) -> None:
    """The --no-fetch missing-external FetchError reaches cake.main()'s
    dedicated handler: non-zero exit, clean message on STDERR (not STDOUT)."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )

        monkeypatch.chdir(main_repo)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

        config = uth.create_temp_config(main_repo)
        argv = [
            "--config",
            config,
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--filename",
            "main.cpp",
            "--externals-dir",
            externals_dir,
            "--no-fetch",
        ]

        try:
            rc = compiletools.cake.main(argv)
        finally:
            # main() calls Cake.registercallback(), which appends a callback to
            # apptools' module-global _substitutioncallbacks list. Reset it so
            # the leaked callback can't fire against a later test's bare args.
            uth.reset()
        assert rc == 1

        captured = capsys.readouterr()
        # The clean fatal message went to STDERR (consistent with the sibling
        # CalledProcessError / OSError / LDFLAGSCycleError handlers), and names
        # the offending external.
        assert "extlib" in captured.err
        assert "extlib" not in captured.out
        # Nothing was cloned.
        assert not os.path.exists(os.path.join(externals_dir, "extlib"))
