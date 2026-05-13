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


def _assert_cas_layer_byte_identical(
    alice: pathlib.Path,
    bob: pathlib.Path,
    cas_layer: str,
    suffixes: tuple[str, ...],
) -> None:
    """Walk the two per-user CAS layers, hash every artefact whose
    suffix matches, and assert equality. Empty hash dicts on both
    sides are treated as a "this sample doesn't exercise this CAS
    layer" skip — only flag when one side has artefacts and the
    other doesn't, or when contents differ.
    """
    alice_hashes = _hash_tree(alice / cas_layer, suffixes=suffixes)
    bob_hashes = _hash_tree(bob / cas_layer, suffixes=suffixes)
    if not alice_hashes and not bob_hashes:
        pytest.skip(f"sample doesn't populate {cas_layer} with {suffixes}")
    assert alice_hashes == bob_hashes, (
        f"{cas_layer} byte-mismatch across two checkout paths.\n"
        f"alice ({alice}): {alice_hashes}\n"
        f"bob   ({bob}):   {bob_hashes}\n"
        f"diff: {set(alice_hashes.items()) ^ set(bob_hashes.items())}"
    )


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
    _assert_cas_layer_byte_identical(alice, bob, "cas-objdir", suffixes=(".o",))


@uth.requires_functional_compiler
def test_two_checkouts_produce_byte_identical_cas_exedir_make(tmp_path):
    """Linker artefact byte-identity across two checkouts. The simple
    sample produces an executable in cas-exedir; the link command's
    canonicalize_for_command pass must rewrite any workspace-rooted
    -L / -Wl,-rpath / -Wl,--version-script= so the embedded RPATH
    bytes match across users."""
    sample = pathlib.Path(uth.samplesdir()) / "simple"
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    alice, bob = _build_in_two_checkouts(
        sample_dir=sample,
        tmp_root=tmp_path,
        backend_name="make",
        main_basename="helloworld_cpp.cpp",
    )
    _assert_cas_layer_byte_identical(alice, bob, "cas-exedir", suffixes=(".exe", ".a", ".so"))


_PCH_BMI_XFAIL_REASON = (
    "Known Round 3 limitation: gcc embeds the absolute source path in PCH "
    "(.gch) and BMI (.pcm/.gcm) artefacts via its internal path-table, "
    "which is NOT subject to -ffile-prefix-map. Closing this gap requires "
    "either workspace-relative source paths in the precompile rule emitter "
    "(plus per-backend CWD discipline) or a PWD=/proc/self/cwd subprocess-env "
    "trick. -fdebug-compilation-dir= would address it for clang but is "
    "rejected by gcc as of 16.1.0. Tracked under Round 3 'Open Questions' "
    "in docs/superpowers/specs/2026-05-12-round3-workspace-relative-compile-paths-design.md. "
    "strict=False so a future fix that closes the gap doesn't fail the suite "
    "until someone gets around to dropping the marker."
)


@pytest.mark.xfail(strict=False, reason=_PCH_BMI_XFAIL_REASON)
@uth.requires_functional_compiler
def test_two_checkouts_produce_byte_identical_cas_pchdir_make(tmp_path):
    """PCH .gch byte-identity across two checkouts. The pch sample uses
    `//#PCH=stdafx.h` magic so a precompiled header lands in cas-pchdir."""
    sample = pathlib.Path(uth.samplesdir()) / "pch"
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    alice, bob = _build_in_two_checkouts(
        sample_dir=sample,
        tmp_root=tmp_path,
        backend_name="make",
        main_basename="pch_user.cpp",
    )
    _assert_cas_layer_byte_identical(alice, bob, "cas-pchdir", suffixes=(".gch",))


@pytest.mark.xfail(strict=False, reason=_PCH_BMI_XFAIL_REASON)
@uth.requires_functional_compiler
def test_two_checkouts_produce_byte_identical_cas_pcmdir_make(tmp_path):
    """C++20 module BMI byte-identity across two checkouts. The
    cxx_modules_header_units sample exercises gcc/clang header units
    so .gcm / .pcm files land in cas-pcmdir."""
    from compiletools.test_cxx_modules import (
        _clang_supports_header_units,
        _detected_gcc_supports_modules,
        _gcc_supports_header_units,
    )

    sample = pathlib.Path(uth.samplesdir()) / "cxx_modules_header_units"
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    gcc_ok = (
        cxx and "g++" in os.path.basename(cxx) and _detected_gcc_supports_modules() and _gcc_supports_header_units()
    )
    if not gcc_ok and not _clang_supports_header_units():
        pytest.skip("No compiler on PATH supports C++20 header units")

    alice, bob = _build_in_two_checkouts(
        sample_dir=sample,
        tmp_root=tmp_path,
        backend_name="make",
        main_basename="main.cpp",
    )
    _assert_cas_layer_byte_identical(alice, bob, "cas-pcmdir", suffixes=(".pcm", ".gcm"))
