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

import json
import os
import shutil
import subprocess
import time

import pytest

import compiletools.apptools
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


def _cache_stats(cas_dir, suffix: str) -> dict[str, tuple[int, float]]:
    """Stat every ``*<suffix>`` file under *cas_dir*, keyed by basename.

    Returns ``{name: (st_ino, st_mtime)}`` so cross-workspace cache-reuse
    tests can compare cached-artefact identity. Inode equality proves
    the second build reused the file (hard-link) rather than temp+rename
    republishing into the same slot.
    """
    return {p.name: (p.stat().st_ino, p.stat().st_mtime) for p in cas_dir.rglob(f"*{suffix}") if p.is_file()}


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

    1. Build a sample once. Record the identity of every cached ``.o``,
       cas-exe ``.exe``, ``.a`` and ``.so``.
    2. ``os.utime`` every source / header to NOW + 1 hour — guaranteed
       newer than every cached artefact.
    3. Build again in the same workspace.
    4. Assert no cached artefact was rewritten (no producer rule fired).

    Pre-fix behaviour: source mtime > target mtime → make/ninja fires
    the producer recipe, which reproduces the byte-identical artefact.
    This test would fail.
    Post-fix behaviour: with ``--use-mtime`` defaulting to False,
    make/ninja drops normal prereqs from compile, link, ar, and
    link-shared rules, so the cached artefact's existence is sufficient
    and the artefact is left alone.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws = tmp_path / "ws" / "factory"
    shutil.copytree(sample_src, ws)

    def _build():
        _assert_build_ok(_run_ct_cake(ws), ws)

    def _artefact_identities() -> dict[str, tuple]:
        """Per-artefact identity that changes iff a producer recipe fired.

        Published artefacts are compared by inode alone: every build
        freshens the cas-exedir entries it uses, republished or not
        (``BuildBackend._freshen_published_cas_entries``), so their mtime
        advances with no recipe behind it. Inode still answers the question
        this test asks, because every producer writes a temp file and
        renames it into place — the same reasoning
        ``test_link_artefact_reused_across_workspaces`` relies on. Object
        entries keep the mtime comparison too; nothing freshens those.
        """
        out: dict[str, tuple] = {}
        for cache_root in (ws / "cas-objdir", ws / "cas-exedir"):
            if not cache_root.is_dir():
                continue
            published = cache_root.name == "cas-exedir"
            for p in cache_root.rglob("*"):
                if p.is_file() and p.suffix in (".o", ".exe", ".a", ".so"):
                    stat = p.stat()
                    out[str(p)] = (stat.st_ino,) if published else (stat.st_ino, stat.st_mtime)
        return out

    _build()
    before = _artefact_identities()
    assert before, "first build produced no cached artefacts in cas-objdir/cas-exedir"

    # Bump every source / header in the workspace to "the future" so
    # mtime-based prereq comparison would force a rebuild.
    future = time.time() + 3600.0
    for p in ws.rglob("*"):
        if p.is_file() and p.suffix in (".cpp", ".cc", ".c", ".h", ".hpp", ".hxx", ".hh"):
            os.utime(p, (future, future))

    _build()
    after = _artefact_identities()

    assert set(before.keys()) == set(after.keys()), (
        f"second build produced different artefact set:\n"
        f"  only in first:  {sorted(set(before) - set(after))}\n"
        f"  only in second: {sorted(set(after) - set(before))}"
    )
    changed = {p: (before[p], after[p]) for p in before if after[p] != before[p]}
    assert not changed, (
        f"second build re-executed {len(changed)} producer recipe(s) despite "
        f"CAS-stable artefact paths (rebuild regressed to mtime-based). "
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
        # --use-mtime is only honored by make/ninja; the default backend
        # (shake) rejects it with a hard ValueError.
        _assert_build_ok(_run_ct_cake(ws, "--backend=make", "--use-mtime"), ws)

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

    Reuse must also keep the entry warm. The entries are aged to 45 days
    old between the two builds, and the second build must leave them
    fresh: ``ct-cas-publish`` freshens the entry's mtime under its lock,
    so ``ct-trim-cache --max-age`` and the oldest-first ``--max-size``
    budget rank an entry every build still publishes as recently used
    rather than as old as its creation.
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

    _build(ws1)
    after_first = _cache_stats(shared_cas_exedir, ".exe")
    assert after_first, f"first build produced no .exe in {shared_cas_exedir}"

    # Age every entry well past any plausible --max-age so the freshening
    # check below cannot be satisfied by filesystem timestamp granularity.
    aged_to = time.time() - 45 * 86400
    for entry in shared_cas_exedir.rglob("*.exe"):
        os.utime(entry, (aged_to, aged_to))

    _build(ws2)
    after_second = _cache_stats(shared_cas_exedir, ".exe")

    only_first = set(after_first) - set(after_second)
    only_second = set(after_second) - set(after_first)
    assert not only_first and not only_second, (
        "cas-exe filenames differ across workspaces — link key is path-bound:\n"
        f"  only in ws1: {sorted(only_first)}\n  only in ws2: {sorted(only_second)}"
    )

    # Same inode after the second build proves the link rule did not
    # re-fire (which would temp+rename to a fresh inode).
    swapped_inode = {n for n in after_first if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"second build re-linked {len(swapped_inode)} cached executable(s) "
        f"(inode swap proves a fresh temp+rename happened): {sorted(swapped_inode)}"
    )
    still_aged = {n for n in after_first if after_second[n][1] <= aged_to}
    assert not still_aged, (
        f"second build reused {len(still_aged)} cached executable(s) but left them "
        f"45 days stale — publishing did not freshen the entry, so an age-gated "
        f"trim would evict artefacts that are still in active use: {sorted(still_aged)}"
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
    # Cross-workspace PCH reuse trips clang < 22's `#pragma once` dedup:
    # the staged ``<cas-pchdir>/<hash>/<header>`` hardlinks to ws1's
    # header inode, but ws2's source ``#include "<header>"`` resolves to
    # ws2's distinct inode, so clang re-parses the header on top of the
    # already-loaded PCH and errors ``redefinition``. gcc and clang ≥ 22
    # dedupe by content/include-guard and handle this; older clang does
    # not. Sample uses ``#pragma once`` with an ``inline`` definition,
    # which is the realistic single-pragma-once PCH header pattern.
    _cxx = compiletools.apptools.get_functional_cxx_compiler()
    _ver = compiletools.apptools._compiler_major_version(_cxx) if _cxx else None
    if _ver is not None and _ver[0] == "clang" and _ver[1] < 22:
        pytest.skip(
            f"clang {_ver[1]} dedupes #pragma once by inode only; cross-workspace "
            "PCH reuse re-parses the header (clang issue, not a ct-cake regression). "
            "Re-enable on clang ≥ 22."
        )

    sample_src = uth.example_path("pch")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws1 = tmp_path / "ws1" / "pch"
    ws2 = tmp_path / "ws2" / "pch"
    shutil.copytree(sample_src, ws1)
    shutil.copytree(sample_src, ws2)
    shared_cas_pchdir = tmp_path / "shared-cas-pchdir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-pchdir={shared_cas_pchdir}"), workdir)

    def _cmd_hash_dirs() -> set[str]:
        # Layout: cas-pchdir/[<variant>/]<cmd_hash>/<header>.gch
        # Walk to the .gch and take its parent dir's name as the cmd_hash.
        return {p.parent.name for p in shared_cas_pchdir.rglob("*.gch") if p.is_file()}

    _build(ws1)
    after_first_gch = _cache_stats(shared_cas_pchdir, ".gch")
    after_first_dirs = _cmd_hash_dirs()
    assert after_first_gch, f"first build produced no .gch in {shared_cas_pchdir}"
    assert len(after_first_dirs) == 1, f"first build produced unexpected cmd_hash count: {sorted(after_first_dirs)}"

    _build(ws2)
    after_second_gch = _cache_stats(shared_cas_pchdir, ".gch")
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


