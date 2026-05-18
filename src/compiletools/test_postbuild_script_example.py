"""End-to-end test: postbuild_script example prints the exact line
that its post-build hook is supposed to produce.

The cross-backend matrix in test_examples_end_to_end_cross_backend.py
already covers "this example builds successfully on every backend".
This test adds the missing piece: the post-build script must set
DEMO_ENV_VAR and exec the binary, whose stdout must be exactly
``DEMO_ENV_VAR=hello-from-postbuild`` — verifying that

* --postbuild-script ran after the build succeeded;
* env-var propagation through subprocess.run(shell=True) works as
  documented;
* exec'ing the freshly-built binary at ``bin/<variant>/<name>``
  succeeds at the moment the post-build hook runs (before _copyexes
  publishes to ``bin/<name>``).
"""

import os
import pathlib
import subprocess

import pytest

import compiletools.testhelper as uth

EXPECTED_LINE = "DEMO_ENV_VAR=hello-from-postbuild"


@uth.requires_functional_compiler
def test_postbuild_script_example_prints_expected_line(tmp_path):
    """Build the postbuild_script example and assert the post-build
    hook's stdout contains exactly the expected line.

    Uses the make backend (always available where a functional compiler
    is) — cross-backend coverage is provided separately by the e2e
    matrix.
    """
    if not uth._backend_tool_available("make"):
        pytest.skip("make not on PATH")

    workspace = uth.copy_example_workspace(
        pathlib.Path(uth.e2e_dir()) / "postbuild_script", tmp_path / "ws"
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
        "--postbuild-script=./run_with_env.sh",
        "--auto",
    ]
    # Strip host *FLAGS so the test isn't at the mercy of operator env,
    # matching test_examples_end_to_end_cross_backend._build_env.
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    # Make sure DEMO_ENV_VAR isn't already set in the parent env — that
    # would defeat the assertion that the post-build script is what
    # produced the value.
    env.pop("DEMO_ENV_VAR", None)

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

    # The post-build hook execs the binary, whose stdout streams up
    # through ct-cake's inherited fds into our capture. Assert the
    # exact line is present, and that the binary did NOT print the
    # "(unset)" fallback (which would mean DEMO_ENV_VAR did not reach
    # the binary's process).
    stdout_lines = result.stdout.splitlines()
    assert EXPECTED_LINE in stdout_lines, (
        f"expected exact line {EXPECTED_LINE!r} in stdout, got:\n{result.stdout}"
    )
    assert "DEMO_ENV_VAR=(unset)" not in result.stdout, (
        f"binary saw unset DEMO_ENV_VAR — post-build hook did not export it.\n"
        f"--- stdout ---\n{result.stdout}"
    )
