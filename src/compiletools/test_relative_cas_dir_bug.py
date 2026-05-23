"""Pin a latent build-backend bug: a *relative* ``--cas-pchdir`` breaks the PCH
precompile rule when ct-cake is invoked from a subdirectory of the gitroot.

Root cause (``build_backend.py``, ``_create_pch_rules``, the
``rule_cwd = anchor_root`` branch). For cross-user PCH byte-identity the rule
passes the PCH source *relative to the gitroot* and runs the compiler under
``cwd = anchor_root`` (``cd <gitroot> && g++ ...``). But the ``-o`` output path
(the cas-pchdir target) and the ``mv`` targets are emitted relative to the
*invocation cwd*, not the gitroot. When ``--cas-pchdir`` is a relative path and
the invocation cwd is a subdirectory of the gitroot, ``make`` creates the cache
directory relative to its own cwd while ``cd <gitroot> && g++ -o <relpath>``
resolves the same relative path against the gitroot -- a different, nonexistent
directory -- and gcc fails with ``cannot create precompiled header ...: No such
file or directory``. The C++20 module/BMI rule (the symmetric
``pcm_rule_cwd = anchor_root`` branch) shares the flaw.

Three scenarios, each invoked from a subdir of the gitroot:

  1. ``relative`` CAS root -> the build FAILS with the PCH-output-path
     signature. PINS CURRENT (BUGGY) BEHAVIOR.
  2. ``absolute`` CAS root *under* the gitroot -> the build succeeds (the
     documented workaround).
  3. ``absolute`` CAS root *outside* the gitroot -> the build succeeds. This is
     the realistic shared-CAS deployment (a team's CAS on an NFS/SMB mount,
     off the source tree); it also exercises the "outside-gitroot" branch of
     ``apptools.canonicalize_path_for_cache_key`` (object paths are not under
     ``<gitroot>`` so they keep absolute, user-consistent strings in the link
     key). A ``tmp_path`` sibling of the repo reproduces the *path/layout*
     dimension faithfully and hermetically; it does NOT reproduce NFS/SMB
     *filesystem semantics* (locking-strategy selection, ``EXDEV`` on the
     cas-exedir hardlink-publish, mtime granularity) -- those are FS-type
     driven and need a real mount, and are covered by the locking tests.

When the backend is fixed (e.g. gitroot-anchor relative cas dirs in
``resolve_cas_directory_arguments``, or absolutize the precompile rule's
``-o`` / ``mv`` paths before the ``cd <anchor_root> &&`` wrapper), scenario 1
will start building successfully and must be flipped to assert success.
See ``examples-features/relative_cas_dir_bug/README.md``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import compiletools.testhelper as uth
from compiletools.examples_registry import example_path


def _nested_workspace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Lay out a workspace with gitroot=<repo> and the PCH consumer one level
    down in ``<repo>/sub``.

    The ``.git`` marker at ``<repo>`` makes ct-cake resolve the gitroot ABOVE
    the invocation cwd (``<repo>/sub``) -- the structural precondition for the
    bug. (``copy_example_workspace`` plants ``.git`` at the copy destination, so
    it cannot produce a cwd-below-gitroot layout; hence the manual setup here.)
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

    ``cas_root`` may be relative (the bug case) or absolute (the workarounds).
    The bindir stays absolute under the workspace -- it is a user-facing build
    product, not part of the CAS-location question under test. Host ``*FLAGS``
    are stripped so the build isn't at the mercy of the operator's environment.
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
def test_relative_cas_root_from_subdir_breaks_pch_rule(tmp_path):
    """Relative CAS root + cwd below the gitroot -> the PCH rule fails.

    Pins the current latent bug; see the module docstring for the flip-on-fix
    instruction.
    """
    repo, sub = _nested_workspace(tmp_path)
    # Relative cas dirs: resolved against cwd=sub by make, but the PCH rule cd's
    # to the gitroot before invoking gcc -- so the relative -o path points at
    # <gitroot>/relcache, which does not exist.
    result = _build(sub, repo, "relcache")

    assert result.returncode != 0, (
        "expected the relative-cas-root build to FAIL (this test pins the "
        "current latent bug); if it now succeeds the backend was fixed -- flip "
        "this assertion to assert success and update the README.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "cannot create precompiled header" in combined and "No such file or directory" in combined, (
        "the build failed, but not with the relative-cas PCH-output-path "
        "signature -- the failure is something else and this test is no longer "
        f"pinning the intended bug.\n--- stderr ---\n{result.stderr}"
    )


@uth.requires_functional_compiler
def test_absolute_cas_root_under_gitroot_is_the_workaround(tmp_path):
    """Absolute CAS root under the gitroot builds cleanly (the documented
    workaround)."""
    repo, sub = _nested_workspace(tmp_path)
    # Absolute cas dirs: the PCH rule's -o is absolute, so the cd <gitroot> is
    # harmless and the build succeeds.
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
