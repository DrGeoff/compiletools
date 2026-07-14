"""End-to-end test: the basename_collision example builds BOTH binaries.

Two subprojects (appalpha/, appbeta/) each hold a ``main.cpp``. Under the
flat bindir layout both mapped to ``bin/<variant>/main`` and the second
link (or publish) rule silently replaced the first — one binary was never
produced and ct-cake exited 0. The source-mirrored layout places them at
``bin/<variant>/appalpha/main`` and ``bin/<variant>/appbeta/main``; this
test builds the example with --auto and asserts both exist and print
their distinct identifying lines.
"""

import os
import pathlib
import subprocess

import pytest

import compiletools.testhelper as uth


@uth.requires_functional_compiler
def test_basename_collision_example_builds_both_binaries(tmp_path):
    if not uth._backend_tool_available("make"):
        pytest.skip("make not on PATH")

    workspace = uth.copy_example_workspace(pathlib.Path(uth.e2e_dir()) / "basename_collision", tmp_path / "ws")

    argv = [
        "ct-cake",
        "--backend=make",
        f"--cas-objdir={workspace}/cas-objdir",
        f"--bindir={workspace}/bin",
        f"--cas-pchdir={workspace}/cas-pchdir",
        f"--cas-pcmdir={workspace}/cas-pcmdir",
        f"--cas-exedir={workspace}/cas-exedir",
        f"--diagnostics-dir={workspace}/diagnostics",
        "--auto",
    ]
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)

    result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"ct-cake failed (exit {result.returncode})\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    bindir_root = pathlib.Path(workspace) / "bin"
    exes = {}
    for exe_path in bindir_root.rglob("main"):
        if exe_path.is_file() and os.access(exe_path, os.X_OK):
            exes[exe_path.parent.name] = exe_path

    assert "appalpha" in exes and "appbeta" in exes, (
        f"expected bin/<variant>/appalpha/main AND bin/<variant>/appbeta/main; "
        f"found only {sorted(exes)} under {bindir_root}"
    )

    for subproject, expected_line in (("appalpha", "appalpha"), ("appbeta", "appbeta")):
        run = subprocess.run([str(exes[subproject])], capture_output=True, text=True)
        assert run.returncode == 0
        assert run.stdout.strip() == expected_line, (
            f"{exes[subproject]} printed {run.stdout!r}, expected {expected_line!r}"
        )
