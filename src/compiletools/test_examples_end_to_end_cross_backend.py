"""Cross-backend coverage matrix for examples-end-to-end/.

For every example under ``examples-end-to-end/`` x every registered
backend, build the example with that backend and assert the build
succeeds (or, for intentional-failure examples, fails at the
documented stage). The matrix is the contract that says "every
feature compiles via every backend" — adding an example to
``EXAMPLES_E2E`` without registering its plan here breaks the test,
forcing the author to make a deliberate decision.

Most examples are buildable straight from ``ct-cake``. A handful need:

  * an explicit entry-point file (when the default discovery would
    pick a multi-target set that includes a TU intended for a
    different invocation)
  * a ``PKG_CONFIG_PATH`` that picks up
    ``examples-features/pkgs/*.pc`` fixtures (for the one xfail
    sample, ``pkgconfig_cycle``)
  * skipping (intentional bug-reproduction examples; examples whose
    build is steered by a multi-step ``build.sh`` that doesn't fit
    the per-backend matrix; examples that need a system library that
    isn't guaranteed to be installed)

Per-example policy lives in ``_EXAMPLE_PLANS`` below; reasons for skip
or xfail are spelled out so a future maintainer doesn't have to
re-discover them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterable

import pytest

import compiletools.apptools
import compiletools.testhelper as uth
from compiletools.build_backend import available_backends, ensure_backends_registered

ensure_backends_registered()


def _toolchain_supports_import_std() -> bool:
    """True iff the local C++ toolchain ships the standard library module source.

    ``import std;`` requires gcc 15+ (which ships ``bits/std.cc``) or a clang
    install that ships ``share/libc++/v1/std.cppm``. Ubuntu's stock
    ``build-essential`` (the CI runner's compiler source) is too old, so the
    matrix has to gate the ``cxx_modules_import_std`` example on the toolchain
    actually shipping a std-module source rather than blindly building it.
    """
    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx:
        return False
    kind = compiletools.apptools.compiler_kind(cxx)
    return compiletools.apptools.find_system_std_module_source(cxx, kind) is not None


# ---------------------------------------------------------------------------
# Example → build-plan registry.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ExamplePlan:
    """How to build a single example.

    Attributes:
        targets: Explicit source files to pass to ``ct-cake``. Empty
            tuple means ``--auto`` (let ct-cake discover everything).
        needs_pkg_config: When True, prepend ``examples-features/pkgs/`` to
            ``PKG_CONFIG_PATH`` for the build subprocess.
        skip_reason: When non-empty, the (example, backend) cell is
            skipped with this reason instead of being built.
        xfail_reason: When non-empty, the build is expected to fail;
            a successful build is the surprise.
        skip_for_backends: Per-backend skip overrides (example-side
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


# Most examples are vanilla --auto builds. Only the awkward ones need
# bespoke entries; everything else falls through to the empty default.
_VANILLA: ExamplePlan = ExamplePlan()

# Named C++20 modules (interface units, partitions, split impl, import std)
# fail on ninja/cmake/bazel today. `slurm` was unblocked by the Wave 2
# fix: _prebuild_aux_artefacts now locally executes named-module interface
# compile rules before the flat slurm job array is submitted, so importer
# compiles no longer race with interface compiles.
# `cxx_modules_header_units` is the exception: header-units now build on
# every backend after the upstream fix tracked by commit d30e2040
# ("test(modules): drop dead _MODULE_FAILING_BACKENDS xfail dispatch"),
# so that example uses an empty blocklist.
_CXX_MODULES_NAMED_BACKENDS_BLOCKED = frozenset()

_EXAMPLE_PLANS: dict[str, ExamplePlan] = {
    # ----- vanilla --auto, no special setup -----
    "appinfo": ExamplePlan(
        # Best-practice generated-implementation pattern. The prebuild
        # writes appinfo.cpp adjacent to appinfo.hpp; ct-cake's implied-
        # source mechanism (Hunter walks main.cpp's includes, looks for a
        # .cpp adjacent to each #include'd header) picks it up at
        # build_graph time without explicit listing. Per-backend matrix
        # entry just confirms the cross-backend wiring works; the exact
        # stdout assertion lives in test_appinfo_example.py.
        extra_args=("--prebuild-script=./gen_appinfo.sh appinfo.cpp",),
        extra_env={"APP_NAME": "demo_app", "APP_VERSION": "1.2.3"},
    ),
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
    # ----- vanilla --auto + per-example CLI knobs -----
    "magicinclude": ExamplePlan(
        # //#INCLUDE=subdir auto-resolves subdir/important.hpp; the
        # other two header dirs (subdir2, subdir3) are reachable only
        # via the --include CLI flag, mirroring test_magicinclude.py.
        extra_args=("--include=subdir2", "--prepend-INCLUDE=subdir3"),
    ),
    "numbers": ExamplePlan(
        # The directory has two test entry points — test_direct_include.cpp
        # builds standalone, test_library.cpp expects -lget_numbers from
        # the library/ build. Pick the standalone one for the matrix; the
        # multi-step variant is exercised by examples-end-to-end/library/build.sh.
        targets=("test_direct_include.cpp",),
    ),
    # ----- intentional build failure (the demonstration IS the failure) -----
    "pkgconfig_cycle": ExamplePlan(
        needs_pkg_config=True,
        xfail_reason=(
            "Intentional: two TUs assert opposite hard PKG-CONFIG link "
            "orderings, ct-cake raises LDFLAGSCycleError before any compile "
            "command runs. See examples-end-to-end/pkgconfig_cycle/README.md."
        ),
    ),
    "project_version": ExamplePlan(
        extra_args=("--project-version=1.2.3", "--project-name=demo_app"),
    ),
    "prebuild_script": ExamplePlan(
        # The prebuild script generates build/version.h before the build
        # graph walks the include graph. The .sh path is relative to the
        # workspace cwd that _run_build passes to subprocess.run.
        extra_args=("--prebuild-script=./gen_version.sh build/version.h",),
    ),
    "postbuild_script": ExamplePlan(
        # The postbuild script sets DEMO_ENV_VAR and execs the built
        # binary, printing its stdout into ct-cake's output stream.
        # Content of that stdout is asserted by
        # test_postbuild_script_example.py; this matrix entry just
        # confirms the example builds (and the hook runs cleanly) on
        # every backend.
        extra_args=("--postbuild-script=./run_with_env.sh",),
    ),
    # ----- multi-step build.sh — exercised by their own dedicated tests -----
    "multi_axis_variant": ExamplePlan(
        # build.sh sweeps several variants; the matrix tests the
        # default. Per-axis coverage lives in test_apptools / test_configutils.
        targets=("axis_probe.cpp",),
    ),
    "testprefix": ExamplePlan(
        extra_env={"TESTPREFIX": "timeout 5"},
    ),
    # ----- C++20 modules: backend support varies by example shape -----
    "cxx_modules": ExamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_split": ExamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_partitions": ExamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    "cxx_modules_import_std": ExamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
    # Header units: full cross-backend coverage thanks to upstream fix.
    "cxx_modules_header_units": _VANILLA,
}


# ---------------------------------------------------------------------------
# Example discovery + parametrize ID generation.
# ---------------------------------------------------------------------------


def _discover_examples_end_to_end() -> list[str]:
    """Return every directory name directly under the e2e examples dir."""
    examples_root = pathlib.Path(uth.e2e_dir())
    return sorted(p.name for p in examples_root.iterdir() if p.is_dir())


def _example_plan(example_name: str) -> ExamplePlan:
    """Look up an example's plan, defaulting to vanilla --auto.

    Unknown examples (added by a future PR without registering them
    here) get _VANILLA — the test will likely succeed if the new
    example is a normal --auto build, or fail with a useful diagnostic
    that points the author at this registry.
    """
    return _EXAMPLE_PLANS.get(example_name, _VANILLA)


# Where the four ``--cas-*dir`` flags point relative to the workspace
# (= gitroot). The CI / shared-build-server pattern is "cas under
# /cache/ct, sources under ~/checkouts/repo" — i.e. cas LIVES OUTSIDE
# THE GITROOT — and that's the layout the production fleet runs. The
# in-tree default (``<git_root>/cas-*dir/<variant>``) is the developer
# convenience case. Both must work; both must be exercised.
#
# Notably, several code paths only branch when cas and workspace are on
# different subtrees:
#
#   * ``BazelBackend._materialise_pch_stagings`` hardlinks cas-pchdir
#     entries into a workspace-local ``.ct-bazel-pch/``. With cas inside
#     the workspace, both endpoints share an inode-space; with cas
#     outside, the EXDEV-tolerant copy fallback can fire if the two
#     paths land on different filesystems.
#   * ``cas_publish.py``'s ``link()`` + ``rename()`` publish path falls
#     back to a symlink only on EXDEV — same EXDEV-only-when-outside
#     story.
#   * Path-canonical CAS keys (gitroot-relative hashing) are designed
#     specifically so an external cas survives moving the gitroot —
#     which means the "outside" layout is the exact case the design is
#     for.
#
# "outside" places the cas root as a sibling of the workspace under the
# same ``effective_tmp`` — outside the gitroot, but on the same
# (shared, for slurm) filesystem the workspace lives on. The
# inside-vs-outside-different-fs case isn't portable to a CI matrix
# without baking in host-specific mountpoint knowledge, so it's not
# parametrized here; the in-tree-default-behavior gap is what we're
# closing.
_CAS_LAYOUTS: tuple[str, ...] = ("inside", "outside")


def _matrix_id(example_name: str, backend_name: str, cas_layout: str) -> str:
    return f"{example_name}-{backend_name}-{cas_layout}"


def _matrix_params() -> Iterable[tuple[str, str, str]]:
    examples = _discover_examples_end_to_end()
    backends = available_backends()
    for example in examples:
        for backend in backends:
            for layout in _CAS_LAYOUTS:
                yield example, backend, layout


_MATRIX = list(_matrix_params())
_IDS = [_matrix_id(example, backend, layout) for example, backend, layout in _MATRIX]


def _resolve_cas_root(workspace: pathlib.Path, cas_layout: str) -> pathlib.Path:
    """Return the directory under which ``cas-{obj,pch,pcm,exe}dir/`` live.

    "inside" anchors the cas under the workspace (= gitroot) — the
    in-tree default behaviour. "outside" anchors it as a sibling of the
    workspace under the same ``effective_tmp``, simulating the
    ``--cas-*dir=/cache/ct`` shared-cache deployment pattern where cas
    lives entirely outside the gitroot.
    """
    if cas_layout == "inside":
        return workspace
    if cas_layout == "outside":
        cas_root = workspace.parent / "cas-external"
        cas_root.mkdir(parents=True, exist_ok=True)
        return cas_root
    raise ValueError(f"unknown cas_layout {cas_layout!r}")


# ---------------------------------------------------------------------------
# Build helpers.
# ---------------------------------------------------------------------------


def _build_argv(
    workspace: pathlib.Path,
    backend_name: str,
    plan: ExamplePlan,
    cas_root: pathlib.Path,
) -> list[str]:
    """Compose the ct-cake argv for a workspace, matching the per-CAS
    isolation pattern from test_ffile_prefix_map.

    ``cas_root`` is the parent of the four ``cas-*dir`` directories. It
    may be the workspace itself (in-tree default) or a sibling
    directory outside the workspace (production shared-cache pattern);
    see ``_resolve_cas_root``. ``--bindir`` and ``--diagnostics-dir``
    are intentionally always under the workspace — the bindir is part
    of the user-facing build product, and diagnostics are per-build
    debugging output, neither of which the CAS-outside-gitroot pattern
    relocates.
    """
    argv = [
        "ct-cake",
        f"--backend={backend_name}",
        f"--cas-objdir={cas_root}/cas-objdir",
        f"--bindir={workspace}/bin",
        f"--cas-pchdir={cas_root}/cas-pchdir",
        f"--cas-pcmdir={cas_root}/cas-pcmdir",
        f"--cas-exedir={cas_root}/cas-exedir",
        f"--diagnostics-dir={workspace}/diagnostics",
    ]
    argv.extend(plan.extra_args)
    if plan.targets:
        argv.extend(str(workspace / t) for t in plan.targets)
    else:
        argv.append("--auto")
    return argv


def _build_env(plan: ExamplePlan) -> dict[str, str]:
    """Build a clean environment for the subprocess.

    Strips host CXXFLAGS/CFLAGS/LDFLAGS/CPPFLAGS so the test isn't at
    the mercy of whatever the operator has exported, then layers on the
    example's own extra_env, then optionally PKG_CONFIG_PATH pointing at
    examples-features/pkgs/."""
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    if plan.needs_pkg_config:
        env["PKG_CONFIG_PATH"] = str(pathlib.Path(uth.example_path("pkgs")))
    env.update(plan.extra_env)
    return env


def _run_build(
    backend_name: str,
    workspace: pathlib.Path,
    plan: ExamplePlan,
    cas_root: pathlib.Path,
) -> subprocess.CompletedProcess:
    argv = _build_argv(workspace, backend_name, plan, cas_root)
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
@pytest.mark.parametrize(("example_name", "backend_name", "cas_layout"), _MATRIX, ids=_IDS)
def test_example_builds_with_backend(example_name, backend_name, cas_layout, tmp_path):
    """Build *example_name* with *backend_name* under *cas_layout*;
    assert the policy in ``_EXAMPLE_PLANS`` is honoured.

    Three outcomes per cell:

    * ``skip_reason`` set → ``pytest.skip`` with the registered reason.
    * ``xfail_reason`` set → expected to fail; an unexpected success
      fails the matrix so stale policy is removed deliberately.
    * default → the build must succeed (returncode == 0).

    The ``cas_layout`` axis exercises the cas-inside-gitroot (developer
    default) and cas-outside-gitroot (CI / shared-build-server)
    deployment patterns. See ``_CAS_LAYOUTS`` for the rationale.

    The backend tool must be on PATH (via ``requires_backend_tool``)
    and a functional compiler must be present.
    """
    plan = _example_plan(example_name)

    if plan.skip_reason:
        pytest.skip(plan.skip_reason)
    if backend_name in plan.skip_for_backends:
        pytest.skip(f"example {example_name} opted out of backend {backend_name}")
    if not uth._backend_tool_available(backend_name):
        pytest.skip(f"{backend_name} build tool not on PATH")
    if example_name == "cxx_modules_import_std" and not _toolchain_supports_import_std():
        pytest.skip(
            "toolchain does not ship a std-module source (needs gcc 15+ for bits/std.cc, or clang+libc++ for std.cppm)"
        )

    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        workspace = uth.copy_example_workspace(
            pathlib.Path(uth.e2e_dir()) / example_name,
            pathlib.Path(effective_tmp) / "ws",
        )
        cas_root = _resolve_cas_root(workspace, cas_layout)
        result = _run_build(backend_name, workspace, plan, cas_root)

        if plan.xfail_reason:
            if result.returncode == 0:
                pytest.fail(f"unexpected build success despite xfail policy: {plan.xfail_reason}")
            return  # expected failure

        assert result.returncode == 0, (
            f"ct-cake failed for example={example_name!r} backend={backend_name!r} "
            f"cas_layout={cas_layout!r}\n"
            f"argv: {_build_argv(workspace, backend_name, plan, cas_root)}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )


def test_xfail_policy_fails_on_unexpected_build_success(monkeypatch, tmp_path):
    """A stale expected-failure policy must fail the matrix, not hide as XFAIL."""
    module = sys.modules[__name__]
    plan = ExamplePlan(xfail_reason="intentional failure still expected")

    @contextlib.contextmanager
    def fake_shared_filesystem_tmpdir(backend_name, fallback_path):
        yield str(fallback_path)

    def fake_copy_example_workspace(src, dst):
        dst.mkdir(parents=True, exist_ok=True)
        return dst

    monkeypatch.setattr(compiletools.apptools, "get_functional_cxx_compiler", lambda: "/usr/bin/c++")
    monkeypatch.setattr(uth, "_backend_tool_available", lambda backend_name: True)
    monkeypatch.setattr(uth, "shared_filesystem_tmpdir", fake_shared_filesystem_tmpdir)
    monkeypatch.setattr(uth, "copy_example_workspace", fake_copy_example_workspace)
    monkeypatch.setattr(module, "_example_plan", lambda example_name: plan)
    monkeypatch.setattr(
        module,
        "_run_build",
        lambda backend_name, workspace, plan, cas_root: subprocess.CompletedProcess(
            args=("ct-cake",),
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    with pytest.raises(pytest.fail.Exception, match="unexpected build success") as raised:
        test_example_builds_with_backend("stale_xfail", "make", "inside", tmp_path)

    assert type(raised.value) is pytest.fail.Exception


# ---------------------------------------------------------------------------
# Drift guard: every directory under examples-end-to-end/ must be registered.
# ---------------------------------------------------------------------------


def test_every_example_has_a_plan():
    """Adding a new example without registering it in ``_EXAMPLE_PLANS``
    is fine — the unknown-example path defaults to vanilla --auto. But
    we want to *force the author to think about it* so that
    intentional-failure examples don't silently start failing the matrix
    or unbuildable fixture-only directories don't waste cycles.

    Asserting unconditionally here surfaces the question at PR time:
    "your new example isn't classified — pick a plan or document why
    --auto is right."
    """
    discovered = set(_discover_examples_end_to_end())
    registered = set(_EXAMPLE_PLANS)
    missing = sorted(discovered - registered)
    assert not missing, (
        "New examples without an entry in _EXAMPLE_PLANS:\n  "
        + "\n  ".join(missing)
        + "\nAdd an ExamplePlan() entry (use _VANILLA if `ct-cake --auto` is correct)."
    )
    stale = sorted(registered - discovered)
    assert not stale, "_EXAMPLE_PLANS references examples that no longer exist:\n  " + "\n  ".join(stale)
