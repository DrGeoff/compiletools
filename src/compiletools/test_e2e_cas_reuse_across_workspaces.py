"""Layer 5 end-to-end smoke tests: cas-objdir / cas-exedir reuse across
workspace moves and across rebuilds in the same workspace.

Two axes are exercised:

* **Filename stability across workspace paths** — the same sample built
  in ``ws1`` and ``ws2`` produces byte-identical CAS filenames in each
  workspace's cas-objdir / cas-exedir. Catches any regression where a
  cache key leaks an absolute workspace path.
* **Mtime-defeats-CAS bug** — bumping every source's mtime to "the
  future" must NOT trigger a rebuild when ``--use-mtime=False``
  (default). Pre-fix, make/ninja would re-fire every recipe because
  ``source.mtime > cached.mtime``; post-fix, the CAS path's existence
  is the sole signal.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
and ``compiletools-cas-mtime-bug-report.md`` in the repo root.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import compiletools.testhelper as uth

# Skip the whole module if either (a) the worktree's venv doesn't
# match this src tree (the e2e tests would silently exercise the wrong
# compiletools install) or (b) ct-cake itself isn't on PATH. The
# venv-mismatch message is the actionable one — it tells the user to
# re-run ``uv pip install -e .`` from this worktree.
pytestmark = uth.skipif_e2e_unavailable(
    lambda: shutil.which("ct-cake") is not None,
    "ct-cake not on PATH; run `uv pip install -e .` in this worktree",
)


def _e2e_env() -> dict[str, str]:
    """Stripped env for ``subprocess.run``.

    PATH is preserved so ct-cake and the compiler resolve normally.
    Variant/compiler env vars are forwarded so the test honours the
    user's ``VARIANT`` / ``CXX`` choices, but no shell config is
    sourced — that would silently shift the build between subprocess
    invocations within the same test.

    LD_PRELOAD is preserved for Termux: ``libtermux-exec.so`` is
    required to ``exec`` binaries on Android; without it the compiler
    subprocess fails with EACCES. Harmless on other platforms (the
    var is typically unset).
    """
    env = {"PATH": os.environ.get("PATH", "")}
    for k in ("CXX", "CC", "CPP", "VARIANT", "HOME", "LD_LIBRARY_PATH", "LD_PRELOAD", "TMPDIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _run_ct_cake(workdir, *extra_args, timeout=180) -> subprocess.CompletedProcess:
    """Invoke ``ct-cake --auto`` in ``workdir`` with the e2e env."""
    cmd = ["ct-cake", *extra_args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(workdir),
        timeout=timeout,
        env=_e2e_env(),
    )


def _assert_build_ok(result: subprocess.CompletedProcess, workdir) -> None:
    assert result.returncode == 0, (
        f"ct-cake --auto failed in {workdir}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@uth.requires_functional_compiler
def test_object_cache_filenames_match_across_workspace_paths(tmp_path):
    """Same sample compiled in two workspace dirs produces identical
    object filenames in each workspace's cas-objdir/.

    Asserts the canonicalizer is doing its job at the macro_state_hash
    component of the object filename: identical TUs share cache entries
    even when the workspace itself lives at a different absolute path.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "factory"
    ws2 = tmp_path / "ws2" / "factory"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)

    _assert_build_ok(_run_ct_cake(ws1), ws1)
    _assert_build_ok(_run_ct_cake(ws2), ws2)

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