def _build_lib_sample(workdir, lib_source_name: str, *, kind: str) -> None:
    """Create a 2-file sample workspace that asks ct-cake to produce a
    library (``kind="static"`` or ``"dynamic"``): a header, a body
    that defines a single function, and a short ``ct.conf.d/ct.conf``
    listing the body under the matching source-kind keyword.

    ``kind="dynamic"`` additionally appends ``CPPFLAGS = -fPIC`` so the
    object can be linked into a ``.so`` on platforms where PIC is not
    the default for static-library compilation.

    Returns nothing; caller invokes ct-cake from ``workdir``.
    """
    assert kind in ("static", "dynamic"), kind
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "ct.conf.d").mkdir(exist_ok=True)
    extra = "CPPFLAGS = -fPIC\n" if kind == "dynamic" else ""
    (workdir / "ct.conf.d" / "ct.conf").write_text(f"{kind} = {lib_source_name}\nvariant = blank\n{extra}")
    stem = os.path.splitext(lib_source_name)[0]
    (workdir / f"{stem}.hpp").write_text(f"#pragma once\nint {stem}_value();\n")
    (workdir / lib_source_name).write_text(f'#include "{stem}.hpp"\nint {stem}_value() {{ return 42; }}\n')


def _assert_reclaimable_once_unpublished(cas_exedir, workspaces, suffix: str) -> None:
    """Once every published ``bin/`` is gone, the pool must be trimmable.

    A library entry that stays above ``nlink == 1`` with nothing published is
    protected by trim's hard-link rule forever, and no ``--keep-count`` /
    ``--max-age`` / ``--max-size`` setting can reclaim it. That is what a
    second, content-addressed cas name for the same library used to do (the
    two names hardlinked to one inode and pinned each other). ``keep_count``
    is 0 here so bucket rank is out of the way and the assertion is about
    reachability alone; ``basenames_found`` is asserted too, because a second
    pool name also mints a phantom second per-basename bucket.

    ``--purge-ca-siblings`` is deliberately NOT passed: a fixed build never
    writes the sibling, so a plain trim has to be sufficient. Reaching for the
    migration flag here would hide a regression that reintroduced it.
    """
    for workspace in workspaces:
        for bindir in workspace.rglob("bin"):
            if bindir.is_dir():
                shutil.rmtree(bindir)

    pinned = {p.name: p.stat().st_nlink for p in cas_exedir.rglob(f"*{suffix}") if p.is_file()}
    assert pinned, f"no {suffix} entries left to check in {cas_exedir}"
    assert all(n == 1 for n in pinned.values()), (
        f"cas-exedir {suffix} entries still hard-linked with no bin/ published — "
        f"trim's nlink protection will spare them forever: {pinned}"
    )

    result = subprocess.run(
        [
            "ct-trim-cache",
            f"--cas-exedir={cas_exedir}",
            "--cas-exedir-only",
            "--keep-count",
            "0",
            "--max-age",
            "0",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(workspaces[0]),
        timeout=180,
        env=_e2e_env(),
    )
    assert result.returncode == 0, f"ct-trim-cache failed:\n{result.stdout}\n{result.stderr}"
    stats = json.loads(result.stdout)["exedir"]
    assert stats["basenames_found"] == 1, (
        f"expected one {suffix} basename bucket, got {stats['basenames_found']} — "
        f"a second cas name for the same library buckets separately: {stats}"
    )
    assert stats["removed"] == len(pinned), f"trim reclaimed {stats['removed']} of {len(pinned)} entries: {stats}"
    survivors = [p.name for p in cas_exedir.rglob(f"*{suffix}") if p.is_file()]
    assert not survivors, f"unreclaimable {suffix} entries survived an aggressive trim: {survivors}"


@uth.requires_functional_compiler
def test_static_library_reused_across_workspaces(tmp_path):
    """Static-library cache regression guard: same .a built in ws1 and
    ws2 (sharing one cas-exedir) must reuse the cached archive.
    """
    ws1 = tmp_path / "ws1" / "lib"
    ws2 = tmp_path / "ws2" / "lib"
    _build_lib_sample(ws1, "mylib.cpp", kind="static")
    _build_lib_sample(ws2, "mylib.cpp", kind="static")
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-exedir={shared_cas_exedir}"), workdir)

    _build(ws1)
    after_first = _cache_stats(shared_cas_exedir, ".a")
    assert after_first, f"first build produced no .a in {shared_cas_exedir}"

    _build(ws2)
    after_second = _cache_stats(shared_cas_exedir, ".a")

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

    _assert_reclaimable_once_unpublished(shared_cas_exedir, [ws1, ws2], ".a")


@uth.requires_functional_compiler
def test_bundle_with_workspace_relative_wrapper_reuses_cache(tmp_path):
    """Integration: bundle variant + workspace-relative compiler wrapper +
    two workspaces -> identical object filenames in cas-objdir.

    This is the missing integration test the audit flagged: upstream's
    `test_compiler_identity_acceptance` proves cache-key sites canonicalise
    given a SimpleNamespace(CXX=...), and `test_configutils` proves the
    resolver chains bundles correctly. This test stitches both:

      1. A multi-axis bundle resolves correctly through configargparse +
         the parseargs pipeline.
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
def test_ldflags_change_forks_link_key(tmp_path):
    """Positive-discrimination guard for the cas-exedir link key: a
    link-relevant LDFLAGS change must produce a NEW cached executable
    entry rather than silently reusing the old one.

    Audit context: nothing previously pinned that changing LDFLAGS
    actually forks the link key — a regression there would silently
    reuse a stale executable that was linked against the wrong flags.
    ``--append-LDFLAGS`` flows into ``get_build_state(args).flags.ld``,
    which ``_create_link_rule`` folds into ``ld_extra`` and then into
    ``link_key_payload["ld_extra"]`` in ``build_backend.py`` (distinct
    from ``link_key_payload["merged_ldflags"]``, which only carries
    per-file ``//#LDFLAGS=`` magic-comment annotations, not CLI/conf
    LDFLAGS). The negative control — an identical-argv rebuild reusing
    the cached entry — is already pinned by
    ``test_link_artefact_reused_across_workspaces`` above; this test is
    its positive-discrimination complement and does not duplicate it.
    """
    sample_src = uth.example_path("factory")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    ws = tmp_path / "ws" / "factory"
    shutil.copytree(sample_src, ws)
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(*extra_args):
        _assert_build_ok(_run_ct_cake(ws, f"--cas-exedir={shared_cas_exedir}", *extra_args), ws)

    _build()
    after_first = _cache_stats(shared_cas_exedir, ".exe")
    assert after_first, f"first build produced no .exe in {shared_cas_exedir}"
    # Precondition: the factory sample links exactly one executable, so a
    # single new entry after the LDFLAGS rebuild is the expected signal —
    # not an artefact of some other target also forking.
    assert len(after_first) == 1, f"expected exactly one cached executable before the rebuild: {sorted(after_first)}"

    _build("--append-LDFLAGS=-Wl,--build-id=none")
    after_second = _cache_stats(shared_cas_exedir, ".exe")

    only_second = set(after_second) - set(after_first)
    assert only_second, (
        "LDFLAGS change did not fork the link key — cas-exedir entry set is "
        f"unchanged after --append-LDFLAGS: {sorted(after_second)}"
    )

    # The original entry must survive untouched: same inode proves the
    # LDFLAGS rebuild produced a NEW entry rather than overwriting the old
    # one in place (which would corrupt any peer still relying on the
    # un-flagged binary's cached bytes).
    unchanged = set(after_first) & set(after_second)
    assert unchanged, "original entry vanished entirely after the LDFLAGS rebuild"
    swapped_inode = {n for n in unchanged if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"LDFLAGS rebuild overwrote the original cached executable in place "
        f"(inode swap proves a fresh temp+rename happened over the old entry): {sorted(swapped_inode)}"
    )


@uth.requires_functional_compiler
def test_object_set_change_forks_link_key(tmp_path):
    """Positive-discrimination guard for the cas-exedir link key: adding
    a second implied source to the build (growing the linked object
    set) must produce a NEW cached executable entry, not merely an
    mtime bump on the old one.

    Mirrors ``test_cake.py::test_deeper_include_edit_recompiles``'s
    mechanism: ``main.cpp`` includes ``extra.hpp``, which is compiled
    into ``extra.cpp``; a standalone ``deeper.hpp``/``deeper.cpp`` pair
    sits in the workspace unreferenced by anything. Injecting
    ``#include "deeper.hpp"`` into ``extra.hpp`` pulls ``deeper.cpp``
    into the required-source set via the ``foo.h`` -> ``foo.cpp``
    implied-source discovery, growing ``link_key_payload["objects"]``
    in ``build_backend.py`` and forking the link key.
    """
    ws = tmp_path / "ws" / "objset"
    ws.mkdir(parents=True)
    (ws / "main.cpp").write_text(
        '#include "extra.hpp"\n\nint main(int argc, char* argv[])\n{\n    return extra_func(42);\n}\n'
    )
    (ws / "extra.hpp").write_text("int extra_func(const int value);\n")
    (ws / "extra.cpp").write_text('#include "extra.hpp"\n\nint extra_func(const int value)\n{\n    return 24;\n}\n')
    (ws / "deeper.hpp").write_text("int deeper_func(const int value);\n")
    (ws / "deeper.cpp").write_text('#include "deeper.hpp"\n\nint deeper_func(const int value)\n{\n    return 42;\n}\n')
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build():
        _assert_build_ok(_run_ct_cake(ws, f"--cas-exedir={shared_cas_exedir}"), ws)

    _build()
    after_first = _cache_stats(shared_cas_exedir, ".exe")
    assert after_first, f"first build produced no .exe in {shared_cas_exedir}"
    assert len(after_first) == 1, f"expected exactly one cached executable before the edit: {sorted(after_first)}"

    # Precondition proving the observable can move: deeper.cpp exists on
    # disk but is not yet reachable from any compiled TU, so it must NOT
    # have been compiled by the first build.
    objs_before = {p.name for p in (ws / "cas-objdir").rglob("*.o")}
    assert objs_before, f"first build produced no objects under {ws}/cas-objdir"
    assert not any(name.startswith("deeper_") for name in objs_before), (
        f"deeper.cpp was unexpectedly compiled before it was included by anything: {sorted(objs_before)}"
    )

    extra_hpp = ws / "extra.hpp"
    extra_hpp.write_text('#include "deeper.hpp"\n' + extra_hpp.read_text())

    _build()
    after_second = _cache_stats(shared_cas_exedir, ".exe")

    only_second = set(after_second) - set(after_first)
    assert only_second, (
        "pulling deeper.cpp into the build via a header edit did not fork the "
        f"link key — cas-exedir entry set unchanged: {sorted(after_second)}"
    )

    # deeper.cpp must actually have joined the object set — otherwise the
    # new .exe entry could be explained by something other than the
    # object-set growth this test targets.
    objs_after = {p.name for p in (ws / "cas-objdir").rglob("*.o")}
    assert any(name.startswith("deeper_") for name in objs_after), (
        f"deeper.cpp was not compiled after being included via extra.hpp: {sorted(objs_after)}"
    )

    # Original entry survives untouched — CAS never deletes on a plain
    # build, and same inode proves it wasn't overwritten in place.
    unchanged = set(after_first) & set(after_second)
    assert unchanged, "original entry vanished entirely after the object-set change"
    swapped_inode = {n for n in unchanged if after_second[n][0] != after_first[n][0]}
    assert not swapped_inode, (
        f"object-set rebuild overwrote the original cached executable in place "
        f"(inode swap proves a fresh temp+rename happened over the old entry): {sorted(swapped_inode)}"
    )


@uth.requires_functional_compiler
def test_shared_library_reused_across_workspaces(tmp_path):
    """Shared-library cache regression guard: same .so built in ws1 and
    ws2 (sharing one cas-exedir) must reuse the cached library.
    """
    ws1 = tmp_path / "ws1" / "lib"
    ws2 = tmp_path / "ws2" / "lib"
    _build_lib_sample(ws1, "mylib.cpp", kind="dynamic")
    _build_lib_sample(ws2, "mylib.cpp", kind="dynamic")
    shared_cas_exedir = tmp_path / "shared-cas-exedir"

    def _build(workdir):
        _assert_build_ok(_run_ct_cake(workdir, f"--cas-exedir={shared_cas_exedir}"), workdir)

    _build(ws1)
    after_first = _cache_stats(shared_cas_exedir, ".so")
    assert after_first, f"first build produced no .so in {shared_cas_exedir}"

    _build(ws2)
    after_second = _cache_stats(shared_cas_exedir, ".so")

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

    _assert_reclaimable_once_unpublished(shared_cas_exedir, [ws1, ws2], ".so")
