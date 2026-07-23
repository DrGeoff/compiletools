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

import dataclasses
import functools
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable

import pytest

import compiletools.apptools
import compiletools.testhelper as uth
import compiletools.utils
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


@functools.lru_cache(maxsize=1)
def _toolchain_supports_stdlib_header_units() -> bool:
    """True iff the local C++ toolchain can build a header unit from libc++/libstdc++.

    ``import <vector>;`` requires the system standard library headers to be
    *modules-clean*. Termux/Android ships libc++ on top of Bionic, and
    Bionic's ``stdlib.h`` / ``sched.h`` declare types like ``locale_t`` /
    ``pid_t`` before including their canonical declaration headers --
    tolerated under preprocessor inclusion but rejected by clang's module
    builder, which compiles each header unit in isolation. Distros that
    use glibc are unaffected.

    Probes the live toolchain with a one-shot ``import <vector>;`` compile;
    the result is cached so the probe runs at most once per process. A
    ``False`` return skips the cxx_modules header-unit examples; this is a
    Bionic/libc++ packaging defect, not a ct-cake regression.
    """
    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "probe.cpp")
        with open(src, "w") as f:
            f.write("import <vector>;\nint main() { return 0; }\n")
        argv = compiletools.utils.split_command_cached(cxx) + [
            "-std=c++26",
            "-fmodules",
            "-fsyntax-only",
            "-x",
            "c++",
            src,
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0


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

# Named C++20 modules (interface units, partitions, split impl, import std) now
# build on EVERY backend, so this blocklist is empty (kept as a named symbol the
# module ExamplePlans reference, in case a future backend needs re-blocking).
# make/ninja/shake build them natively; cmake/bazel
# have no native module support, so ct-cake prebuilds the module artefacts
# (interface BMIs + objects AND implementation-unit objects) itself and feeds the
# native tool the prebuilt objects. `cxx_modules_header_units` was unblocked
# earlier by the upstream fix tracked by commit d30e2040.
_CXX_MODULES_NAMED_BACKENDS_BLOCKED = frozenset()

# Examples whose build path involves ``import <stdlib_header>;`` --
# either directly in main.cpp or transitively through a user header
# that includes a libc++ header. These need the system standard
# library to be modules-clean (see _toolchain_supports_stdlib_header_units).
_EXAMPLES_REQUIRING_STDLIB_HEADER_UNITS = frozenset(
    {
        "cxx_modules_header_units",
        "cxx_modules_header_unit_isystem",
        "cxx_modules_header_unit_pkg_config",
        "cxx_modules_transitive_header_unit",
    }
)

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
    # Two subprojects each with a main.cpp — the source-mirrored bindir
    # layout must build BOTH (they'd silently collide under a flat
    # layout). Existence/run assertions live in
    # test_basename_collision_example.py; this cell confirms the build
    # succeeds on every backend.
    "basename_collision": _VANILLA,
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
    "sudoku_tui": ExamplePlan(
        skip_reason=(
            "//#GIT= external-fetch example: needs a network clone of "
            "github.com/DrGeoff/sudoku, but this matrix is hermetic. "
            "Covered by test_e2e_sudoku_tui.py (skips when offline)."
        ),
    ),
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
    # Header unit reached only through a project-supplied -isystem path.
    # Regression guard for ``_resolve_system_header_abs_paths`` honouring
    # user include flags during gcc's cas-pcmdir mapper resolution; the
    # sample's ``${CONF_DIR}``-anchored ``-isystem`` is what real
    # projects (the user's reproducer) write. bazel and cmake sandbox
    # builds against the workspace and reject absolute ``-isystem``
    # paths pointing outside it; same hermeticity restriction already
    # documented for bazel PCH staging.
    "cxx_modules_header_unit_isystem": ExamplePlan(
        skip_for_backends=frozenset({"bazel", "cmake"}),
    ),
    # Sibling of the isystem case: the header unit's ``-isystem`` arrives
    # not from ``append-CXXFLAGS`` but from a ``//#PKG-CONFIG=extlib``
    # magic flag whose ``.pc`` exports ``-I<sample>/include`` (converted
    # to ``-isystem`` by ``filter_pkg_config_cflags``). Regression guard
    # for commit 77c8ce0c threading those per-source PKG-CONFIG-derived
    # ``-isystem`` paths into the gcc/clang header-unit precompile pre-pass
    # (``_header_unit_extra_system_includes``). The sample self-configures
    # via ``prepend-PKG-CONFIG-PATH = ${CONF_DIR}/extlib_pc`` in its
    # ct.conf, so it builds vanilla with no test-side PKG_CONFIG_PATH. Same
    # bazel/cmake hermeticity skip as the isystem sibling: their sandboxes
    # reject the absolute ``-isystem`` path pkg-config resolves to.
    "cxx_modules_header_unit_pkg_config": ExamplePlan(
        skip_for_backends=frozenset({"bazel", "cmake"}),
    ),
    # Transitive header unit: a consumer (main.cpp) whose ONLY module
    # interaction is a header unit (`import <vector>;`) reached through a
    # #include'd wrapper header (vecutil.h), never imported directly. The
    # sibling vecutil.cpp imports the header unit directly so the
    # precompile rule / gcc mapper entry exists; the bug being guarded is
    # the consumer compile not getting the header-unit consume flags
    # (clang) when the import is transitive-only. Builds with the
    # matrix's default compiler here; the gcc-vs-clang asymmetry is
    # exercised explicitly by
    # test_transitive_header_unit_builds_on_gcc_and_clang below.
    "cxx_modules_transitive_header_unit": _VANILLA,
    # The canonical CAS showcase: four terminal games (moonlander, snake,
    # invaders, breakout) plus a controls-free ASCII aquarium artwork, all
    # sharing one terminal facade in common/. Every game module is
    # interface/impl-split (.cppm interface + _impl.cpp impl unit). Snake,
    # invaders and breakout are multi-module — each decomposes at natural seams
    # into leaf modules under a re-exporting aggregate (snake: rng+world;
    # invaders: formation+bullet+field; breakout: bricks+arena); moonlander
    # stays a single cohesive module (lander.physics). Builds all five exes +
    # thirteen pure tests across subdirs; the facade's single PCH
    # (common/terminal.cpp) and single terminal.o compile once and serve every
    # game from the CAS, alongside a second compile-once object frontend.o (the
    # splash/ANSI scaffolding, no PCH) also shared by all five programs. Follows
    # the named-module blocklist so it tracks the same per-backend support as
    # the other module examples.
    "terminal_games": ExamplePlan(skip_for_backends=_CXX_MODULES_NAMED_BACKENDS_BLOCKED),
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
# filesystem the workspace lives on. The
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


# Heavy real-subprocess backends whose e2e cells contend under ``-n auto`` and
# flake intermittently: on this box ``nproc`` is large (~127), so nearly all of
# a backend's ~90 cells fire their bazel server at once,
# saturating the box. We bound the concurrency by
# pinning each backend's cells across ``_HEAVY_E2E_SHARDS`` xdist load-groups
# (requires ``--dist loadgroup``, set in pyproject addopts): tests in a group run
# on one worker, so at most ``_HEAVY_E2E_SHARDS`` of a backend's cells run
# concurrently (down from ~90) while the shards still parallelise. Full
# serialisation (1 group) would make ~90 cells run back-to-back and dominate
# wall-clock; sharding keeps the suite time roughly unchanged. The group names
# (``e2e-<backend>-<shard>``) are shared with
# test_test_exe_rebuild_on_upstream_change.py so the bound is global across both
# heavy-e2e files. Sharding is by a stable content hash (NOT ``hash()``, which is
# per-process salted and would make workers disagree on grouping).
_HEAVY_E2E_BACKENDS = frozenset({"bazel"})
_HEAVY_E2E_SHARDS = 8


def _heavy_e2e_group(backend_name: str, shard_key: str) -> str:
    shard = int(hashlib.sha1(shard_key.encode()).hexdigest(), 16) % _HEAVY_E2E_SHARDS
    return f"e2e-{backend_name}-{shard}"


def _matrix_param(example_name: str, backend_name: str, cas_layout: str):
    marks = ()
    if backend_name in _HEAVY_E2E_BACKENDS:
        group = _heavy_e2e_group(backend_name, f"{example_name}-{backend_name}-{cas_layout}")
        marks = (pytest.mark.xdist_group(group),)
    return pytest.param(
        example_name,
        backend_name,
        cas_layout,
        id=_matrix_id(example_name, backend_name, cas_layout),
        marks=marks,
    )


_PARAMS = [_matrix_param(*cell) for cell in _matrix_params()]


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
@pytest.mark.parametrize(("example_name", "backend_name", "cas_layout"), _PARAMS)
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
    # Examples that pull stdlib symbols through a header-unit import need
    # the local libc++/libstdc++ to be modules-clean. Termux/Android's
    # libc++ over Bionic is not (Bionic stdlib.h/sched.h declare
    # locale_t/pid_t before their canonical headers); probe and skip.
    if example_name in _EXAMPLES_REQUIRING_STDLIB_HEADER_UNITS and not _toolchain_supports_stdlib_header_units():
        pytest.skip(
            "toolchain's standard library headers are not modules-clean "
            "(Termux/Android Bionic + libc++); stdlib header-unit imports fail"
        )

    effective_tmp = str(tmp_path)
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


@uth.requires_functional_compiler
def test_bazel_outside_layout_regenerates_bmi_after_staging_wipe(tmp_path):
    """Regression: bazel + outside cas-pcmdir regenerates a gcc named-module
    interface BMI when its workspace-local staging is wiped.

    For the "outside" cas layout a gcc interface's ``.gcm`` BMI lives only in
    ``<workspace>/.ct-bazel-pcm/`` (not the CAS — bazel hermeticity forces
    workspace-local BMIs; only objects are CAS-shared). A rebuild with a warm
    (shared, outside) cas-objdir but a wiped ``.ct-bazel-pcm/`` used to skip the
    interface prebuild — it keyed only on the ``.o`` existing — leaving the BMI
    absent, so bazel failed on the missing declared input. The prebuild skip now
    requires the BMI side effect too and force-recompiles the interface when
    it is gone. The single-cold-build matrix cell cannot exercise this warm path.
    """
    backend_name = "bazel"
    if not uth._backend_tool_available(backend_name):
        pytest.skip("bazel build tool not on PATH")
    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if compiletools.apptools.compiler_kind(cxx) != "gcc":
        pytest.skip("the .gcm BMI side-effect regeneration path is gcc-specific")

    example_name = "cxx_modules"
    plan = _example_plan(example_name)
    effective_tmp = str(tmp_path)
    workspace = uth.copy_example_workspace(
        pathlib.Path(uth.e2e_dir()) / example_name,
        pathlib.Path(effective_tmp) / "ws",
    )
    # "outside" keeps cas-objdir (and the .o) warm across both builds while
    # the in-workspace .ct-bazel-pcm/ staging is wiped between them.
    cas_root = _resolve_cas_root(workspace, "outside")

    first = _run_build(backend_name, workspace, plan, cas_root)
    assert first.returncode == 0, (
        f"cold outside-layout build failed:\n--- stdout ---\n{first.stdout}\n--- stderr ---\n{first.stderr}"
    )

    staging = workspace / ".ct-bazel-pcm"
    assert staging.is_dir(), (
        "expected <workspace>/.ct-bazel-pcm/ after an outside-layout bazel "
        "build of a gcc named-module example (the BMI staging dir)"
    )
    shutil.rmtree(staging)

    second = _run_build(backend_name, workspace, plan, cas_root)
    assert second.returncode == 0, (
        "rebuild after wiping .ct-bazel-pcm/ failed: the gcc module-interface "
        "BMI was not regenerated (warm cas-objdir + wiped staging).\n"
        f"--- stdout ---\n{second.stdout}\n--- stderr ---\n{second.stderr}"
    )


def test_xfail_policy_fails_on_unexpected_build_success(monkeypatch, tmp_path):
    """A stale expected-failure policy must fail the matrix, not hide as XFAIL."""
    module = sys.modules[__name__]
    plan = ExamplePlan(xfail_reason="intentional failure still expected")

    def fake_copy_example_workspace(src, dst):
        dst.mkdir(parents=True, exist_ok=True)
        return dst

    monkeypatch.setattr(compiletools.apptools, "get_functional_cxx_compiler", lambda: "/usr/bin/c++")
    monkeypatch.setattr(uth, "_backend_tool_available", lambda backend_name: True)
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


@uth.skipif_e2e_unavailable(
    uth.wild_available,
    "wild linker not on PATH (install: cargo install --locked wild-linker)",
)
@pytest.mark.parametrize(
    ("driver", "variant"),
    [("clang++", "clang,wild"), ("g++", "gcc,wild")],
)
def test_terminal_games_links_with_wild(driver, variant, tmp_path):
    """terminal_games (the canonical multi-module example) links and builds
    with the wild linker, from its root, on both compilers.

    Skipped entirely when wild isn't installed. The gcc leg additionally
    skips on gcc < 16.1 (can't drive -fuse-ld=wild — that's the wild-B
    axis's job, exercised by the apptools unit tests). The clang leg skips
    when clang isn't installed.
    """
    if shutil.which(driver) is None:
        pytest.skip(f"{driver} not on PATH")
    if variant.startswith("gcc"):
        identity = compiletools.apptools._compiler_major_version(driver)
        if not identity or identity[0] != "gcc" or identity[1] < 16:
            pytest.skip("gcc < 16.1 cannot drive -fuse-ld=wild; covered by wild-B unit tests")

    effective_tmp = str(tmp_path)
    workspace = uth.copy_example_workspace(
        pathlib.Path(uth.e2e_dir()) / "terminal_games",
        pathlib.Path(effective_tmp) / "ws",
    )
    cas_root = workspace
    argv = [
        "ct-cake",
        f"--variant={variant}",
        "--backend=make",
        f"--cas-objdir={cas_root}/cas-objdir",
        f"--cas-pchdir={cas_root}/cas-pchdir",
        f"--cas-pcmdir={cas_root}/cas-pcmdir",
        f"--cas-exedir={cas_root}/cas-exedir",
        f"--bindir={workspace}/bin",
        "--auto",
    ]
    env = os.environ.copy()
    for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
        env.pop(var, None)
    result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"ct-cake failed for variant={variant!r}\n"
        f"argv: {argv}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
