"""Smoke coverage for the ``examples-end-to-end/*/build.sh`` tour scripts.

The cross-backend matrix builds each example with ``ct-cake`` directly and
never execs its ``build.sh``, so the scripts themselves were unverified: a
flag renamed out from under a tour step (or a step that only works on a
non-default backend) stayed green in CI and broke for the reader following
the README. This module runs each script end to end in a throwaway git
workspace and asserts it exits 0.

Smoke depth only. What a step *achieves* belongs in that feature's own test
(``test_postbuild_script_example.py``, ``test_apptools``'s per-axis variant
coverage, and so on); what is checked here is that every documented command
still parses and runs against the current CLI.
"""

from __future__ import annotations

import glob
import os
import pathlib
import shutil
import subprocess

import pytest

import compiletools.testhelper as uth

# Discovered from disk rather than hand-listed so a new tour script is
# covered the moment it lands; _EXPECTED_BUILD_SCRIPTS below is the
# deliberate-decision gate on that set changing.
_BUILD_SCRIPTS: tuple[str, ...] = tuple(
    sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(uth.e2e_dir(), "*", "build.sh")))
)

_EXPECTED_BUILD_SCRIPTS = frozenset(
    {
        "appinfo",
        "cli_features",
        "ffile_prefix_map",
        "multi_axis_variant",
        "postbuild_script",
        "prebuild_script",
        "project_version",
        "testprefix",
    }
)

# Scripts whose steps pin a non-default backend. cli_features step 8
# demonstrates --use-mtime=True, which only make and ninja honour, so it
# spells out --backend=make and needs make on PATH.
_REQUIRED_BACKEND_TOOL = {"cli_features": "make"}


def _bash_available() -> bool:
    """The scripts carry a ``#!/usr/bin/bash`` shebang, not ``/usr/bin/env``."""
    return os.path.exists("/usr/bin/bash")


def _make_script_workspace(source_dir: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """Copy *source_dir* to *dest* as a real git repository.

    ``uth.copy_example_workspace`` plants a ``.git`` directory holding only
    ``HEAD``, which is enough for ``git_utils.find_git_root`` but NOT for
    ``git rev-parse --show-toplevel`` -- and cli_features/build.sh calls
    exactly that under ``set -e``. So the marker is replaced with an
    initialised repo.
    """
    uth.copy_example_workspace(source_dir, dest)
    shutil.rmtree(dest / ".git")
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    return dest


def _run_build_script(workspace: pathlib.Path) -> subprocess.CompletedProcess:
    """Exec ``<workspace>/build.sh`` and clean up the ``/tmp`` dirs it makes.

    cli_features steps 7 and 9 write ``/tmp/ct-diag-$$`` and
    ``/tmp/ct-cache-$$``, outside any pytest tmpdir. Because the script is
    exec'd directly, its ``$$`` is the child's pid, so those paths are known
    exactly and only the run's own directories are removed -- a glob over
    ``/tmp/ct-diag-*`` would delete a concurrent peer's.
    """
    script = str(workspace / "build.sh")
    with subprocess.Popen(
        [script],
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        output, _ = proc.communicate()
        pid = proc.pid
    for leaked in (f"/tmp/ct-diag-{pid}", f"/tmp/ct-cache-{pid}"):
        shutil.rmtree(leaked, ignore_errors=True)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout=output, stderr="")


def test_every_discovered_build_script_is_registered() -> None:
    """Drift guard, same contract as the cross-backend matrix's plan check:
    adding or deleting a tour script is a deliberate decision, not a silent
    change in what CI covers."""
    discovered = set(_BUILD_SCRIPTS)
    assert discovered, f"no build.sh found under {uth.e2e_dir()}; the examples layout moved"
    missing = sorted(discovered - _EXPECTED_BUILD_SCRIPTS)
    assert not missing, (
        "examples-end-to-end build.sh scripts not registered in _EXPECTED_BUILD_SCRIPTS:\n  " + "\n  ".join(missing)
    )
    stale = sorted(_EXPECTED_BUILD_SCRIPTS - discovered)
    assert not stale, "_EXPECTED_BUILD_SCRIPTS names scripts that no longer exist:\n  " + "\n  ".join(stale)


@uth.requires_functional_compiler
@uth.skipif_e2e_unavailable(_bash_available, "/usr/bin/bash not present (build.sh shebang)")
@pytest.mark.parametrize("example_name", _BUILD_SCRIPTS)
def test_build_script_runs_clean(example_name: str, tmp_path) -> None:
    """Every documented step in the tour still runs against the current CLI.

    ``set -e`` at the top of each script means the first step whose flag was
    renamed, whose default moved, or whose backend rejects it takes the whole
    run non-zero -- so exit status is a sufficient observable here and the
    captured output is attached for the reader.
    """
    tool = _REQUIRED_BACKEND_TOOL.get(example_name)
    if tool is not None and shutil.which(tool) is None:
        pytest.skip(f"{example_name}/build.sh pins --backend={tool}, which is not on PATH")

    workspace = _make_script_workspace(
        pathlib.Path(uth.e2e_dir()) / example_name,
        tmp_path / "ws",
    )
    result = _run_build_script(workspace)
    assert result.returncode == 0, f"{example_name}/build.sh exited {result.returncode}\n{result.stdout}"


@uth.skipif_e2e_unavailable(_bash_available, "/usr/bin/bash not present (build.sh shebang)")
def test_the_runner_reports_a_failing_step(tmp_path) -> None:
    """Negative control for the test above.

    ``set -e`` plus a non-zero exit is the entire failure signal, so a runner
    that dropped the returncode -- or a script whose steps silently no-op --
    would leave every cell above passing vacuously. Appending a failing step
    to a real tour script must be observable through the same helper.
    """
    workspace = _make_script_workspace(
        pathlib.Path(uth.e2e_dir()) / "project_version",
        tmp_path / "ws",
    )
    script = workspace / "build.sh"
    script.write_text(script.read_text() + "\nct-cake --this-flag-does-not-exist\n")

    result = _run_build_script(workspace)
    assert result.returncode != 0
