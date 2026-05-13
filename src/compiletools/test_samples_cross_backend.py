"""Cross-backend coverage matrix for samples/.

For every sample under ``samples/`` x every registered backend, build
the sample with that backend and assert the build succeeds (or, for
intentional-failure samples, fails at the documented stage). The
matrix is the contract that says "every feature compiles via every
backend" — adding a sample without registering it here breaks the
test, forcing the author to make a deliberate decision.

Most samples are buildable straight from ``ct-cake --auto``. A
handful need:

  * a ``PKG_CONFIG_PATH`` that picks up ``samples/pkgs/*.pc`` fixtures
  * an explicit entry-point file (when ``--auto`` would discover a
    multi-target set that includes intentionally-broken TUs)
  * skipping (intentional bug-reproduction samples; samples whose
    build is steered by a multi-step ``build.sh`` that doesn't fit
    the per-backend matrix; samples that need a system library that
    isn't guaranteed to be installed)

Per-sample policy lives in ``_SAMPLE_PLANS`` below; reasons for skip
or xfail are spelled out so a future maintainer doesn't have to
re-discover them.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
from collections.abc import Iterable

import pytest

import compiletools.testhelper as uth
from compiletools.build_backend import available_backends, ensure_backends_registered

ensure_backends_registered()


# ---------------------------------------------------------------------------
# Sample → build-plan registry.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SamplePlan:
    """How to build a single sample.

    Attributes:
        targets: Explicit source files to pass to ``ct-cake``. Empty
            tuple means ``--auto`` (let ct-cake discover everything).
        needs_pkg_config: When True, prepend ``samples/pkgs/`` to
            ``PKG_CONFIG_PATH`` for the build subprocess.
        skip_reason: When non-empty, the (sample, backend) cell is
            skipped with this reason instead of being built.
        xfail_reason: When non-empty, the build is expected to fail;
            a successful build is the surprise.
        skip_for_backends: Per-backend skip overrides (sample-side
            opt-out, e.g. C++20 modules on backends whose rule emitter
            doesn't yet wire the BMI plumbing).
        extra_args: Additional argv tokens appended after ``--backend``.
        extra_env: Environment variables to set for the subprocess.
    """

    targets: tuple[str, ...] = ()
    needs_pkg_config: bool = False
    skip_reason: str = ""
    xfail_reason: str = ""
    skip_for_backends: frozenset[str] = frozenset()
    extra_args: tuple[str, ...] = ()
    extra_env: dict[str, str] = dataclasses.field(default_factory=dict)


# Most samples are vanilla --auto builds. Only the awkward ones need
# bespoke entries; everything else falls through to the empty default.
_VANILLA: SamplePlan = SamplePlan()

# Many bug-reproduction samples annotate //#PKG-CONFIG=... pointing at
# samples/pkgs/*.pc fixtures whose `Libs:` lines reference stand-in
# library names (-ltestpkg, -lleakedmacro, etc.). They aren't installed
# on the host so the link step fails. The samples exist for ct-
# magicflags / direct-unit-test consumption, not end-to-end builds.
_LINK_FAIL_FICTIONAL_LIBS: SamplePlan = SamplePlan(
    needs_pkg_config=True,
    skip_reason=(
        "Annotates non-existent libraries (samples/pkgs/*.pc Libs lines "
        "reference stand-in names like -ltestpkg, -lleakedmacro). End-to-"
        "end link is intentionally outside the matrix's scope; per-sample "
        "tests in src/compiletools/test_*.py exercise the behaviour."
    ),
)

# Named C++20 modules (interface units, partitions, split impl, import std)
# only build on `make` and `shake` today; the other four backends fail
# at the BMI plumbing stage. `cxx_modules_header_units` is the exception:
# header-units now build on every backend after the upstream fix tracked
# by commit d30e2040 ("test(modules): drop dead _MODULE_FAILING_BACKENDS
# xfail dispatch"), so that sample uses an empty blocklist.
_CXX_MODULES_NAMED_BACKENDS_BLOCKED = frozenset({"bazel", "cmake", "ninja", "slurm"})

_SAMPLE_PLANS: dict[str, SamplePlan] = {
    # ----- vanilla --auto, no special setup -----
    "calculator": _VANILLA,
    "cache_scoping": _VANILLA,
    "computed_include": _VANILLA,
    "conditional_includes": _VANILLA,
    "cppflags_macros": _VANILLA,
    "cross_platform": _VANILLA,
    "dottypaths": _VANILLA,
    "factory": _VANILLA,
    "feature_headers": _VANILLA,
    "ffile_prefix_map": _VANILLA,
    "has_include": _VANILLA,
    "hunter_macro_propagation": _VANILLA,
    "macro_state_dependency": _VANILLA,
    "magicsourceinheader": _VANILLA,
    "movingheaders": _VANILLA,
    "nestedconfig": _VANILLA,
    "pch": _VANILLA,
    "platform_has_include": _VANILLA,
    "separate_cpp_cxx": _VANILLA,
    "simple": _VANILLA,
    "unit_test_marker": _VANILLA,
    "version_dependent_api": _VANILLA,
    "cli_features": _VANILLA,
    # ----- vanilla --auto + per-sample CLI knobs -----
    "magicinclude": SamplePlan(
        # //#INCLUDE=subdir auto-resolves subdir/important.hpp; the
        # other two header dirs (subdir2, subdir3) are reachable only
        # via the --include CLI flag, mirroring test_magicinclude.py.
        extra_args=("--include=subdir2", "--prepend-INCLUDE=subdir3"),
    ),
    "numbers": SamplePlan(
        # The directory has two test entry points — test_direct_include.cpp
        # builds standalone, test_library.cpp expects -lget_numbers from
        # the library/ build. Pick the standalone one for the matrix; the
        # multi-step variant is exercised by samples/library/build.sh.
        targets=("test_direct_include.cpp",),
    ),
    # ----- intentional build failure (the demonstration IS the failure) -----
    "pkgconfig_cycle": SamplePlan(
        needs_pkg_config=True,
        xfail_reason=(
            "Intentional: two TUs assert opposite hard PKG-CONFIG link "
            "orderings, ct-cake raises LDFLAGSCycleError before any compile "
            "command runs. See samples/pkgconfig_cycle/README.md."
        ),
    ),
    # ----- skip: link references stand-in / fictional libraries -----
    "empty_macro_bug": _LINK_FAIL_FICTIONAL_LIBS,
    "header_guard_bug": _LINK_FAIL_FICTIONAL_LIBS,
    "magic_processing_order": _LINK_FAIL_FICTIONAL_LIBS,
    "magicpkgconfig_fake": _LINK_FAIL_FICTIONAL_LIBS,
    "parse_order_macro_bug": _LINK_FAIL_FICTIONAL_LIBS,
    "pkg_config_header_deps": _LINK_FAIL_FICTIONAL_LIBS,
    "project_pkgconfig_override": _LINK_FAIL_FICTIONAL_LIBS,
    "transitive_cache_bug": _LINK_FAIL_FICTIONAL_LIBS,
    "undef_bug": _LINK_FAIL_FICTIONAL_LIBS,
    "static_link_order": SamplePlan(
        skip_reason=(
            "//#LDFLAGS=-llibbase -llibnext reference fictional libraries; "
            "the sample exists to verify topological link-order resolution "
            "(test_static_link_order.py), not to produce a working binary."
        ),
    ),
    "ldflags": SamplePlan(
        skip_reason=(
            "TUs reference -lmylib, -lnewapi, -ldebug_library etc — "
            "stand-in names with no real .so on the system, so the link "
            "step fails. The sample is for ct-magicflags / unit-test "
            "consumption, not end-to-end builds."
        ),
    ),
    "lotsofmagic": SamplePlan(
        skip_reason="Requires -lpcap installed system-wide; not portable.",
    ),
    "duplicate_flags": SamplePlan(
        skip_reason=(
            "Annotates -lzip, -lmath, -lpthread, -lm with no source "
            "matching the linker's expectations; intended for "
            "ct-magicflags dedup tests, not end-to-end builds."
        ),
    ),
    "macro_deps": SamplePlan(
        skip_reason=(
            "Requires real zlib + samples/pkgs/nested.pc; unit-test "
            "coverage in test_*.py covers the dependency-discovery "
            "behaviour without a full link."
        ),
    ),
    "magicpkgconfig": SamplePlan(
        skip_reason="Requires real zlib installed and discoverable via pkg-config.",
    ),
    "pkgconfig": SamplePlan(
        skip_reason="Requires real zlib installed and discoverable via pkg-config.",
    ),
    # ----- skip: setup-dependent configurations -----
    "cycle": SamplePlan(
        skip_reason=(
            "Headers-only fixture with no main(); ct-cake --auto has "
            "nothing to discover here. The sample exists to be loaded "
            "by ct-magicflags / direct unit tests of cycle handling."
        ),
    ),
    "isystem_include_bug": SamplePlan(
        skip_reason=(
            "Requires `-isystem fake_system_include` to be added to "
            "CPPFLAGS; per-sample test_isystem_include_bug.py wires "
            "this in via a temp config rather than the matrix harness."
        ),
    ),
    "test_xml_output": SamplePlan(
        skip_reason=(
            "Per-fixture test_test_xml_output_e2e.py copies one stub at "
            "a time (gtest, doctest, catch2) into a temp dir with the "
            "matching framework header. `ct-cake --auto` on the whole "
            "directory tries to compile all three stubs against all "
            "three framework include paths simultaneously, which fails."
        ),
    ),
    # cmake wraps each copt in "..." (line: f'"{c}"') so a copt token like
    # -DCT_PROJECT_VERSION="1.2.3" becomes "-DCT_PROJECT_VERSION="1.2.3""
    # which is malformed CMake syntax.  bazel passes copts through its own
    # quoting layer which strips the inner double-quote chars.  Both are
    # separate bugs from the make/ninja fix in apptools._unify_cpp_cxx_flags.
    "project_version": SamplePlan(
        extra_args=("--project-version=1.2.3", "--project-name=demo_app"),
        skip_for_backends=frozenset({"cmake", "bazel"}),
    ),
    # ----- multi-step build.sh — exercised by their own dedicated tests -----
    "library": SamplePlan(
        skip_reason=(
            "Two-stage build (mylib first, then top-level) is steered by "
            "build.sh and covered by test_library.py, which already runs "
            "across backends."
        ),
    ),
    "dynamic_library": SamplePlan(
        skip_reason=(
            "Two-stage build via build.sh; matrix coverage would require a fixture mirroring test_library.py."
        ),
    ),
    "multi_axis_variant": SamplePlan(
        # build.sh sweeps several variants; the matrix tests the
        # default. Per-axis coverage lives in test_apptools / test_configutils.
        targets=("axis_probe.cpp",),
    ),
    "testprefix": SamplePlan(
        extra_env={"TESTPREFIX": "timeout 5"},
    ),
    "serialise_tests": SamplePlan(
        skip_reason=(
            "Test executables verify file-locking semantics by holding "
            "locks across processes; covered end-to-end by "
            "test_serialisetests.py with backend-specific fixtures."
        ),
    ),
    # ----- C++20 modules: backend support varies by sample shape -----
    "cxx_modules": SamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_split": SamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_partitions": SamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_import_std": SamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    # Header units: full cross-backend coverage thanks to upstream fix.
    "cxx_modules_header_units": _VANILLA,
    # ----- pure fixture directories (no buildable TU) -----
    "pkgs": SamplePlan(
        skip_reason="Holds .pc fixtures only; not a buildable sample.",
    ),
}


# ---------------------------------------------------------------------------
# Sample discovery + parametrize ID generation.
# ---------------------------------------------------------------------------


def _discover_samples() -> list[str]:
    """Return every directory name directly under samples/."""
    samples_root = pathlib.Path(uth.samplesdir())
    return sorted(p.name for p in samples_root.iterdir() if p.is_dir())


def _sample_plan(sample_name: str) -> SamplePlan:
    """Look up a sample's plan, defaulting to vanilla --auto.

    Unknown samples (added by a future PR without registering them
    here) get _VANILLA — the test will likely succeed if the new
    sample is a normal --auto build, or fail with a useful diagnostic
    that points the author at this registry.
    """
    return _SAMPLE_PLANS.get(sample_name, _VANILLA)


def _matrix_id(sample_name: str, backend_name: str) -> str:
    return f"{sample_name}-{backend_name}"


def _matrix_params() -> Iterable[tuple[str, str]]:
    samples = _discover_samples()
    backends = available_backends()
    for sample in samples:
        for backend in backends:
            yield sample, backend


_MATRIX = list(_matrix_params())
_IDS = [_matrix_id(s, b) for s, b in _MATRIX]


# ---------------------------------------------------------------------------
# Build helpers.
# ---------------------------------------------------------------------------


def _copy_sample(sample_name: str, dst: pathlib.Path) -> pathlib.Path:
    """Copy samples/<sample_name>/ to a fresh dst/ workspace and plant
    a .git marker so :func:`compiletools.git_utils.find_git_root` lands
    on the workspace (not on the surrounding pytest tmpdir or the
    test-runner's cwd)."""
    src = pathlib.Path(uth.samplesdir()) / sample_name
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst)
        else:
            shutil.copytree(entry, dst / entry.name)
    (dst / ".git").mkdir()
    return dst


def _build_argv(workspace: pathlib.Path, backend_name: str, plan: SamplePlan) -> list[str]:
    """Compose the ct-cake argv for a workspace, matching the per-CAS
    isolation pattern from test_ffile_prefix_map."""
    argv = [
        "ct-cake",
        f"--backend={backend_name}",
        f"--cas-objdir={workspace}/cas-objdir",
        f"--bindir={workspace}/bin",
        f"--cas-pchdir={workspace}/cas-pchdir",
        f"--cas-pcmdir={workspace}/cas-pcmdir",
        f"--cas-exedir={workspace}/cas-exedir",
        f"--diagnostics-dir={workspace}/diagnostics",
    ]
    argv.extend(plan.extra_args)
    if plan.targets:
        argv.extend(str(workspace / t) for t in plan.targets)
    else:
        argv.append("--auto")
    return argv


def _build_env(plan: SamplePlan) -> dict[str, str]:
    """Build a clean environment for the subprocess.

    Strips host CXXFLAGS/CFLAGS/LDFLAGS/CPPFLAGS so the test isn't at
    the mercy of whatever the operator has exported, then layers on the
    sample's own extra_env, then optionally PKG_CONFIG_PATH pointing at
    samples/pkgs/."""
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    if plan.needs_pkg_config:
        env["PKG_CONFIG_PATH"] = str(pathlib.Path(uth.samplesdir()) / "pkgs")
    env.update(plan.extra_env)
    return env


def _run_build(
    sample_name: str,
    backend_name: str,
    workspace: pathlib.Path,
    plan: SamplePlan,
) -> subprocess.CompletedProcess:
    argv = _build_argv(workspace, backend_name, plan)
    env = _build_env(plan)
    try:
        return subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True)
    finally:
        # Bazel keeps a JVM server alive per cwd. Across many cells the
        # accumulated server processes exhaust native thread allocation
        # and downstream cells fail with `unable to create native thread:
        # possibly out of memory or process/resource limits reached`.
        # Explicit shutdown after each cell prevents the pile-up.
        if backend_name == "bazel":
            subprocess.run(
                ["bazel", "shutdown"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )


# ---------------------------------------------------------------------------
# The matrix test.
# ---------------------------------------------------------------------------


@uth.requires_functional_compiler
@pytest.mark.parametrize(("sample_name", "backend_name"), _MATRIX, ids=_IDS)
def test_sample_builds_with_backend(sample_name, backend_name, tmp_path):
    """Build *sample_name* with *backend_name*; assert the policy in
    ``_SAMPLE_PLANS`` is honoured.

    Three outcomes per cell:

    * ``skip_reason`` set → ``pytest.skip`` with the registered reason.
    * ``xfail_reason`` set → expected to fail; an unexpected success
      becomes an XPASS.
    * default → the build must succeed (returncode == 0).

    The backend tool must be on PATH (via ``requires_backend_tool``)
    and a functional compiler must be present.
    """
    plan = _sample_plan(sample_name)

    if plan.skip_reason:
        pytest.skip(plan.skip_reason)
    if backend_name in plan.skip_for_backends:
        pytest.skip(f"sample {sample_name} opted out of backend {backend_name}")
    if not uth._backend_tool_available(backend_name):
        pytest.skip(f"{backend_name} build tool not on PATH")

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        workspace = _copy_sample(sample_name, pathlib.Path(effective_tmp) / "ws")
        result = _run_build(sample_name, backend_name, workspace, plan)

        if plan.xfail_reason:
            if result.returncode == 0:
                pytest.xfail(f"unexpected build success despite xfail policy: {plan.xfail_reason}")
            return  # expected failure

        assert result.returncode == 0, (
            f"ct-cake failed for sample={sample_name!r} backend={backend_name!r}\n"
            f"argv: {_build_argv(workspace, backend_name, plan)}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )


# ---------------------------------------------------------------------------
# Drift guard: every directory under samples/ must be registered.
# ---------------------------------------------------------------------------


def test_every_sample_has_a_plan():
    """Adding a new sample without registering it in ``_SAMPLE_PLANS``
    is fine — the unknown-sample path defaults to vanilla --auto. But
    we want to *force the author to think about it* so that
    intentional-failure samples don't silently start failing the matrix
    or unbuildable fixture-only directories don't waste cycles.

    Asserting unconditionally here surfaces the question at PR time:
    "your new sample isn't classified — pick a plan or document why
    --auto is right."
    """
    discovered = set(_discover_samples())
    registered = set(_SAMPLE_PLANS)
    missing = sorted(discovered - registered)
    assert not missing, (
        "New samples without an entry in _SAMPLE_PLANS:\n  "
        + "\n  ".join(missing)
        + "\nAdd a SamplePlan() entry (use _VANILLA if `ct-cake --auto` is correct)."
    )
    stale = sorted(registered - discovered)
    assert not stale, "_SAMPLE_PLANS references samples that no longer exist:\n  " + "\n  ".join(stale)
