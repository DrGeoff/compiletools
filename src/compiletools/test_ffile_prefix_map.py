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
from compiletools.build_backend import available_backends, ensure_backends_registered

# Trigger @register_backend across all backend modules so available_backends()
# returns the full list at parametrization time.
ensure_backends_registered()


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
    suffix matches, and assert their CONTENT SETS match.

    Compares the set of distinct content-hashes rather than the
    filename → hash mapping because some backends (notably slurm)
    sidecar a per-invocation copy of the artefact under a hash-
    suffixed name; alice's ``foo_<hashA>.exe`` and bob's
    ``foo_<hashB>.exe`` carry byte-identical content but the
    filename hash differs per run. Set comparison sees them as
    equivalent; mapping comparison would falsely fail.

    Empty hash sets on both sides are treated as a "this sample
    doesn't exercise this CAS layer" skip.
    """
    alice_hashes = _hash_tree(alice / cas_layer, suffixes=suffixes)
    bob_hashes = _hash_tree(bob / cas_layer, suffixes=suffixes)
    if not alice_hashes and not bob_hashes:
        pytest.skip(f"sample doesn't populate {cas_layer} with {suffixes}")
    alice_content = set(alice_hashes.values())
    bob_content = set(bob_hashes.values())
    assert alice_content == bob_content, (
        f"{cas_layer} content-set mismatch across two checkout paths.\n"
        f"alice ({alice}): {alice_hashes}\n"
        f"bob   ({bob}):   {bob_hashes}\n"
        f"alice-only content: {alice_content - bob_content}\n"
        f"bob-only content:   {bob_content - alice_content}"
    )


@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_cas_objdir(backend_name, tmp_path):
    """Build the simple sample under two distinct workspace paths with
    every available backend; assert every .o under cas-objdir is
    byte-identical across the two checkouts."""
    sample = pathlib.Path(uth.example_path("simple"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_in_two_checkouts(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="helloworld_cpp.cpp",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-objdir", suffixes=(".o",))


@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_cas_exedir(backend_name, tmp_path):
    """Linker artefact byte-identity across two checkouts. Bazel and
    cmake have their own native CAS layers (``_has_native_cas_exe``
    returns True), so they don't populate cas-exedir at all -- the
    ``_assert_cas_layer_byte_identical`` helper skips when both sides
    are empty."""
    sample = pathlib.Path(uth.example_path("simple"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_in_two_checkouts(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="helloworld_cpp.cpp",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-exedir", suffixes=(".exe", ".a", ".so"))


_PCH_BMI_BYTES_DIVERGE_NOTE = (
    "gcc's PCH (.gch) and BMI (.pcm/.gcm) on-disk layout depends on the "
    "build-cwd path LENGTH (empirically: same-length cwds yield identical "
    "bytes; differing lengths shift internal pointer offsets). This is NOT "
    "controllable via -ffile-prefix-map, -fdebug-compilation-dir, "
    "PWD=/proc/self/cwd, or any other compile-time flag we tested. Two "
    "users at distinct workspace paths therefore produce different .gch / "
    ".pcm bytes for the same logical compile -- but the cache key is the "
    "same, and consumption produces byte-identical .o (see "
    "test_two_checkouts_produce_byte_identical_o_with_shared_cas_pchdir, "
    "the test that asserts what cross-user CAS sharing actually requires). "
    "The negative-control test below pins the empirical divergence so any "
    "future change that accidentally achieves byte-identity is noticed."
)


def _build_with_shared_pchdir(
    sample_dir: pathlib.Path,
    tmp_root: pathlib.Path,
    backend_name: str,
    main_basename: str,
    shared_cas_layer: str,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Two per-user workspaces, but a SINGLE shared cas-{pchdir,pcmdir}.

    Models the cross-user NFS-shared cache scenario users actually care
    about: alice runs first and populates the shared cache, bob runs second
    and must hit it. Other CAS layers stay per-user (so bob's .o lands in
    his own cas-objdir and we can compare against alice's independently).
    """
    shared_cache = tmp_root / shared_cas_layer
    shared_cache.mkdir()
    workspaces: list[pathlib.Path] = []
    for user in ("alice", "bob"):
        workspace = tmp_root / f"home-{user}" / "proj"
        workspace.mkdir(parents=True)
        for entry in sample_dir.iterdir():
            if entry.is_file():
                shutil.copy2(entry, workspace)
            else:
                shutil.copytree(entry, workspace / entry.name)
        (workspace / ".git").mkdir()

        argv = [
            "ct-cake",
            "--auto",
            f"--backend={backend_name}",
            f"--cas-objdir={workspace}/cas-objdir",
            f"--bindir={workspace}/bin",
            f"--cas-pchdir={shared_cache if shared_cas_layer == 'shared-pchdir' else workspace / 'cas-pchdir'}",
            f"--cas-pcmdir={shared_cache if shared_cas_layer == 'shared-pcmdir' else workspace / 'cas-pcmdir'}",
            f"--cas-exedir={workspace}/cas-exedir",
            str(workspace / main_basename),
        ]
        env = os.environ.copy()
        for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
            env.pop(var, None)
        result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True)
        assert result.returncode == 0, (
            f"ct-cake failed in {workspace} (backend={backend_name}, shared={shared_cas_layer}):\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        workspaces.append(workspace)
    return workspaces[0], workspaces[1]


@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_o_with_shared_cas_pchdir(backend_name, tmp_path):
    """Cross-user PCH cache sharing works: alice and bob use a single shared
    cas-pchdir; each user's resulting .o files are byte-identical.

    This is the property that actually matters for NFS-shared caches. The
    .gch bytes themselves DIFFER (gcc PCH layout is cwd-length-sensitive --
    see _PCH_BMI_BYTES_DIVERGE_NOTE), but the consumed-into-.o pathway is
    byte-identical when the PCH precompile rule uses workspace-relative
    source paths + cwd=anchor_root discipline (see _create_pch_rules in
    build_backend.py).
    """
    sample = pathlib.Path(uth.example_path("pch"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_with_shared_pchdir(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="pch_user.cpp",
            shared_cas_layer="shared-pchdir",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-objdir", suffixes=(".o",))


@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_o_with_shared_cas_pcmdir(backend_name, tmp_path):
    """Cross-user PCM/BMI cache sharing works: alice and bob share one
    cas-pcmdir; each user's resulting .o files are byte-identical.

    Mirror of the cas-pchdir test for C++20 modules. Same rationale:
    .pcm/.gcm bytes themselves diverge across users (gcc/clang BMI layout
    is cwd-sensitive), but consumed-into-.o is stable.
    """
    from compiletools.test_cxx_modules import (
        _clang_supports_header_units,
        _detected_gcc_supports_modules,
        _gcc_supports_header_units,
    )

    sample = pathlib.Path(uth.example_path("cxx_modules_header_units"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    gcc_ok = (
        cxx and "g++" in os.path.basename(cxx) and _detected_gcc_supports_modules() and _gcc_supports_header_units()
    )
    if not gcc_ok and not _clang_supports_header_units():
        pytest.skip("No compiler on PATH supports C++20 header units")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_with_shared_pchdir(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="main.cpp",
            shared_cas_layer="shared-pcmdir",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-objdir", suffixes=(".o",))


@pytest.mark.xfail(strict=False, reason=_PCH_BMI_BYTES_DIVERGE_NOTE)
@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_cas_pchdir(backend_name, tmp_path):
    """Negative control: documents that .gch bytes legitimately differ
    across two-checkout builds. Kept xfailed so a future change that
    accidentally achieves byte-identity is noticed (and the marker can be
    dropped). The cross-user cache-sharing property is asserted by
    test_two_checkouts_produce_byte_identical_o_with_shared_cas_pchdir."""
    sample = pathlib.Path(uth.example_path("pch"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_in_two_checkouts(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="pch_user.cpp",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-pchdir", suffixes=(".gch",))


@pytest.mark.xfail(strict=False, reason=_PCH_BMI_BYTES_DIVERGE_NOTE)
@uth.requires_backend_tool()
@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", available_backends())
def test_two_checkouts_produce_byte_identical_cas_pcmdir(backend_name, tmp_path):
    """Negative control mirror for .pcm/.gcm. See companion docstring
    above; the functional cross-user property is asserted by
    test_two_checkouts_produce_byte_identical_o_with_shared_cas_pcmdir."""
    from compiletools.test_cxx_modules import (
        _clang_supports_header_units,
        _detected_gcc_supports_modules,
        _gcc_supports_header_units,
    )

    sample = pathlib.Path(uth.example_path("cxx_modules_header_units"))
    if not sample.is_dir():
        pytest.skip(f"missing sample dir: {sample}")

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    gcc_ok = (
        cxx and "g++" in os.path.basename(cxx) and _detected_gcc_supports_modules() and _gcc_supports_header_units()
    )
    if not gcc_ok and not _clang_supports_header_units():
        pytest.skip("No compiler on PATH supports C++20 header units")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        alice, bob = _build_in_two_checkouts(
            sample_dir=sample,
            tmp_root=pathlib.Path(effective_tmp),
            backend_name=backend_name,
            main_basename="main.cpp",
        )
        _assert_cas_layer_byte_identical(alice, bob, "cas-pcmdir", suffixes=(".pcm", ".gcm"))
