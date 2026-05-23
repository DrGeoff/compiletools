"""Pin a latent build-backend bug: a *relative* ``--cas-pchdir`` breaks the PCH
precompile rule when ct-cake is invoked from a subdirectory of the gitroot.

Root cause (``build_backend.py``, ``_create_pch_rules``, the
``rule_cwd = anchor_root`` branch). For cross-user PCH byte-identity the rule
passes the PCH source *relative to the gitroot* and runs the compiler under
``cwd = anchor_root`` (``cd <gitroot> && g++ ...``). But the ``-o`` output path
(the cas-pchdir target) and the ``mv`` targets are emitted relative to the
*invocation cwd*, not the gitroot. When ``--cas-pchdir`` is a relative path and
the invocation cwd is a subdirectory of the gitroot, ``make`` creates the cache
directory relative to its own cwd while ``cd <gitroot> && g++ -o <relpath>``
resolves the same relative path against the gitroot -- a different, nonexistent
directory -- and gcc fails with ``cannot create precompiled header ...: No such
file or directory``. The C++20 module/BMI rule (the symmetric
``pcm_rule_cwd = anchor_root`` branch) shares the flaw.

These tests PIN CURRENT (BUGGY) BEHAVIOR:
  * a relative ``--cas-pchdir`` from a subdir of the gitroot -> the build fails
    with the PCH-output-path signature;
  * an absolute ``--cas-pchdir`` (the workaround) from the same cwd -> the build
    succeeds.

When the backend is fixed (absolutize the PCH/PCM rule's ``-o`` / ``mv`` / lock
paths against the invocation cwd before the ``cd <anchor_root> &&`` wrapper),
the first test will start building successfully and must be flipped to assert
success. See ``examples-features/relative_cas_dir_bug/README.md``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import compiletools.testhelper as uth
from compiletools.examples_registry import example_path


def _nested_workspace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay out a workspace with gitroot=<repo> and the PCH consumer one level
    down in ``<repo>/sub``.

    The ``.git`` marker at ``<repo>`` makes ct-cake resolve the gitroot ABOVE
    the invocation cwd (``<repo>/sub``) -- the structural precondition for the
    bug. (``copy_example_workspace`` plants ``.git`` at the copy destination, so
    it cannot produce a cwd-below-gitroot layout; hence the manual setup here.)
    """
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    src = pathlib.Path(example_path("relative_cas_dir_bug"))
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, sub)
    return repo, sub


def _build(sub: pathlib.Path, repo: pathlib.Path, cas_pchdir: str) -> subprocess.CompletedProcess:
    """Run ct-cake on ``widget.cpp`` from ``<sub>``.

    Only ``--cas-pchdir`` varies between the bug case (relative) and the control
    (absolute); the other cas dirs and the bindir are absolute so the test
    isolates the cas-pchdir output path. Host ``*FLAGS`` are stripped so the
    build isn't at the mercy of the operator's environment.
    """
    cache = repo / "cache"
    argv = [
        "ct-cake",
        "widget.cpp",
        f"--bindir={repo}/bin",
        f"--cas-pchdir={cas_pchdir}",
        f"--cas-objdir={cache}/obj",
        f"--cas-pcmdir={cache}/pcm",
        f"--cas-exedir={cache}/exe",
    ]
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    return subprocess.run(argv, cwd=sub, env=env, capture_output=True, text=True)


@uth.requires_functional_compiler
def test_relative_cas_pchdir_from_subdir_breaks_pch_rule(tmp_path):
    """Relative ``--cas-pchdir`` + cwd below the gitroot -> PCH rule fails.

    Pins the current latent bug; see the module docstring for the flip-on-fix
    instruction.
    """
    repo, sub = _nested_workspace(tmp_path)
    # Relative cas-pchdir: resolved against cwd=sub by make, but the PCH rule
    # cd's to the gitroot before invoking gcc -- so the relative -o path points
    # at <gitroot>/relcache, which does not exist.
    result = _build(sub, repo, "relcache/pch")

    assert result.returncode != 0, (
        "expected the relative --cas-pchdir build to FAIL (this test pins the "
        "current latent bug); if it now succeeds the backend was fixed -- flip "
        "this assertion to assert success and update the README.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "cannot create precompiled header" in combined and "No such file or directory" in combined, (
        "the build failed, but not with the relative-cas-pchdir PCH-output-path "
        "signature -- the failure is something else and this test is no longer "
        f"pinning the intended bug.\n--- stderr ---\n{result.stderr}"
    )


@uth.requires_functional_compiler
def test_absolute_cas_pchdir_from_subdir_is_the_workaround(tmp_path):
    """Absolute ``--cas-pchdir`` from the same cwd builds cleanly (the
    documented workaround)."""
    repo, sub = _nested_workspace(tmp_path)
    # Absolute cas-pchdir: the PCH rule's -o is absolute, so the cd <gitroot> is
    # harmless and the build succeeds.
    abs_pch = repo / "abscache" / "pch"
    result = _build(sub, repo, str(abs_pch))

    assert result.returncode == 0, (
        "absolute --cas-pchdir should build cleanly from a subdir of the "
        f"gitroot.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert sorted(abs_pch.rglob("*.gch")), f"expected a cached .gch under the absolute cas-pchdir {abs_pch}"