@uth.requires_functional_compiler
def test_compile_and_link_skipped_on_rerun_when_sources_touched(tmp_path):
    """The original mtime bug: ``cas-objdir`` keys were stable across
    workspaces, but make/ninja still re-fired every compile recipe
    because ``source.mtime > cached_obj.mtime`` after a fresh checkout.

    This test would have caught the bug pre-fix:

    1. Build a sample once. Record mtimes of every cached ``.o``,
       cas-exe ``.exe``, ``.a`` and ``.so``.
    2. ``os.utime`` every source / header to NOW + 1 hour — guaranteed
       newer than every cached artefact.
    3. Build again in the same workspace.
    4. Assert no cached-artefact mtime changed (no producer rule fired).

    Pre-fix behaviour: source mtime > target mtime → make/ninja fires
    the producer recipe, which reproduces the byte-identical artefact
    and advances the artefact's mtime. This test would fail.
    Post-fix behaviour: with ``--use-mtime`` defaulting to False,
    make/ninja drops normal prereqs from compile, link, ar, and
    link-shared rules, so the cached artefact's existence is sufficient
    and the mtime is untouched.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws = tmp_path / "ws" / "factory"
    shutil.copytree(sample_src, ws)

    def _build():
        _assert_build_ok(_run_ct_cake(ws), ws)

    def _artefact_mtimes() -> dict[str, float]:
        out: dict[str, float] = {}
        for cache_root in (ws / "cas-objdir", ws / "cas-exedir"):
            if not cache_root.is_dir():
                continue
            for p in cache_root.rglob("*"):
                if p.is_file() and p.suffix in (".o", ".exe", ".a", ".so"):
                    out[str(p)] = p.stat().st_mtime
        return out

    _build()
    before = _artefact_mtimes()
    assert before, "first build produced no cached artefacts in cas-objdir/cas-exedir"

    # Bump every source / header in the workspace to "the future" so
    # mtime-based prereq comparison would force a rebuild.
    future = max(before.values()) + 3600.0
    for p in ws.rglob("*"):
        if p.is_file() and p.suffix in (".cpp", ".cc", ".c", ".h", ".hpp", ".hxx", ".hh"):
            os.utime(p, (future, future))

    _build()
    after = _artefact_mtimes()

    assert set(before.keys()) == set(after.keys()), (
        f"second build produced different artefact set:\n"
        f"  only in first:  {sorted(set(before) - set(after))}\n"
        f"  only in second: {sorted(set(after) - set(before))}"
    )
    changed = {p: (before[p], after[p]) for p in before if after[p] != before[p]}
    assert not changed, (
        f"second build re-executed {len(changed)} producer recipe(s) despite "
        f"CAS-stable artefact paths (mtime regressed to mtime-based rebuild). "
        f"Sample:\n  " + "\n  ".join(f"{p}: {b} -> {a}" for p, (b, a) in list(changed.items())[:5])
    )


@uth.requires_functional_compiler
def test_use_mtime_true_restores_legacy_rebuild_on_source_touch(tmp_path):
    """Smoke test the ``--use-mtime`` legacy path: when the user opts back
    in, bumping a source's mtime DOES retrigger the producer rules (the
    behaviour required for interactive editor workflows where re-saving
    a file should cause a rebuild even if the content didn't change).

    Without this test, a future refactor could silently render
    ``--use-mtime=True`` a no-op and we'd never know.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws = tmp_path / "ws" / "factory"
    shutil.copytree(sample_src, ws)

    def _build():
        _assert_build_ok(_run_ct_cake(ws, "--use-mtime"), ws)

    def _object_mtimes() -> dict[str, float]:
        cache_root = ws / "cas-objdir"
        if not cache_root.is_dir():
            return {}
        return {str(p): p.stat().st_mtime for p in cache_root.rglob("*.o") if p.is_file()}

    _build()
    before = _object_mtimes()
    assert before, "first build produced no objects"

    future = max(before.values()) + 3600.0
    for p in ws.rglob("*"):
        if p.is_file() and p.suffix in (".cpp", ".cc", ".c", ".h", ".hpp", ".hxx", ".hh"):
            os.utime(p, (future, future))

    _build()
    after = _object_mtimes()

    # In legacy mtime mode, at least one object's mtime MUST have advanced
    # because every prerequisite source was touched to "the future".
    rebuilt = {p for p in before if p in after and after[p] != before[p]}
    assert rebuilt, (
        "ct-cake --use-mtime did not retrigger any compile rule after touching "
        "every source — legacy mtime semantics are broken (--use-mtime is a no-op)."
    )


