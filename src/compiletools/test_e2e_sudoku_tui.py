"""End-to-end test for the ``sudoku_tui`` example -- the ``//#GIT=`` showcase.

DELIBERATELY NETWORK-TOUCHING: the example's whole point is a real remote
declaration (``//#GIT=https://github.com/DrGeoff/sudoku.git@master``), so this
test drives ``ct-cake`` through a REAL clone of the real URL -- no
``CT_GIT_PATH_SUDOKU`` shortcut, because exercising clone -> resolve ->
include-widening IS the test. When github.com is unreachable the test skips at
runtime (reachability is probed inside the test, not at collection, so offline
full-suite runs pay the probe only in the worker that reaches this test).

The hermetic fetch coverage lives in test_fetch.py / test_cake_fetch.py /
test_e2e_git_fetch.py. The cross-backend matrix also builds this example per
backend (``ExamplePlan.needs_network_clone`` reuses this module's
``_github_sudoku_reachable`` probe); this test keeps the deeper behavioural
assertions (auto-demo output, clone reuse, CAS stability).

What one passing run proves end-to-end: the declaration in stepper.hpp (a
transitive header) is found, the external is cloned at the declared ref, the
include path widens so ``#include "sudoku/src/grid.hpp"`` resolves, hunter
discovers the upstream ``constraintregion.cpp`` implied source, test_stepper
compiles/links/RUNS against the external (testmarkers), the exe publishes to
the bindir, and its non-TTY auto-demo solves the embedded puzzle.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess

import pytest

import compiletools.examples_registry as er
import compiletools.testhelper as uth

pytestmark = uth.skipif_e2e_unavailable(
    lambda: shutil.which("ct-cake") is not None,
    "ct-cake not on PATH; run `uv pip install -e .` in this worktree",
)

_SUDOKU_URL = "https://github.com/DrGeoff/sudoku.git"


def _git_env() -> dict[str, str]:
    """Deterministic git environment with no ambient-config bleed.

    Mirrors ``test_e2e_git_fetch._git_env``.
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
    """Stripped env for the ``ct-cake`` subprocess (mirrors test_e2e_git_fetch)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for k in ("CXX", "CC", "CPP", "VARIANT", "HOME", "LD_LIBRARY_PATH", "LD_PRELOAD", "TMPDIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


@functools.cache
def _github_sudoku_reachable() -> bool:
    """One ls-remote probe per process; cached so parallel tests share it."""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", _SUDOKU_URL, "HEAD"],
            capture_output=True,
            timeout=15,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _make_workspace(tmp_path) -> str:
    """Copy the example into a tmp git repo (the copy becomes the gitroot)."""
    ws = str(tmp_path / "ws")
    shutil.copytree(er.example_path("sudoku_tui"), ws)
    _git(ws, "init", "-q", "-b", "master", ".")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "sudoku_tui example workspace")
    return ws


def _run_ct_cake(ws: str, externals_dir: str, bindir: str) -> subprocess.CompletedProcess:
    # --externals-dir overrides the example ct.conf's /tmp default so the
    # clone stays inside this test's tmp_path.
    cmd = ["ct-cake", "--externals-dir", externals_dir, "--bindir", bindir]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ws, timeout=600, env=_e2e_env())


def _object_filenames(ws: str) -> set[str]:
    """Set of ``cas-objdir/**/*.o`` basenames produced under the workspace."""
    cas = os.path.join(ws, "cas-objdir")
    assert os.path.isdir(cas), f"cas-objdir not produced under {ws}"
    names: set[str] = set()
    for _dirpath, _dirnames, filenames in os.walk(cas):
        for fn in filenames:
            if fn.endswith(".o"):
                names.add(fn)
    return names


@uth.requires_functional_compiler
@uth.requires_compiler_supports_default_std
def test_sudoku_tui_fetches_builds_and_runs(tmp_path):
    if not _github_sudoku_reachable():
        pytest.skip(f"cannot reach {_SUDOKU_URL} (offline or blocked)")

    ws = _make_workspace(tmp_path)
    externals_dir = str(tmp_path / "externals")
    bindir = str(tmp_path / "bin")

    result = _run_ct_cake(ws, externals_dir, bindir)
    assert result.returncode == 0, f"ct-cake failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # (1) The external was really cloned from GitHub at the declared layout.
    clone = os.path.join(externals_dir, "sudoku")
    assert os.path.isfile(os.path.join(clone, "src", "grid.hpp")), (
        f"external not cloned to {clone}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # (2) The exe published to the bindir (test_stepper already ran inline --
    # a failing test would have failed the build).
    exe = os.path.join(bindir, "sudoku_tui")
    assert os.path.isfile(exe), (
        f"executable not produced at {exe}; bindir contents: "
        f"{os.listdir(bindir) if os.path.isdir(bindir) else '<missing>'}"
    )
    assert os.access(exe, os.X_OK)

    # (3) The non-TTY auto-demo solves the embedded puzzle deterministically.
    run = subprocess.run([exe], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, f"{exe} exited {run.returncode}:\nstderr:\n{run.stderr}"
    assert run.stdout.startswith("solved in "), f"expected the auto-demo 'solved in ...' line, got {run.stdout!r}"

    # (4) A second build reuses the present clone (no pull without --update)
    # and the CAS (no object churn).
    head_before = _git(clone, "rev-parse", "HEAD")
    objs_before = _object_filenames(ws)
    rerun = _run_ct_cake(ws, externals_dir, bindir)
    assert rerun.returncode == 0, f"ct-cake re-run failed:\nstdout:\n{rerun.stdout}\nstderr:\n{rerun.stderr}"
    assert _git(clone, "rev-parse", "HEAD") == head_before, "re-run moved the external's HEAD"
    assert _object_filenames(ws) == objs_before, "re-run churned cas-objdir object names"
