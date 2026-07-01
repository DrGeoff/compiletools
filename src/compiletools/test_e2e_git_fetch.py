"""End-to-end tests for the ``//#GIT=`` multi-repo external-fetch feature.

Unlike ``test_cake_fetch.py`` (which exercises ``Cake._fetch_and_register_externals``
in-process and stops at the frozen ``args.flags`` level), these tests drive the
real ``ct-cake`` entry point as a subprocess, so they cover the *whole* pipeline:
auto-clone the external -> fold its include dirs into the compile -> discover the
external's implied source -> compile -> link -> produce and RUN an executable.

Two axes are covered:

* **Test A (build & run)** — a main repo whose ``main.cpp`` declares a
  ``//#GIT=<file:// url>@master`` external, ``#include``\\s the external's
  header, and calls a function *defined in the external's source*. A passing
  run proves the external's ``.cpp`` was really compiled and linked (not
  stubbed): the produced executable is located and executed, and its stdout is
  asserted to be the external function's distinctive return value.

* **Test B (determinism)** — the same scenario built in two workspaces living
  at different absolute paths. Two path-canonical determinism properties are
  asserted:

    1. *Rebuild idempotence* — building twice in one workspace produces the
       identical set of ``cas-objdir/**/*.o`` filenames (no churn / no drift
       from the fetch step).
    2. *Content-addressed external* — the external source's object
       ``file_hash`` component is identical across the two workspace paths
       (content-addressed, workspace-path-independent).

  The full object *filename* is deliberately NOT asserted equal across
  workspaces: the ``//#GIT=`` URL is a per-workspace ``file://`` absolute path
  baked into ``main.cpp``, and the external's ``-I`` include dir is an absolute
  path *outside* the gitroot (the externals dir is a sibling of the gitroot, so
  gitroot-relative cache-key canonicalisation does not reach it). Both
  legitimately shift the per-workspace hash components; only the external
  source's content hash is genuinely path-invariant.

Everything is hermetic: no network, all git operations go through local
``file://`` bare repos under a neutralised git environment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import compiletools.testhelper as uth

# Skip the whole module if either (a) the worktree's venv doesn't match this
# src tree (the e2e build would silently exercise the wrong compiletools
# install) or (b) ct-cake itself isn't on PATH. Mirrors the cas-reuse e2e
# suite's module marker.
pytestmark = uth.skipif_e2e_unavailable(
    lambda: shutil.which("ct-cake") is not None,
    "ct-cake not on PATH; run `uv pip install -e .` in this worktree",
)


def _git_env() -> dict[str, str]:
    """Deterministic git environment with no ambient-config bleed.

    Mirrors ``test_cake_fetch._git_env`` so the local bare-repo commits use a
    fixed identity and never read the user's system/global git config.
    """
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


def _e2e_env() -> dict[str, str]:
    """Stripped env for the ``ct-cake`` subprocess.

    Preserves PATH (so ct-cake + the compiler resolve) and forwards the
    compiler/variant selectors, exactly like the cas-reuse suite's ``_e2e_env``.
    Adds git isolation vars so the clone ct-cake performs in-subprocess doesn't
    read ambient git config. No shell config is sourced.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    # LD_PRELOAD preserved for Termux exec; see
    # test_e2e_cas_reuse_across_workspaces._e2e_env.
    for k in ("CXX", "CC", "CPP", "VARIANT", "HOME", "LD_LIBRARY_PATH", "LD_PRELOAD", "TMPDIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


# Distinctive return value so a passing run proves the external's code (not a
# stub) was linked in.
_EXT_MAGIC = 42


def _make_external_bare_repo(root: str) -> str:
    """Create a ``file://`` bare external repo ``extlib`` and return its URL.

    The external ships a header AND a co-located source that defines
    ``extfn()``. Co-location matters: compiletools' implied-source discovery
    (``utils.implied_source``) finds ``extlib.cpp`` by looking beside the
    resolved ``extlib.h``, so the external's source is compiled and linked
    automatically once its ``include/`` dir is on the include path.
    """
    work = os.path.join(root, "extlib-work")
    include = os.path.join(work, "include")
    os.makedirs(include)
    with open(os.path.join(include, "extlib.h"), "w") as fh:
        fh.write("#pragma once\nint extfn();\n")
    with open(os.path.join(include, "extlib.cpp"), "w") as fh:
        fh.write(f'#include "extlib.h"\nint extfn() {{ return {_EXT_MAGIC}; }}\n')
    _git(root, "init", "-q", "-b", "master", work)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = os.path.join(root, "extlib.git")
    _git(root, "clone", "-q", "--bare", work, bare)
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/master")
    return "file://" + bare


def _make_main_repo(root: str, ext_url: str) -> str:
    """Create the main git repo (at ``<root>/main``) with a ``//#GIT=`` main.cpp.

    ``main`` prints ``extfn()`` to stdout so the test can assert the external's
    code was linked by observing the printed value.
    """
    main_repo = os.path.join(root, "main")
    os.makedirs(main_repo)
    _git(main_repo, "init", "-q", "-b", "master", ".")
    with open(os.path.join(main_repo, "main.cpp"), "w") as fh:
        fh.write(
            f"//#GIT={ext_url}@master\n"
            '#include "extlib.h"\n'
            "#include <cstdio>\n"
            'int main() { std::printf("%d\\n", extfn()); return 0; }\n'
        )
    _git(main_repo, "add", "-A")
    _git(main_repo, "commit", "-q", "-m", "main")
    return main_repo


def _build_scenario(root: str) -> dict[str, str]:
    """Materialise a full main+external scenario rooted at *root*.

    Returns a dict of the paths the tests need: ``main_repo``, ``externals_dir``,
    ``bindir``, ``config``.
    """
    ext_url = _make_external_bare_repo(root)
    main_repo = _make_main_repo(root, ext_url)
    # Pin CXX/CC + -std=c++20 so the build doesn't inherit the bundled default
    # variant's newer -std= (which would need a newer toolchain), mirroring
    # test_cake_fetch's _build_cake_args.
    config = uth.create_temp_config(main_repo)
    externals_dir = os.path.join(root, "externals")
    os.makedirs(externals_dir, exist_ok=True)
    bindir = os.path.join(root, "bin")
    return {
        "main_repo": main_repo,
        "externals_dir": externals_dir,
        "bindir": bindir,
        "config": config,
    }


def _run_ct_cake(scenario: dict[str, str], *extra_args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Invoke ``ct-cake`` in the scenario's main repo with the e2e + git env."""
    cmd = [
        "ct-cake",
        "--config",
        scenario["config"],
        "--exemarkers=main",
        "--testmarkers=unittest.hpp",
        "--filename",
        "main.cpp",
        "--externals-dir",
        scenario["externals_dir"],
        "--bindir",
        scenario["bindir"],
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=scenario["main_repo"],
        timeout=timeout,
        env=_e2e_env(),
    )


def _assert_build_ok(result: subprocess.CompletedProcess, where: str) -> None:
    assert result.returncode == 0, f"ct-cake failed in {where}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _object_filenames(main_repo: str) -> set[str]:
    """Set of ``cas-objdir/**/*.o`` basenames produced under *main_repo*."""
    cas = os.path.join(main_repo, "cas-objdir")
    assert os.path.isdir(cas), f"cas-objdir not produced under {main_repo}"
    names: set[str] = set()
    for _dirpath, _dirnames, filenames in os.walk(cas):
        for fn in filenames:
            if fn.endswith(".o"):
                names.add(fn)
    return names


def _external_file_hash(main_repo: str) -> str:
    """The ``file_hash`` component of the external's ``extlib`` object.

    Object naming is ``{basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o``;
    the first hex group after ``extlib_`` is the content-addressed file hash.
    The 12/14/16 widths are pinned so a naming-format shift is caught as a
    no-match (raising below) rather than silently absorbed by a loose pattern.
    """
    for name in _object_filenames(main_repo):
        m = re.match(r"extlib_([0-9a-f]{12})_[0-9a-f]{14}_[0-9a-f]{16}\.o$", name)
        if m:
            return m.group(1)
    raise AssertionError(f"no extlib_*.o object found under {main_repo}/cas-objdir")


@uth.requires_functional_compiler
def test_git_external_builds_and_runs(tmp_path):
    """A ``//#GIT=`` main is cloned, its source compiled+linked, and the
    resulting executable runs and prints the external function's value.
    """
    root = str(tmp_path / "scenario")
    os.makedirs(root)
    scenario = _build_scenario(root)

    result = _run_ct_cake(scenario)
    _assert_build_ok(result, root)

    # (1) The external was actually cloned to <externals>/extlib.
    clone_header = os.path.join(scenario["externals_dir"], "extlib", "include", "extlib.h")
    assert os.path.isfile(clone_header), (
        f"external not cloned to {clone_header}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # (2) The executable was produced at the explicit bindir.
    exe = os.path.join(scenario["bindir"], "main")
    assert os.path.isfile(exe), (
        f"executable not produced at {exe}; bindir contents: "
        f"{os.listdir(scenario['bindir']) if os.path.isdir(scenario['bindir']) else '<missing>'}"
    )
    assert os.access(exe, os.X_OK), f"{exe} is not executable"

    # (3) Run it: stdout must be the external's distinctive return value,
    # proving the external's .cpp was really compiled and linked (a missing
    # link would have failed the build at step (1)/link time, but running it
    # is the strongest end-to-end proof).
    run = subprocess.run([exe], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, f"{exe} exited {run.returncode}:\nstderr:\n{run.stderr}"
    assert run.stdout.strip() == str(_EXT_MAGIC), (
        f"expected external extfn()=={_EXT_MAGIC} on stdout, got {run.stdout!r}"
    )


@uth.requires_functional_compiler
def test_git_external_build_is_deterministic_across_workspaces(tmp_path):
    """Path-canonical determinism for the ``//#GIT=`` build.

    Two properties, both true and meaningful for this feature:

    1. Rebuild idempotence: building twice in one workspace yields the
       identical set of object filenames.
    2. Content-addressed external: the external source's object ``file_hash``
       component matches across two workspaces at different absolute paths.
    """
    root1 = str(tmp_path / "ws1-a-deliberately-longer-workspace-path")
    root2 = str(tmp_path / "w2")
    os.makedirs(root1)
    os.makedirs(root2)
    scenario1 = _build_scenario(root1)
    scenario2 = _build_scenario(root2)

    # Build ws1 twice to check rebuild idempotence.
    _assert_build_ok(_run_ct_cake(scenario1), root1)
    objs_first = _object_filenames(scenario1["main_repo"])
    assert objs_first, f"no objects produced under {root1}"

    _assert_build_ok(_run_ct_cake(scenario1), root1)
    objs_second = _object_filenames(scenario1["main_repo"])

    only_first = objs_first - objs_second
    only_second = objs_second - objs_first
    assert not only_first and not only_second, (
        "rebuild in the same workspace changed the object filename set (fetch "
        "step introduced churn / non-determinism):\n"
        f"  only in first build:  {sorted(only_first)}\n"
        f"  only in second build: {sorted(only_second)}"
    )

    # Build ws2 (different absolute path).
    _assert_build_ok(_run_ct_cake(scenario2), root2)

    # The external source's content-addressed file_hash must be identical
    # across the two workspace paths — it is the genuinely path-invariant
    # component of the object name.
    fh1 = _external_file_hash(scenario1["main_repo"])
    fh2 = _external_file_hash(scenario2["main_repo"])
    assert fh1 == fh2, (
        "external object file_hash differs across workspaces — the external "
        "source's content hash leaked a workspace-absolute path:\n"
        f"  ws1: {fh1}\n  ws2: {fh2}"
    )