@uth.requires_functional_compiler
def test_link_artefact_reused_across_workspaces(tmp_path):
    """Cas-exe regression guard: build the same sample at workspace A
    and workspace B sharing a single ``cas-exedir`` root. The second
    build must reuse the cached executable (same filename, same
    inode) instead of relinking.

    Inode equality is the strong assertion here: matching mtime alone
    can be fooled by a fast rebuild that produces the same second-
    granularity timestamp; same inode proves the second build did
    NOT do a temp+rename publish, only a hard-link reuse.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "factory"
    ws2 = tmp_path / "ws2" / "factory"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-exedir={shared_cas_exedir}"), workdir)

    def _exe_stats() -> dict[str, tuple[int, float]]:
        return {p.name: (p.stat().st_ino, p.stat().st_mtime) for p in shared_cas_exedir.rglob("*.exe") if p.is_file()}

    _build(ws1)
    after_first = _exe_stats()
    assert after_first, f"first build produced no .exe in {shared_cas_exedir}"

    _build(ws2)
    after_second = _exe_stats()

    only_first = set(after_first) - set(after_second)
    only_second = set(after_second) - set(after_first)
    assert not only_first and not only_second, (
        "cas-exe filenames differ across workspaces — link key is path-bound:\n"
        f"  only in ws1: {sorted(only_first)}\n  only in ws2: {sorted(only_second)}"
    )

    # Same inode after the second build proves the link rule did not
    # re-fire (which would temp+rename to a fresh inode). Mtime is the
    # weaker consistency check — a fast rebuild can land in the same
    # whole-second slot — but it's a useful additional signal.
    swapped_inode = {n for n in after_first if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"second build re-linked {len(swapped_inode)} cached executable(s) "
        f"(inode swap proves a fresh temp+rename happened): {sorted(swapped_inode)}"
    )
    advanced_mtime = {n for n in after_first if after_second[n][1] != after_first[n][1]}
    assert not advanced_mtime, (
        f"second build advanced the mtime on {len(advanced_mtime)} cached "
        f"executable(s) without inode swap — unexpected: {sorted(advanced_mtime)}"
    )


@uth.requires_functional_compiler
def test_pch_artefact_reused_across_workspaces(tmp_path):
    """Cas-pchdir regression guard for the bug-report scenario where a
    persistent shared ``--cas-pchdir`` accumulates one duplicate
    ``cmd_hash`` directory per CI working-directory prefix because the
    PCH cache key embedded the absolute header path.

    Reproducer mirrors the report's `cp -a workspace1 workspace2`
    sketch: build the PCH-using ``examples-end-to-end/pch`` project in ws1 and ws2
    (distinct absolute paths) against a single shared ``cas-pchdir``;
    after the second build the shared dir must hold exactly one
    ``cmd_hash`` directory (not two), and the same .gch inode must be
    reused (proving the second build hit the cache rather than
    re-precompiling and racing for the same path).
    """
    sample_src = uth.example_path("pch")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "pch"
    ws2 = tmp_path / "ws2" / "pch"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)
    shared_cas_pchdir = tmp_path / "shared-cas-pchdir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-pchdir={shared_cas_pchdir}"), workdir)

    def _gch_stats() -> dict[str, tuple[int, float]]:
        return {p.name: (p.stat().st_ino, p.stat().st_mtime) for p in shared_cas_pchdir.rglob("*.gch") if p.is_file()}

    def _cmd_hash_dirs() -> set[str]:
        # Layout: cas-pchdir/[<variant>/]<cmd_hash>/<header>.gch
        # Walk to the .gch and take its parent dir's name as the cmd_hash.
        return {p.parent.name for p in shared_cas_pchdir.rglob("*.gch") if p.is_file()}

    _build(ws1)
    after_first_gch = _gch_stats()
    after_first_dirs = _cmd_hash_dirs()
    assert after_first_gch, f"first build produced no .gch in {shared_cas_pchdir}"
    assert len(after_first_dirs) == 1, f"first build produced unexpected cmd_hash count: {sorted(after_first_dirs)}"

    _build(ws2)
    after_second_gch = _gch_stats()
    after_second_dirs = _cmd_hash_dirs()

    # Single cmd_hash dir after both workspaces have built — the core
    # symptom from the bug report. Pre-fix this would be 2 dirs (one per
    # workspace path) holding bit-identical .gch content.
    extra_dirs = after_second_dirs - after_first_dirs
    assert not extra_dirs and after_second_dirs == after_first_dirs, (
        "second workspace produced a NEW PCH cmd_hash directory — cache key is path-bound:\n"
        f"  after ws1: {sorted(after_first_dirs)}\n"
        f"  after ws2: {sorted(after_second_dirs)}\n"
        f"  extra dirs introduced by ws2 build: {sorted(extra_dirs)}"
    )

    # Inode equality on the cached .gch proves ws2 reused the file
    # rather than re-precompiling and atomic-rename'ing into the slot
    # (which would produce a fresh inode at the same path).
    only_first = set(after_first_gch) - set(after_second_gch)
    only_second = set(after_second_gch) - set(after_first_gch)
    assert not only_first and not only_second, (
        "PCH .gch filenames differ across workspaces — header path leaked into key:\n"
        f"  only in ws1: {sorted(only_first)}\n  only in ws2: {sorted(only_second)}"
    )
    swapped_inode = {n for n in after_first_gch if after_second_gch[n][0] != after_first_gch[n][0]}
    assert not swapped_inode, (
        f"second build re-precompiled {len(swapped_inode)} cached PCH file(s) "
        f"(inode swap proves the producer rule re-fired): {sorted(swapped_inode)}"
    )


def _build_static_lib_sample(workdir, lib_source_name: str) -> None:
    """Create a 2-file sample workspace that asks ct-cake to produce a
    static library: a header, a body that defines a single function,
    and a short ``ct.conf.d/ct.conf`` listing the body as ``static``.
    Returns nothing; caller invokes ct-cake from ``workdir``.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "ct.conf.d").mkdir(exist_ok=True)
    (workdir / "ct.conf.d" / "ct.conf").write_text(f"static = {lib_source_name}\nvariant = blank\n")
    stem = os.path.splitext(lib_source_name)[0]
    (workdir / f"{stem}.hpp").write_text(f"#pragma once\nint {stem}_value();\n")
    (workdir / lib_source_name).write_text(f'#include "{stem}.hpp"\nint {stem}_value() {{ return 42; }}\n')


