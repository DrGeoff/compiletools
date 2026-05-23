"""Regression guard for the relative-cas-dir / PCH-precompile interaction.

History. The PCH/PCM precompile rules run the compiler under
``cwd = anchor_root`` (``cd <gitroot> && g++ ... -o <cas-path>``) and pass the
*source* gitroot-relative, for cross-user PCH byte-identity
(``build_backend.py``, ``_create_pch_rules`` / the ``pcm_rule_cwd`` branch). The
``-o`` output path, however, is emitted verbatim. So when ``--cas-pchdir`` was a
*relative* path and ct-cake was invoked from a subdirectory of the gitroot, the
relative ``-o`` resolved against the gitroot *after* the ``cd`` instead of the
invocation cwd, and gcc failed with ``cannot create precompiled header ...: No
such file or directory``.

Fix. ``apptools.resolve_cas_directory_arguments`` now anchors any *relative* cas
dir to the gitroot (``os.path.join(find_git_root(), value)``, a no-op for
already-absolute values), reusing the same ``find_git_root()`` value the build's
``anchor_root`` uses. So the precompile rule always receives an absolute ``-o``,
and a relative ``--cas-*dir`` consistently means "relative to the gitroot"
(matching the gitroot-anchored default) regardless of the invocation cwd.
Gitroot-anchoring -- not cwd-based ``abspath`` -- is required because
``canonicalize_path_for_cache_key`` is a textual string-prefix op, so cross-user
byte-identity needs the cas-dir string to share the exact ``anchor_root`` prefix.

These tests, all invoked from a subdir of the gitroot, guard the fix:

  1. ``relative`` CAS root -> builds, and the cache lands at the *gitroot*-
     anchored location (not the cwd). This is the case that used to fail.
  2. ``absolute`` CAS root *under* the gitroot -> builds (unchanged).
  3. ``absolute`` CAS root *outside* the gitroot -> builds. The realistic
     shared-CAS deployment (a team's cache on an NFS/SMB mount, off the source
     tree); exercises the outside-gitroot canonicalization branch. A
     ``tmp_path`` sibling of the repo reproduces the *path/layout* dimension
     faithfully; it does NOT reproduce NFS/SMB *filesystem semantics*
     (locking-strategy selection, ``EXDEV`` on the cas-exedir hardlink-publish,
     mtime granularity) -- those are FS-type driven, need a real mount, and are
     covered by the locking tests.

A fast, compiler-free unit test pins the resolver's anchoring semantics directly.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess

import compiletools.apptools
import compiletools.git_utils
import compiletools.testhelper as uth
from compiletools.examples_registry import example_path


def _nested_workspace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay out a workspace with gitroot=<repo> and the PCH consumer one level
    down in ``<repo>/sub``.

    The ``.git`` marker at ``<repo>`` makes ct-cake resolve the gitroot ABOVE
    the invocation cwd (``<repo>/sub``) -- the structural precondition for the
    historical bug. (``copy_example_workspace`` plants ``.git`` at the copy
    destination, so it cannot produce a cwd-below-gitroot layout; hence the
    manual setup here.)
    """
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    src = pathlib.Path(example_path("relative_cas_dir_bug"))
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, sub)
    return repo, sub


def _build(sub: pathlib.Path, repo: pathlib.Path, cas_root) -> subprocess.CompletedProcess:
    """Run ct-cake on ``widget.cpp`` from ``<sub>``, with all four cas dirs under
    ``cas_root``.

    ``cas_root`` may be relative or absolute. The bindir stays absolute under the
    workspace -- it is a user-facing build product, not part of the
    CAS-location question under test. Host ``*FLAGS`` are stripped so the build
    isn't at the mercy of the operator's environment.
    """
    cas_root = str(cas_root)
    argv = [
        "ct-cake",
        "widget.cpp",
        f"--bindir={repo}/bin",
        f"--cas-objdir={cas_root}/obj",
        f"--cas-pchdir={cas_root}/pch",
        f"--cas-pcmdir={cas_root}/pcm",
        f"--cas-exedir={cas_root}/exe",
    ]
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    return subprocess.run(argv, cwd=sub, env=env, capture_output=True, text=True)


