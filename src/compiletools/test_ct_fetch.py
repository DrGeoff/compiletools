"""Tests for the standalone ``ct-fetch`` command (``fetch.main``) and its
report-only ``gather_external_status`` helper (Task 12).

All git operations stay local via ``file://`` bare repos (no network), under a
neutralised git environment. A functional C++ compiler is required for anything
that drives ``parseargs`` / headerdeps (the fixpoint probes the compiler for
its built-in macros; the header walk itself does no compilation).
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

import compiletools.fetch as fetch
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.fetch import FetchError, gather_external_status
from compiletools.testhelper import requires_functional_compiler


@pytest.fixture(autouse=True)
def _isolate_callbacks():
    """Reset apptools' module-global substitution callbacks around each test.

    ct-cake's ``registercallback`` (exercised by test_cake_fetch.py) appends to
    apptools' module-global ``_substitutioncallbacks``. A leaked callback would
    otherwise fire during our bare-parser ``parseargs`` and reach for cake-only
    attributes (``args.bindir``). Resetting on both sides keeps this file
    order-independent under xdist.
    """
    uth.reset()
    yield
    uth.reset()


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


def _add_commit_to_bare(root: str, bare_info: dict, rel: str, content: str) -> str:
    """Add a new commit to the bare repo's master (via a fresh clone); return SHA.

    Clones the bare into a throwaway work tree so the push has a real ``origin``
    (the initial ``work`` dir was the clone *source* and has no such remote).
    """
    pusher = os.path.join(root, "pusher")
    _git(root, "clone", "-q", bare_info["bare"], pusher)
    dest = os.path.join(pusher, rel)
    os.makedirs(os.path.dirname(dest) or pusher, exist_ok=True)
    with open(dest, "w") as fh:
        fh.write(content)
    _git(pusher, "add", "-A")
    _git(pusher, "commit", "-q", "-m", "second")
    _git(pusher, "push", "-q", "origin", "master")
    return _git(pusher, "rev-parse", "HEAD")


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


def _neutralise_git(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


def _pinned_argv(main_repo: str, extra: list[str]) -> list[str]:
    """Build a ct-fetch argv with a temp config pinning CXX/CC and -std=c++20.

    Pinning std avoids the bundled default variant's newer -std= (which the
    conftest std-guard would skip on an under-spec toolchain), matching the
    test_cake_fetch.py convention.
    """
    config = uth.create_temp_config(main_repo)
    return ["--config", config, *extra]


# ---------------------------------------------------------------------------
# main() — default (clone) mode
# ---------------------------------------------------------------------------


@requires_functional_compiler
def test_main_default_clones_and_summarizes(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        rc = fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir]))
        assert rc == 0

        clone = os.path.join(externals_dir, "extlib")
        assert os.path.isfile(os.path.join(clone, "include", "extlib.h"))

        out = capsys.readouterr().out
        assert "extlib" in out
        assert clone in out


@requires_functional_compiler
def test_main_empty_targets_is_noop(monkeypatch, capsys) -> None:
    """No on-disk targets: helpful stderr note, returns 0, no crash."""
    with tempfile.TemporaryDirectory() as root:
        main_repo = _make_main_repo(root, "int main() { return 0; }\n")
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        rc = fetch.main(_pinned_argv(main_repo, []))
        assert rc == 0
        captured = capsys.readouterr()
        assert "nothing to do" in captured.err.lower()


# ---------------------------------------------------------------------------
# main() — --update mode
# ---------------------------------------------------------------------------


@requires_functional_compiler
def test_main_update_advances_branch(monkeypatch, capsys) -> None:
    """A branch-pinned external fast-forwards on --update after upstream moves."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        # First run clones at the initial commit.
        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir])) == 0
        clone = os.path.join(externals_dir, "extlib")
        head_before = _git(clone, "rev-parse", "HEAD")
        assert head_before == ext["sha"]
        capsys.readouterr()  # drain

        # Upstream advances; --update fast-forwards the on-disk checkout.
        new_sha = _add_commit_to_bare(root, ext, "include/more.h", "#pragma once\n")
        assert new_sha != ext["sha"]

        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir, "--update"])) == 0
        head_after = _git(clone, "rev-parse", "HEAD")
        assert head_after == new_sha


# ---------------------------------------------------------------------------
# main() — --no-fetch mode
# ---------------------------------------------------------------------------


@requires_functional_compiler
def test_main_no_fetch_missing_returns_nonzero_stderr(monkeypatch, capsys) -> None:
    """--no-fetch with a MISSING external: non-zero, Error:/name on STDERR."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        rc = fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir, "--no-fetch"]))
        assert rc == 1

        captured = capsys.readouterr()
        assert "Error:" in captured.err
        assert "extlib" in captured.err
        assert "extlib" not in captured.out
        assert not os.path.exists(os.path.join(externals_dir, "extlib"))


@requires_functional_compiler
def test_main_no_fetch_present_returns_zero(monkeypatch, capsys) -> None:
    """--no-fetch with the external PRESENT: returns 0."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        # Clone it first (default mode), then verify offline.
        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir])) == 0
        capsys.readouterr()
        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir, "--no-fetch"])) == 0