@uth.requires_functional_compiler
def test_static_library_reused_across_workspaces(tmp_path):
    """Static-library cache regression guard: same .a built in ws1 and
    ws2 (sharing one cas-exedir) must reuse the cached archive.
    """
    ws1 = tmp_path / "ws1" / "lib"
    ws2 = tmp_path / "ws2" / "lib"
    _build_static_lib_sample(ws1, "mylib.cpp")
    _build_static_lib_sample(ws2, "mylib.cpp")
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-exedir={shared_cas_exedir}"), workdir)

    def _lib_stats(suffix: str) -> dict[str, tuple[int, float]]:
        return {
            p.name: (p.stat().st_ino, p.stat().st_mtime) for p in shared_cas_exedir.rglob(f"*{suffix}") if p.is_file()
        }

    _build(ws1)
    after_first = _lib_stats(".a")
    assert after_first, f"first build produced no .a in {shared_cas_exedir}"

    _build(ws2)
    after_second = _lib_stats(".a")

    only_first = set(after_first) - set(after_second)
    only_second = set(after_second) - set(after_first)
    assert not only_first and not only_second, (
        "cas-static-library filenames differ across workspaces — lib key is path-bound:\n"
        f"  only in ws1: {sorted(only_first)}\n  only in ws2: {sorted(only_second)}"
    )

    swapped_inode = {n for n in after_first if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"second build re-archived {len(swapped_inode)} cached static lib(s) (inode swap): {sorted(swapped_inode)}"
    )