@uth.requires_functional_compiler
def test_relative_cas_root_from_subdir_anchors_to_gitroot(tmp_path):
    """Relative CAS root + cwd below the gitroot -> builds, anchored to the
    gitroot. This is the case that used to fail with the PCH-output-path error.
    """
    repo, sub = _nested_workspace(tmp_path)
    result = _build(sub, repo, "relcache")

    assert result.returncode == 0, (
        "a relative cas root from a subdir of the gitroot should now build "
        "(gitroot-anchored). If this fails with 'cannot create precompiled "
        "header ... No such file or directory', the resolver anchoring "
        f"regressed.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # The cache is anchored to the gitroot, NOT the invocation cwd.
    assert sorted((repo / "relcache" / "pch").rglob("*.gch")), (
        f"expected the cached .gch under the gitroot-anchored {repo}/relcache/pch"
    )
    assert not (sub / "relcache").exists(), (
        "the cache was created relative to the invocation cwd (cwd-anchored), "
        "not the gitroot -- the fix must gitroot-anchor, not abspath-from-cwd."
    )


@uth.requires_functional_compiler
def test_absolute_cas_root_under_gitroot_builds(tmp_path):
    """Absolute CAS root under the gitroot builds (unchanged by the fix)."""
    repo, sub = _nested_workspace(tmp_path)
    cas_root = repo / "abscache"
    result = _build(sub, repo, cas_root)

    assert result.returncode == 0, (
        "absolute cas root under the gitroot should build cleanly from a subdir."
        f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert sorted((cas_root / "pch").rglob("*.gch")), f"expected a cached .gch under {cas_root}/pch"


@uth.requires_functional_compiler
def test_absolute_cas_root_outside_gitroot_builds(tmp_path):
    """A CAS root *outside* the gitroot builds cleanly -- the realistic shared
    CAS deployment (team cache on an NFS/SMB mount, off the source tree).

    Simulated by a ``tmp_path`` sibling of the repo, which is genuinely not
    under the gitroot. This covers the path/layout dimension (off-tree absolute
    artifacts surviving the ``cd <gitroot>`` wrapper, and the outside-gitroot
    canonicalization branch); it does not simulate NFS/SMB locking or EXDEV
    semantics (see module docstring).
    """
    repo, sub = _nested_workspace(tmp_path)
    external = tmp_path / "shared_cas"  # sibling of repo -> outside the gitroot
    # Sanity: the simulated shared CAS really is off-tree.
    assert os.path.commonpath([str(external), str(repo)]) == str(tmp_path)

    result = _build(sub, repo, external)

    assert result.returncode == 0, (
        "an absolute cas root outside the gitroot (shared-CAS deployment) should "
        f"build cleanly from a subdir.\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    gch = sorted((external / "pch").rglob("*.gch"))
    assert gch, f"expected the cached .gch to land in the off-tree CAS {external}/pch"
    # The artifact really lives outside the gitroot, not silently inside it.
    assert not sorted((repo / "abscache").rglob("*.gch"))


def test_resolve_cas_directory_arguments_gitroot_anchors_relative(monkeypatch):
    """Fast, compiler-free check of the resolver's anchoring semantics:
    relative cas dirs anchor to the gitroot, absolute ones pass through, and the
    whole thing is idempotent.
    """
    monkeypatch.setattr(compiletools.git_utils, "find_git_root", lambda *a, **k: "/fake/gitroot")
    args = argparse.Namespace(
        variant="gcc.debug",
        bindir="/fake/gitroot/bin",
        verbose=0,
        cas_objdir="relcache/obj",
        cas_pchdir="/abs/elsewhere/pch",  # absolute -> must pass through, not be re-anchored
        cas_pcmdir="relcache/pcm",
        cas_exedir="relcache/exe",
    )
    compiletools.apptools.resolve_cas_directory_arguments(args)

    assert args.cas_objdir.startswith("/fake/gitroot/relcache/obj")
    assert args.cas_pcmdir.startswith("/fake/gitroot/relcache/pcm")
    assert args.cas_exedir.startswith("/fake/gitroot/relcache/exe")
    # An absolute cas dir (e.g. an NFS mount) is left where it is, not pulled
    # under the gitroot.
    assert args.cas_pchdir.startswith("/abs/elsewhere/pch")
    assert not args.cas_pchdir.startswith("/fake/gitroot")

    # Idempotent: a second resolve must not change anything.
    snapshot = (args.cas_objdir, args.cas_pchdir, args.cas_pcmdir, args.cas_exedir)
    compiletools.apptools.resolve_cas_directory_arguments(args)
    assert (args.cas_objdir, args.cas_pchdir, args.cas_pcmdir, args.cas_exedir) == snapshot