# ---------------------------------------------------------------------------
# main() — --status mode
# ---------------------------------------------------------------------------


@requires_functional_compiler
def test_main_status_all_present(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir])) == 0
        capsys.readouterr()

        assert fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir, "--status"])) == 0
        out = capsys.readouterr().out
        assert "extlib" in out
        assert "present" in out
        assert ext["sha"] in out  # on_disk_ref shown


@requires_functional_compiler
def test_main_status_missing_does_not_raise(monkeypatch, capsys) -> None:
    """--status reports a missing external as 'missing' without raising."""
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "extlib", {"include/extlib.h": "#pragma once\nint extfn();\n"})
        externals_dir = os.path.join(root, "externals")
        os.makedirs(externals_dir)
        main_repo = _make_main_repo(
            root,
            f'//#GIT={ext["url"]}@master\n#include "extlib.h"\nint main() {{ return extfn(); }}\n',
        )
        monkeypatch.chdir(main_repo)
        _neutralise_git(monkeypatch)

        rc = fetch.main(_pinned_argv(main_repo, ["main.cpp", "--externals-dir", externals_dir, "--status"]))
        assert rc == 0
        out = capsys.readouterr().out
        assert "extlib" in out
        assert "missing" in out
        # No clone was created.
        assert not os.path.exists(os.path.join(externals_dir, "extlib"))


# ---------------------------------------------------------------------------
# gather_external_status — direct unit tests (present / missing / dirty)
# ---------------------------------------------------------------------------


def _make_headerdeps_args() -> object:
    """Minimal args namespace to drive gather_external_status / fetch_externals."""
    import configargparse

    import compiletools.apptools
    import compiletools.headerdeps

    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.headerdeps.add_arguments(cap)
    return compiletools.apptools.parseargs(cap, ["--headerdeps", "direct"], context=BuildContext())


@requires_functional_compiler
def test_gather_external_status_present(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "mylib/foo.h"\nint main() {{ return foo(); }}\n')
        monkeypatch.chdir(root)
        _neutralise_git(monkeypatch)

        # Clone first so it's present.
        fetch.fetch_externals([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)

        statuses = gather_external_status([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)
        assert [s.name for s in statuses] == ["mylib"]
        st = statuses[0]
        assert st.state == "present"
        assert st.source == "managed"
        assert st.on_disk_ref == ext["sha"]
        assert st.path == os.path.join(externals, "mylib")


@requires_functional_compiler
def test_gather_external_status_missing(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f"//#GIT={ext['url']}@master\nint main() {{ return 0; }}\n")
        monkeypatch.chdir(root)
        _neutralise_git(monkeypatch)

        statuses = gather_external_status([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)
        assert [s.name for s in statuses] == ["mylib"]
        st = statuses[0]
        assert st.state == "missing"
        assert st.on_disk_ref is None
        assert not os.path.exists(st.path)


@requires_functional_compiler
def test_gather_external_status_dirty(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as root:
        ext = _make_bare_with_files(root, "mylib", {"foo.h": "#pragma once\nint foo();\n"})
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write(f'//#GIT={ext["url"]}@master\n#include "mylib/foo.h"\nint main() {{ return foo(); }}\n')
        monkeypatch.chdir(root)
        _neutralise_git(monkeypatch)

        fetch.fetch_externals([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)

        # Dirty the work tree.
        with open(os.path.join(externals, "mylib", "foo.h"), "a") as fh:
            fh.write("// local edit\n")

        statuses = gather_external_status([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)
        assert statuses[0].state == "dirty"


@requires_functional_compiler
def test_gather_external_status_none_declared(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write("int main() { return 0; }\n")
        monkeypatch.chdir(root)
        _neutralise_git(monkeypatch)

        statuses = gather_external_status([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)
        assert statuses == []


@requires_functional_compiler
def test_gather_external_status_malformed_raises(monkeypatch) -> None:
    """A malformed //#GIT= value still raises (source-code defect the user sees)."""
    with tempfile.TemporaryDirectory() as root:
        externals = os.path.join(root, "externals")
        os.makedirs(externals)
        main = os.path.join(root, "main.cpp")
        with open(main, "w") as fh:
            fh.write("//#GIT=notaurl\nint main() { return 0; }\n")
        monkeypatch.chdir(root)
        _neutralise_git(monkeypatch)

        with pytest.raises(FetchError):
            gather_external_status([main], _make_headerdeps_args(), BuildContext(), externals_dir=externals)
