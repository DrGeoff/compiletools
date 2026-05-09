"""Layer 5 end-to-end smoke test: CAS reuse across workspace moves.

Builds the same sample twice under two different absolute workspace
paths, then asserts that the produced object filenames in cas-objdir
are byte-identical across the two runs. Filenames encode the three
hashes (file, dep, macro_state) so identical filenames prove all
three are stable across the workspace move.

This is the test that would have caught the original bug.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import compiletools.testhelper as uth

pytestmark = pytest.mark.skipif(
    shutil.which("ct-cake") is None,
    reason="ct-cake not on PATH; run `uv pip install -e .` in this worktree",
)


@uth.requires_functional_compiler
def test_object_cache_filenames_match_across_workspace_paths(tmp_path):
    """Same sample compiled in two workspace dirs produces identical
    object filenames in each workspace's cas-objdir/.

    Asserts the canonicalizer is doing its job at the macro_state_hash
    component of the object filename: identical TUs share cache entries
    even when the workspace itself lives at a different absolute path.
    """
    sample_src = os.path.join(uth.samplesdir(), "factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "factory"
    ws2 = tmp_path / "ws2" / "factory"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)

    def _build(workdir) -> None:
        # Stripped env so user's shell config can't shift CXX/variant
        # between the two builds. PATH is preserved so ct-cake and the
        # compiler resolve normally.
        env = {"PATH": os.environ.get("PATH", "")}
        # LD_PRELOAD preserved for Termux: libtermux-exec.so is required to
        # exec binaries on Android; without it the compiler subprocess fails
        # with EACCES. Harmless on other platforms (typically unset).
        for k in ("CXX", "CC", "CPP", "VARIANT", "HOME", "LD_LIBRARY_PATH", "LD_PRELOAD", "TMPDIR"):
            if k in os.environ:
                env[k] = os.environ[k]
        result = subprocess.run(
            ["ct-cake", "--auto"],
            capture_output=True,
            text=True,
            cwd=str(workdir),
            timeout=180,
            env=env,
        )
        assert result.returncode == 0, (
            f"ct-cake --auto failed in {workdir}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    _build(ws1)
    _build(ws2)

    def _object_filenames(workdir) -> set[str]:
        cas = workdir / "cas-objdir"
        assert cas.is_dir(), f"cas-objdir not produced under {workdir}; ct-cake may have used a different layout"
        # Layout: cas-objdir/{variant}/{2-char-shard}/{basename}_<hashes>.o
        # Recurse and collect basenames across all variants/shards.
        return {p.name for p in cas.rglob("*.o")}

    objs_ws1 = _object_filenames(ws1)
    objs_ws2 = _object_filenames(ws2)

    assert objs_ws1, f"no object files produced under {ws1}/cas-objdir"
    assert objs_ws2, f"no object files produced under {ws2}/cas-objdir"

    only_in_ws1 = objs_ws1 - objs_ws2
    only_in_ws2 = objs_ws2 - objs_ws1
    assert not only_in_ws1 and not only_in_ws2, (
        "object filenames differ across workspaces (cache-key path-bound):\n"
        f"  only in ws1: {sorted(only_in_ws1)}\n"
        f"  only in ws2: {sorted(only_in_ws2)}"
    )