def _build_shared_lib_sample(workdir, lib_source_name: str) -> None:
    """Same shape as ``_build_static_lib_sample`` but configures a
    shared library instead. Adds ``-fPIC`` to the bundled ct.conf so
    the object can be linked into a ``.so`` on platforms where PIC is
    not the default for static-library compilation.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "ct.conf.d").mkdir(exist_ok=True)
    (workdir / "ct.conf.d" / "ct.conf").write_text(f"dynamic = {lib_source_name}\nvariant = blank\nCPPFLAGS = -fPIC\n")
    stem = os.path.splitext(lib_source_name)[0]
    (workdir / f"{stem}.hpp").write_text(f"#pragma once\nint {stem}_value();\n")
    (workdir / lib_source_name).write_text(f'#include "{stem}.hpp"\nint {stem}_value() {{ return 42; }}\n')


@uth.requires_functional_compiler
def test_bundle_with_workspace_relative_wrapper_reuses_cache(tmp_path):
    """Integration: bundle variant + workspace-relative compiler wrapper +
    two workspaces -> identical object filenames in cas-objdir.

    This is the missing integration test the audit flagged: upstream's
    `test_compiler_identity_acceptance` proves cache-key sites canonicalise
    given a SimpleNamespace(CXX=...), and `test_configutils` proves the
    resolver chains bundles correctly. This test stitches both:

      1. A multi-axis bundle resolves correctly through configargparse +
         _commonsubstitutions.
      2. The workspace-relative wrapper at --CXX=./tools/wrap.sh in each
         workspace is canonicalised against that workspace's gitroot.
      3. Object filenames in `cas-objdir/<variant>/` match across the
         two workspaces, proving the path-bound leak is closed for the
         realistic bundle-plus-wrapper case.

    Uses --variant=gcc,cxx17,debug rather than a heavyweight bundle so
    the test doesn't fail on minor compile-time issues (asan runtime
    missing, hardened flags requiring fortify-source headers, etc.) —
    the multi-axis composition path is what matters here, not the
    specific axes' runtime semantics.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "factory"
    ws2 = tmp_path / "ws2" / "factory"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)

    # Workspace-relative wrapper script in each. Identical bytes, pinned
    # mtime so the (size, mtime_ns) segment of compiler_identity matches
    # — leaving only the path component, which is what compiler_identity
    # canonicalisation is supposed to neutralise.
    wrapper_content = '#!/bin/sh\nexec g++ "$@"\n'
    FIXED_TIME = (1700000000, 1700000000)
    for ws in (ws1, ws2):
        tools_dir = ws / "tools"
        tools_dir.mkdir()
        wrap = tools_dir / "wrap.sh"
        wrap.write_text(wrapper_content)
        wrap.chmod(0o755)
        os.utime(wrap, FIXED_TIME)

    def _run(workdir):
        return _run_ct_cake(
            workdir,
            "--variant=gcc,cxx17,debug",  # multi-axis composition
            "--CXX=./tools/wrap.sh",  # workspace-relative wrapper
            "--CC=./tools/wrap.sh",
            "--LD=./tools/wrap.sh",
        )

    _assert_build_ok(_run(ws1), ws1)
    _assert_build_ok(_run(ws2), ws2)

    def _object_filenames(workdir) -> set[str]:
        cas = workdir / "cas-objdir"
        assert cas.is_dir(), f"cas-objdir not produced under {workdir}"
        return {p.name for p in cas.rglob("*.o")}

    objs_ws1 = _object_filenames(ws1)
    objs_ws2 = _object_filenames(ws2)

    assert objs_ws1, f"no object files produced under {ws1}/cas-objdir"
    assert objs_ws2, f"no object files produced under {ws2}/cas-objdir"

    only_in_ws1 = objs_ws1 - objs_ws2
    only_in_ws2 = objs_ws2 - objs_ws1
    assert not only_in_ws1 and not only_in_ws2, (
        "object filenames differ across workspaces for bundle+wrapper case "
        "(cache key leaks the workspace prefix):\n"
        f"  only in ws1: {sorted(only_in_ws1)}\n"
        f"  only in ws2: {sorted(only_in_ws2)}"
    )

    # And confirm the multi-axis variant produced the expected canonical
    # subdir name (gcc.cxx17.debug) under cas-objdir — not some
    # un-canonicalised typing of the user's input.
    for ws in (ws1, ws2):
        variant_dirs = [p.name for p in (ws / "cas-objdir").iterdir() if p.is_dir()]
        assert variant_dirs == ["gcc.cxx17.debug"], (
            f"expected single cas-objdir subdir 'gcc.cxx17.debug' in {ws}, got {variant_dirs}"
        )


@uth.requires_functional_compiler
def test_shared_library_reused_across_workspaces(tmp_path):
    """Shared-library cache regression guard: same .so built in ws1 and
    ws2 (sharing one cas-exedir) must reuse the cached library.
    """
    ws1 = tmp_path / "ws1" / "lib"
    ws2 = tmp_path / "ws2" / "lib"
    _build_shared_lib_sample(ws1, "mylib.cpp")
    _build_shared_lib_sample(ws2, "mylib.cpp")
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-exedir={shared_cas_exedir}"), workdir)

    def _lib_stats(suffix: str) -> dict[str, tuple[int, float]]:
        return {
            p.name: (p.stat().st_ino, p.stat().st_mtime) for p in shared_cas_exedir.rglob(f"*{suffix}") if p.is_file()
        }

    _build(ws1)
    after_first = _lib_stats(".so")
    assert after_first, f"first build produced no .so in {shared_cas_exedir}"

    _build(ws2)
    after_second = _lib_stats(".so")

    only_first = set(after_first) - set(after_second)
    only_second = set(after_second) - set(after_first)
    assert not only_first and not only_second, (
        "cas-shared-library filenames differ across workspaces — lib key is path-bound:\n"
        f"  only in ws1: {sorted(only_first)}\n  only in ws2: {sorted(only_second)}"
    )

    swapped_inode = {n for n in after_first if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"second build re-linked {len(swapped_inode)} cached shared lib(s) (inode swap): {sorted(swapped_inode)}"
    )
