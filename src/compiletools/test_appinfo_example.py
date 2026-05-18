"""End-to-end test: appinfo example links the generated implementation
file and the binary prints the exact name/version baked in.

This complements the cross-backend matrix in
test_examples_end_to_end_cross_backend.py, which only proves the build
succeeds. Here we prove the generated .cpp's symbols actually reached
the linked exe — i.e., the implied-source mechanism (Hunter looking
for ``appinfo.cpp`` adjacent to ``#include "appinfo.hpp"``) picked up
the prebuild-generated file rather than the build silently producing
a stub with unresolved externs.

Uses the make backend (always available where a functional compiler
is) — cross-backend coverage is provided separately by the e2e matrix.
"""

import os
import pathlib
import subprocess

import pytest

import compiletools.testhelper as uth

EXPECTED_NAME = "demo_app"
EXPECTED_VERSION = "1.2.3"


@uth.requires_functional_compiler
def test_appinfo_example_prints_generated_symbols(tmp_path):
    """Build the appinfo example with APP_NAME / APP_VERSION pinned in
    the env; assert the exe prints exactly the values the prebuild
    script wrote into appinfo.cpp.
    """
    if not uth._backend_tool_available("make"):
        pytest.skip("make not on PATH")

    workspace = uth.copy_example_workspace(
        pathlib.Path(uth.e2e_dir()) / "appinfo", tmp_path / "ws"
    )
    cas_root = workspace  # in-tree cas layout

    argv = [
        "ct-cake",
        "--backend=make",
        f"--cas-objdir={cas_root}/cas-objdir",
        f"--bindir={workspace}/bin",
        f"--cas-pchdir={cas_root}/cas-pchdir",
        f"--cas-pcmdir={cas_root}/cas-pcmdir",
        f"--cas-exedir={cas_root}/cas-exedir",
        f"--diagnostics-dir={workspace}/diagnostics",
        "--prebuild-script=./gen_appinfo.sh appinfo.cpp",
        "--auto",
    ]
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    env["APP_NAME"] = EXPECTED_NAME
    env["APP_VERSION"] = EXPECTED_VERSION

    result = subprocess.run(
        argv,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"ct-cake failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    # The published exe lives at bin/main when --bindir is explicit.
    exe = workspace / "bin" / "main"
    assert exe.is_file(), f"expected exe at {exe}; bin contents: {list((workspace / 'bin').glob('*'))}"

    proc = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, (
        f"exe exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    lines = proc.stdout.splitlines()
    assert f"name={EXPECTED_NAME}" in lines, (
        f"expected 'name={EXPECTED_NAME}' in exe stdout, got:\n{proc.stdout}"
    )
    assert f"version={EXPECTED_VERSION}" in lines, (
        f"expected 'version={EXPECTED_VERSION}' in exe stdout, got:\n{proc.stdout}"
    )
