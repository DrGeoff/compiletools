"""Two-checkout byte-identity test for cross-user CAS sharing (Round 3).

The same source compiled at two distinct workspace paths must produce
byte-identical CAS-layer outputs, so two users sharing a cas-objdir on
NFS get true cross-user cache hits. Round 3 design doc:
docs/superpowers/specs/2026-05-12-round3-workspace-relative-compile-paths-design.md

Mechanism (under test): apptools._inject_ffile_prefix_map appends
``-ffile-prefix-map=<gitroot>=<target>`` (default target ``.``) to
CXXFLAGS / CFLAGS so paths the compiler emits (debug info, __FILE__,
.d output) are anchor-relative. Link rules pass ldflags through
canonicalize_for_command so RPATH / version-script paths under the
gitroot become target-prefixed in the emitted argv too.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess

import pytest

import compiletools.testhelper as uth


def _hash_tree(root: pathlib.Path, suffixes: tuple[str, ...]) -> dict[str, str]:
    """Return ``{relpath_under_root: sha256_hex}`` for every file under
    ``root`` whose name ends with one of ``suffixes``.

    Sorted iteration keeps ordering deterministic so assertion diffs
    name the offending entries cleanly.
    """
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not path.name.endswith(suffixes):
            continue
        rel = str(path.relative_to(root))
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _build_in_two_checkouts(
    sample_dir: pathlib.Path,
    tmp_root: pathlib.Path,
    backend_name: str,
    main_basename: str,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Copy *sample_dir* into two distinct per-user workspaces under
    *tmp_root*, build with *backend_name* in each, return both
    workspace roots so callers can hash whichever CAS sub-tree they
    care about.

    Each workspace gets its own ``.git`` marker so
    :func:`compiletools.git_utils.find_git_root` resolves to that
    workspace (not the surrounding pytest tmpdir or the test runner's
    cwd). All four CAS layers live inside the per-user workspace so
    inputs / outputs are isolated and the byte-identity assertion
    compares like-for-like.
    """
    workspaces: list[pathlib.Path] = []
    for user in ("alice", "bob"):
        workspace = tmp_root / f"home-{user}" / "proj"
        workspace.mkdir(parents=True)
        for entry in sample_dir.iterdir():
            if entry.is_file():
                shutil.copy2(entry, workspace)
            else:
                shutil.copytree(entry, workspace / entry.name)
        # Marker so find_git_root() finds the per-user workspace via
        # the fallback walker (without invoking real `git rev-parse`).
        (workspace / ".git").mkdir()

        argv = [
            "ct-cake",
            "--auto",
            f"--backend={backend_name}",
            f"--cas-objdir={workspace}/cas-objdir",
            f"--bindir={workspace}/bin",
            f"--cas-pchdir={workspace}/cas-pchdir",
            f"--cas-pcmdir={workspace}/cas-pcmdir",
            f"--cas-exedir={workspace}/cas-exedir",
            str(workspace / main_basename),
        ]
        # Strip user CXXFLAGS / CFLAGS / LDFLAGS so the host's environment
        # can't smuggle paths or override the injected prefix-map.
        env = os.environ.copy()
        for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
            env.pop(var, None)
        result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True)
        assert result.returncode == 0, (
            f"ct-cake failed in {workspace} (backend={backend_name}):\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        workspaces.append(workspace)
    return workspaces[0], workspaces[1]


@uth.requires_functional_compiler
def test_two_checkouts_produce_byte_identical_cas_objdir_make(tmp_path):
    """Build the simple sample under two distinct workspace paths with the
    make backend; assert every .o under cas-objdir is byte-identical
    across the two checkouts."""
    sample = pathlib.Path(uth.samplesdir()) / "simple"
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    alice, bob = _build_in_two_checkouts(
        sample_dir=sample,
        tmp_root=tmp_path,
        backend_name="make",
        main_basename="helloworld_cpp.cpp",
    )
    alice_hashes = _hash_tree(alice / "cas-objdir", suffixes=(".o",))
    bob_hashes = _hash_tree(bob / "cas-objdir", suffixes=(".o",))
    assert alice_hashes, f"no .o files found under {alice / 'cas-objdir'}"
    assert alice_hashes == bob_hashes, (
        f"cas-objdir byte-mismatch across two checkout paths.\n"
        f"alice ({alice}): {alice_hashes}\n"
        f"bob   ({bob}):   {bob_hashes}\n"
        f"diff: {set(alice_hashes.items()) ^ set(bob_hashes.items())}"
    )
