"""Abstract base class for build backends.

A BuildBackend knows how to:
1. Take a BuildGraph (backend-agnostic) and produce a native build file
   (Makefile, build.ninja, CMakeLists.txt, etc.)
2. Execute the build using the native tool (make, ninja, cmake --build, etc.)

The base class provides `build_graph()` which populates a BuildGraph from the
Hunter/Namer dependency data. This is the shared logic across all backends.

Backends may implement a static ``add_arguments(cap)`` method to register
backend-specific CLI arguments (see MakefileBackend for an example).
"""

from __future__ import annotations

import abc
import hashlib
import itertools
import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from types import MappingProxyType

import compiletools.apptools
import compiletools.diagnostics
import compiletools.file_analyzer
import compiletools.filesystem_utils
import compiletools.git_utils
import compiletools.global_hash_registry
import compiletools.namer
import compiletools.test_framework
import compiletools.utils
import compiletools.wrappedos

# Re-exported so existing importers (bazel/cmake/makefile/ninja/trace
# backends, tests) and ``unittest.mock.patch`` targets that reference
# ``compiletools.build_backend.<name>`` keep resolving after the move to
# ``backend_command_args``. Binding (not copying) preserves object identity
# so patches intercept the same function objects the BuildBackend methods
# call. ``_COMPILE_ORDERING_INPUT_EXTS`` is bound back because the
# ``_ORDER_ONLY_DEP_FORBIDDEN_EXTS`` definition below (which stays here, as
# it is consumed by a BuildBackend method and the trace backend) derives
# from it. Names that are re-exported purely for external importers/tests —
# not referenced by code remaining in this module — carry F401 suppressions.
from compiletools.backend_command_args import (
    _COMPILE_ORDERING_INPUT_EXTS,
    _DETACHED_ARG_FLAGS,  # noqa: F401
    _LINK_ENVIRONMENT_VARS,  # noqa: F401
    CAS_PRODUCER_TYPES,
    ObjInfo,  # noqa: F401
    _link_environment_snapshot,
    _read_link_sig,
    _toposort_rules,
    _touch,
    _write_link_sig,
    aggregate_rule_sources,  # noqa: F401
    build_obj_info,  # noqa: F401
    cas_demoted_order_only,
    compute_link_signature,
    extract_copts,  # noqa: F401
    extract_include_paths,  # noqa: F401
    extract_linkopts,  # noqa: F401
    mangle_target_name,
    ordering_inputs_for_compile,  # noqa: F401
    split_compound_args,  # noqa: F401
)

# Re-exported so existing importers (bazel/cmake/makefile/ninja/trace
# backends, ``debug_pcm_hash_inputs``, tests) and ``unittest.mock.patch``
# targets that reference ``compiletools.build_backend.<name>`` keep resolving
# after the move to ``backend_cxx_modules``. Binding (not copying) preserves
# object identity so patches intercept the same function objects the
# BuildBackend methods call. ``_NAME_ESCAPE`` and the single-path wrapper
# ``_resolve_system_header_abs_path`` are referenced only via those external
# channels here (tests), hence the F401 suppressions; the remaining names are
# called by BuildBackend methods still living in this module.
from compiletools.backend_cxx_modules import (
    _NAME_ESCAPE,  # noqa: F401
    _cas_pcm_path,
    _extract_system_include_path_flags,
    _header_unit_arg,
    _header_unit_safe_stem,
    _module_pcm_filename,
    _pcm_command_hash,
    _resolve_system_header_abs_path,  # noqa: F401
    _resolve_system_header_abs_paths,
    _write_pcm_manifest,
)

# Re-exported so existing importers and ``unittest.mock.patch`` targets that
# reference ``compiletools.build_backend.<name>`` keep resolving after the move
# to ``backend_locking``. Binding (not copying) preserves object identity so
# patches intercept the same function objects the BuildBackend methods call.
# ``_build_lock_env_prefix`` / ``_native_flock_available`` are referenced only
# via those two channels here, hence the F401 suppressions.
from compiletools.backend_locking import (
    _build_lock_env_prefix,  # noqa: F401
    _native_flock_available,  # noqa: F401
    check_lock_helper_available,
    report_lock_helper_missing,
    wrap_compile_with_lock,
    wrap_link_with_lock,
)

# Re-exported so existing importers (bazel backend, tests) and
# ``unittest.mock.patch`` targets that reference
# ``compiletools.build_backend.<name>`` keep resolving after the move to
# ``backend_pch``. Binding (not copying) preserves object identity so
# patches intercept the same function objects the BuildBackend methods
# call. ``_PCHDIR_WARNED`` is the one-time-warning guard set that
# ``test_build_backend`` clears between cases; it travels with
# ``_warn_if_pchdir_not_cross_user_safe`` and is re-exported here so that
# test's ``.clear()`` / ``.discard()`` still mutate the live set the
# warning function reads. All these names are referenced only via those
# external channels from this module (the BuildBackend methods call them
# through the bound names), hence the F401 suppressions.
from compiletools.backend_pch import (
    _PCHDIR_WARNED,  # noqa: F401
    _gch_path,
    _is_under,
    _pch_command_hash,
    _pch_scope_macro_hash,
    _stage_pch_header_alongside_gch,
    _warn_if_pchdir_not_cross_user_safe,
    _write_pch_manifest,
    _write_pch_scope_diagnostic,  # noqa: F401
)

# Re-exported so existing importers (bazel/cmake/makefile/ninja/trace
# backends, cake, listbackends, tests) and ``unittest.mock.patch`` targets
# that reference ``compiletools.build_backend.<name>`` keep resolving after
# the move to ``backend_registry``. Binding (not copying) preserves object
# identity, which is load-bearing here: the concrete backend modules
# ``from compiletools.build_backend import register_backend`` and decorate
# into the SAME ``_REGISTRY`` dict, while ``get_backend_class`` /
# ``ensure_backends_registered`` (also re-exported here) read it back.
# ``backend_registry`` never imports ``build_backend`` at runtime (it only
# stores/returns backend classes), so this layering carries no cycle. The
# make / bazel CLI registrars are pulled in for ``makefile_backend`` /
# ``bazel_backend``. Names re-exported purely for external importers/tests —
# not referenced by code remaining in this module — carry F401 suppressions.
from compiletools.backend_registry import (
    _ALWAYS_AVAILABLE_BACKENDS,  # noqa: F401
    _BUILTIN_BACKEND_MODULES,  # noqa: F401
    _REGISTRY,  # noqa: F401
    _BackendT,  # noqa: F401
    _import_builtin_backend,  # noqa: F401
    _register_bazel_cli_arguments,  # noqa: F401
    _register_make_cli_arguments,  # noqa: F401
    available_backends,  # noqa: F401
    backend_tool_command,  # noqa: F401
    detect_available_backends,  # noqa: F401
    ensure_backends_registered,  # noqa: F401
    get_backend_class,  # noqa: F401
    is_backend_available,  # noqa: F401
    known_backend_names,  # noqa: F401
    register_backend,  # noqa: F401
    register_backend_cli_arguments,  # noqa: F401
)
from compiletools.build_graph import BuildGraph, BuildRule, RuleType, render_shell_recipe
from compiletools.locking import execute_compile_rule, execute_link_rule
from compiletools.magicflags import _HARD_ORDERINGS_KEY
from compiletools.test_framework import TestFramework

# Sentinel: read-only empty mapping used as the class-level default for the
# module-iface dict attrs in ``BuildBackend``. A bare ``= {}`` would alias a
# single dict across every BuildBackend instance, so a future subclass or
# test that does ``self._module_iface_obj[k] = v`` BEFORE __init__ runs
# would silently corrupt state seen by all instances. MappingProxyType makes
# that mutation attempt raise TypeError instead — production code re-binds
# the attribute to a per-instance dict in ``__init__``, so writes always
# target the instance dict, never the shared sentinel.
_EMPTY_STR_MAP: Mapping[str, str] = MappingProxyType({})


# Narrower subset: the BMI/PCH artefacts the *compiler* actually
# opens at compile time (gcc/clang reads .gch via -include and .pcm /
# .gcm via -fmodule-file= / -fmodule-mapper=). The bazel backend
# declares these as ``additional_compiler_inputs`` so bazel symlinks
# them into the action's exec root for sandbox/dependency-validation.
# Excludes .o (a link-rule input, not a compile-action input — would
# spuriously pull link object lists into compile actions) and .stamp
# (build-ordering marker only, never read by the compiler).
_BMI_PCH_ARTEFACT_EXTS = (".gch", ".pcm", ".gcm")


# Suffixes that disqualify a path from being a valid ``order_only_deps``
# entry. ``order_only_deps`` is reserved for *bucket directories* the
# prebuild loop / trace backend will ``mkdir``. A file-shaped entry
# (object, library, BMI, executable, stamp) signals that the producing
# rule should have been put in ``inputs`` (or its existence ought to
# be enforced via ``rule.inputs`` so the consumer recurses into the
# producer rule). Catching this contract violation up front gives a
# uniform diagnostic across every backend instead of the silent
# "mkdir clobbers the artefact path" failure that previously haunted
# trace_backend / cold-cache prebuild paths.
_ORDER_ONLY_DEP_FORBIDDEN_EXTS = _COMPILE_ORDERING_INPUT_EXTS + (
    ".obj",
    ".a",
    ".so",
    ".dylib",
    ".exe",
)


def _wild_b_link_argv(args) -> list[str]:
    """Return the ``-B<absolute-search-dir>`` argv for the wild-B axis, else [].

    Routed here (and appended AFTER the canonicalize_for_command pass in the
    link-rule builders) rather than through LDFLAGS so the absolute path is
    never rewritten to a target-relative form. The wild-B variant is already
    in the link key via ``canonical_bindir`` — the per-user wild path stays
    out of the cache-key payload.
    """
    search_dir = getattr(args, "_wild_b_search_dir", None)
    return [f"-B{search_dir}"] if search_dir else []


class BuildBackend(abc.ABC):
    """Abstract base class for build system backends."""

    # Class-level default so tests that bypass __init__ via ``__new__`` see a
    # safe value at link-rule construction time without needing to mock every
    # piece of state. Instances set the per-build value during __init__.
    _compile_used_libcxx: bool = False

    # Same rationale: class-level defaults for C++20 modules state so test
    # fixtures bypassing __init__ via __new__ can still read these without
    # AttributeError, and _prebuild_aux_artefacts can use plain attribute
    # access (matches the invariant documented in __init__).
    #
    # The dict-typed module-iface attrs default to a shared empty
    # MappingProxyType so reads (.values(), .get(), iteration, ``in``)
    # work on bypass-init test backends, but any attempt to mutate the
    # default — ``self._module_iface_obj[k] = v`` before __init__ ran —
    # raises TypeError instead of silently aliasing one dict across every
    # BuildBackend instance. __init__ re-binds each attribute to a fresh
    # per-instance ``dict``, so production writes always target the
    # instance dict.
    _module_compiler_kind: str | None = None
    _module_pcm_cache_root: str | None = None
    _module_pcm_dir: str | None = None
    _module_iface_obj: Mapping[str, str] = _EMPTY_STR_MAP
    _module_iface_pcm: Mapping[str, str] = _EMPTY_STR_MAP
    _module_iface_gcm: Mapping[str, str] = _EMPTY_STR_MAP
    # Objects of C++20 module IMPLEMENTATION units (``module M;`` in a .cpp).
    # A set of object paths (not a name->path map: several files may implement
    # one module). cmake/bazel prebuild these like interface units.
    _module_impl_obj: frozenset[str] = frozenset()
    _gcc_module_mapper_path: str | None = None

    def __init__(self, args, hunter, *, context=None):
        self.args = args
        self.hunter = hunter
        if context is not None:
            self.context = context
        elif hunter is not None:
            self.context = hunter.context
        else:
            # The BuildContext-mandatory refactor (commit e352d20c) requires
            # callers to thread a BuildContext through. Silently constructing
            # a fresh one here would let the backend's caches diverge from any
            # other component's caches — the exact bug the refactor existed
            # to prevent. Force the caller to be explicit.
            raise ValueError(
                "BuildBackend requires either hunter or context. Pass context=BuildContext() if you have no hunter."
            )
        self.namer = compiletools.namer.Namer(args, context=self.context)
        # Resolve once per backend instance; threaded into every cache-key
        # site so they share a single find_git_root() call (which itself is
        # cached but pays an os.getcwd() per call) and a single source of truth.
        # Captured at __init__: a mid-build os.chdir won't update this; every
        # production path constructs the backend with the build's cwd already set.
        self._anchor_root: str = compiletools.git_utils.find_git_root() or ""
        self._graph: BuildGraph | None = None
        self._dynamic_sources: set[str] = set()
        # C++20 modules state. Set here so every read site can use
        # plain attribute access; ``_create_compile_rules`` populates
        # them with their final values per build. Defensive ``getattr``
        # at read sites is no longer needed.
        self._module_compiler_kind: str | None = None
        self._module_pcm_cache_root: str | None = None
        self._module_pcm_dir: str | None = None
        # Re-bind to per-instance dicts. Declared as Mapping[str, str] at
        # the class level (with a MappingProxyType sentinel) so writes via
        # ``self._module_iface_obj[k] = v`` before __init__ ran would raise
        # TypeError instead of silently aliasing one dict across instances.
        # Production callers (_create_compile_rule, the bazel/ninja paths,
        # and test fixtures) write through the instance dict bound here.
        self._module_iface_obj = {}
        self._module_iface_pcm = {}
        self._module_iface_gcm = {}
        self._module_impl_obj = frozenset()
        self._header_unit_artefact: dict[str, str] = {}
        self._gcc_module_mapper_path: str | None = None
        self._gcc_header_unit_resolved: dict[str, list[str]] = {}
        self._build_imports_std_cached: bool | None = None
        self._compile_used_libcxx = False

        # Hard-fail if the user explicitly opted into legacy mtime semantics
        # but this backend can't deliver them. ``--use-mtime`` is a
        # make/ninja-only knob: only those two backends consume the prereq
        # list as a literal mtime comparison. A content-hash backend (bazel,
        # shake) or self-managed one (cmake) cannot deliver "touch the
        # source to force a rebuild" semantics — a touch without a content
        # change is invisible to their rebuild check — so silently ignoring
        # the opt-in would mislead the user about what their flag does.
        # ``is True`` (not truthy) so a MagicMock attribute on a stub
        # backend in tests doesn't trip the check.
        if getattr(args, "use_mtime", False) is True and not self._honors_use_mtime():
            raise ValueError(
                f"--use-mtime=True is not supported by the {self.name()!r} backend; only the "
                "'make' and 'ninja' backends honor it. Other backends use content-hash-based "
                "(bazel, shake) or self-managed (cmake) change detection, which cannot "
                "deliver mtime-based 'touch the source to force a rebuild' semantics. Drop "
                "--use-mtime (the CAS-only default) or switch to --backend=make / --backend=ninja."
            )

    @property
    def _timer(self):
        """Return the enabled BuildTimer from context, or None."""
        from compiletools.build_timer import get_timer

        return get_timer(self.context)

    @staticmethod
    @abc.abstractmethod
    def name() -> str:
        """Short identifier for this backend (e.g., 'make', 'ninja')."""

    @staticmethod
    @abc.abstractmethod
    def build_filename() -> str:
        """Default output filename (e.g., 'Makefile', 'build.ninja')."""

    @abc.abstractmethod
    def generate(self, graph: BuildGraph, output=None) -> None:
        """Write the native build file from the given BuildGraph.

        Args:
            graph: The build graph to render.
            output: A file-like object to write to. If None, writes to the
                backend's default file path.
        """

    def execute(self, target: str = "build") -> None:
        """Invoke the native build tool to execute the build.

        Handles the common template: runtests delegation, early exit when all
        outputs are current, backend-specific build, and link signature recording.
        Override this method entirely for backends with non-standard execution
        (e.g. ShakeBackend which uses its own build engine).
        """
        if target == "runtests":
            # Every backend runs its test rules during the build phase; a
            # standalone ``runtests`` request routes through those same native
            # test rules via _execute_build.
            self._execute_build("runtests")
            return
        if self._graph is not None and self._all_outputs_current(self._graph):
            return
        self._execute_build(target)
        if self._graph is not None:
            self._record_link_signatures(self._graph)

    @abc.abstractmethod
    def _execute_build(self, target: str) -> None:
        """Backend-specific build invocation (subprocess call to native tool)."""

    def _native_target_for(self, target: str) -> str:
        """Map the abstract ``build`` target onto the backend's aggregate phony.

        ``execute("build")`` must drive ``all``, not the bare ``build`` phony:
        ``build`` covers only link/library outputs, while ``all`` additionally
        depends on ``runtests`` -> every test rule, so the native ``-j``
        scheduler interleaves test runs with compilation. ``runtests`` and
        explicit targets pass through verbatim.
        """
        return "all" if target == "build" else target

    def _test_command_for(self, source: str, exe_path: str) -> tuple[list[str], TestFramework | None]:
        """Return ``(argv, framework)`` for invoking a test executable.

        ``argv`` includes TESTPREFIX parts and, when ``--test-xml-dir`` is set
        and a known unit-test framework is detected from *source*'s transitive
        header set, the framework-specific JUnit-XML emit argv appended after
        ``exe_path`` (so prefix tools forward the trailing argv to the child).
        ``framework`` is the detected ``TestFramework`` or ``None``.
        """
        cmd: list[str] = []
        testprefix = getattr(self.args, "TESTPREFIX", "")
        if testprefix:
            cmd.extend(testprefix.split())
        cmd.append(exe_path)

        if not getattr(self.args, "test_xml_dir", None):
            return cmd, None

        headers = [str(h) for h in self.hunter.header_dependencies(source)]
        framework = compiletools.test_framework.detect_framework(headers, source)
        if framework is None:
            if getattr(self.args, "verbose", 0) >= 1:
                print(
                    f"{source}: no known unit-test framework detected; skipping XML output",
                    file=sys.stderr,
                )
            return cmd, None

        cmd.extend(framework.xml_argv(self._xml_path_for(exe_path)))
        return cmd, framework

    def _touch_result_marker(self, result_path: str) -> None:
        """Touch a test's success marker. No-op (without error) if result_path
        is empty. Used by backends that run tests in-process and need to record
        success themselves (shake, bazel post-hoc)."""
        if not result_path:
            return
        _touch(result_path)

    def _module_iface_bmi_path(self, cas_gcm: str) -> str | None:
        """Absolute path a gcc named-module interface BMI (``.gcm``) is actually
        written at.

        Default: the cas-pcmdir path, where the default ``-fmodule-mapper``
        points. ``BazelBackend`` overrides this for its workspace-relative
        mapper (cas-pcmdir-resolved for the "inside" cas layout,
        ``<workspace>/.ct-bazel-pcm/`` for "outside") so the prebuild skip below
        checks the file gcc will really write.
        """
        return cas_gcm

    def _module_interface_bmi_by_output(self) -> dict[str, str]:
        """Map each gcc named-module INTERFACE object to the path its BMI
        (``.gcm``) side effect is written at.

        ``_prebuild_aux_artefacts`` consults this so a module interface rule is
        skipped only when BOTH its ``.o`` and its BMI are present -- the ``.o``
        alone is not enough, because the BMI is a *side effect* of the same
        compile and can go missing independently (e.g. a wiped bazel
        ``.ct-bazel-pcm/`` staging dir while the shared cas-objdir stays warm),
        and importer compiles then fail "failed to read compiled module".

        Empty for clang (its ``.pcm`` is a rule *output*, already
        existence-checked) and when no gcc module cache is active.
        """
        if self._module_compiler_kind != "gcc":
            return {}
        out: dict[str, str] = {}
        for name, obj in self._module_iface_obj.items():
            cas_gcm = self._module_iface_gcm.get(name)
            if cas_gcm is None:
                continue
            loc = self._module_iface_bmi_path(cas_gcm)
            if loc is not None:
                out[obj] = loc
        return out

    def _prebuild_aux_artefacts(self) -> None:
        """Locally execute aux artefact producer rules before the native backend runs.

        Backends that emit one ``cc_binary`` / ``add_executable`` per LINK
        rule (CMake, Bazel) hand source-to-binary compilation to the
        native tool, but the PCH, header-unit, and named-module interface
        producer rules sit outside that chain — the native tool never sees
        them. Running them here lands the artefacts on disk so the per-TU
        compile commands the native tool subsequently runs find them via the
        already-baked ``-fmodule-file=`` / ``-fmodule-mapper=`` /
        ``-include <pchdir>/<hash>/<basename>`` flags. Locking via ``atomic_compile`` /
        ``atomic_link`` lets peer ct-cake invocations sharing a CAS dir
        cooperate.

        Execution order for named-module interface rules: we topologically
        sort by ``rule.inputs`` within the interface-rule set so partitions
        are compiled before primary interfaces that import them (clang's
        precompile stage; gcc's -fmodule-mapper compile).
        """
        graph = self._graph
        if graph is None:
            return
        pch_rules = [r for r in graph.rules_by_type(RuleType.COMPILE) if r.output.endswith(".gch")]
        # clang header-unit precompiles are COMPILE rules with a .pcm output
        # (`--precompile -xc++-system-header`), not RuleType.HEADER_UNIT --
        # that type is reserved for gcc's shell-pipeline form whose output
        # can't ride on `-o`. Collect them via the artefact registry so
        # cmake/bazel (whose native tool never sees these rules) land the
        # .pcm before consumer TUs compile with `-fmodule-file=<h>=<pcm>`.
        # getattr fallback for
        # test backends that bypass __init__ (same as
        # _header_unit_extra_system_includes below).
        hu_artefacts = set(getattr(self, "_header_unit_artefact", {}).values())
        clang_hu_rules = [r for r in graph.rules_by_type(RuleType.COMPILE) if r.output in hu_artefacts]
        aux_rules = pch_rules + clang_hu_rules + graph.rules_by_type(RuleType.HEADER_UNIT)

        # Module compile rules to run locally before the native tool: named-
        # module INTERFACE objects (gcc .o, and clang .o from pcm-to-o stage)
        # or clang precompile .pcm outputs, PLUS module IMPLEMENTATION-unit
        # objects (`module M;`). We must NOT include _module_iface_gcm entries
        # separately -- those .gcm paths are side effects of the same gcc
        # interface compile rule whose .o is already in _module_iface_obj;
        # double-executing would corrupt the output. Implementation-unit
        # objects ARE each the sole output of their own rule, so they're safe
        # to add. Topological sort within this set ensures partitions and
        # interface units run before the implementation units / primary
        # interfaces that import them (impl rules carry the interface .o in
        # their inputs via _COMPILE_ORDERING_INPUT_EXTS).
        module_prebuilt_outputs: set[str] = (
            set(self._module_iface_obj.values()) | set(self._module_iface_pcm.values()) | set(self._module_impl_obj)
        )
        if module_prebuilt_outputs:
            prebuilt_rules_by_output: dict[str, BuildRule] = {}
            for rule in graph.rules_by_type(RuleType.COMPILE):
                if rule.output in module_prebuilt_outputs:
                    prebuilt_rules_by_output[rule.output] = rule
            module_prebuilt_rules = _toposort_rules(prebuilt_rules_by_output)
            aux_rules = module_prebuilt_rules + aux_rules

        if not aux_rules:
            return

        for rule in aux_rules:
            for d in rule.order_only_deps:
                # order_only_deps must be bucket dirs, not artefact paths.
                # The earlier "catch FileExistsError after mkdir" form only
                # tripped if the artefact already existed -- a cold cache
                # with a file-shaped order_only_dep would silently mkdir a
                # directory at the artefact's path, then the producer rule
                # would fail opaquely. Reject artefact suffixes up front so
                # the diagnostic fires identically whether or not the path
                # has been built yet, and so the same check protects every
                # backend that funnels through this prebuild loop.
                if d.endswith(_ORDER_ONLY_DEP_FORBIDDEN_EXTS):
                    raise AssertionError(
                        f"order_only_dep {d!r} on rule {rule.output!r} has an artefact "
                        f"suffix; order_only_deps must be bucket directories. Route "
                        f"artefact dependencies through `inputs` instead (see "
                        f"build_backend._wire_module_inputs)."
                    )
                try:
                    os.makedirs(d, exist_ok=True)
                except FileExistsError as e:
                    raise AssertionError(
                        f"order_only_dep {d!r} on rule {rule.output!r} is a file but must be a directory"
                    ) from e

        # gcc named-module interface rules write their BMI (.gcm) as a side
        # effect of producing the .o; the .o alone existing is not proof the
        # artefact is whole (see _module_interface_bmi_by_output).
        iface_bmi_by_output = self._module_interface_bmi_by_output()

        verbose = getattr(self.args, "verbose", 0)

        # Resolve probe paths against rule.cwd so relative outputs are checked
        # under the same cwd execute_compile_rule will run the rule with below.
        def _cwd_aware(path: str, cwd: str | None) -> str:
            if os.path.isabs(path) or not cwd:
                return path
            return os.path.join(cwd, path)

        for rule in aux_rules:
            # Pre-lock fast-path mirrors trace_backend._do_build:365-383.
            # The skip_if_exists below closes the TOCTOU window inside the
            # lock; this skip avoids the lock entirely on warm builds.
            output_exists = os.path.exists(_cwd_aware(rule.output, rule.cwd))
            bmi = iface_bmi_by_output.get(rule.output)
            bmi_missing = bmi is not None and not os.path.exists(_cwd_aware(bmi, rule.cwd))
            if output_exists and not bmi_missing:
                continue
            assert rule.command is not None, f"aux rule {rule.output} has no command"
            if verbose >= 1:
                print(" ".join(rule.command), file=sys.stderr)
            # Force a recompile (skip_if_exists=False) only when the .o is
            # cached but its BMI side effect is gone -- otherwise the
            # existing-.o skip inside execute_*_rule would no-op and leave the
            # BMI ungenerated. The .o rewrite is content-identical (temp+rename).
            skip_if_exists = not (output_exists and bmi_missing)
            if rule.rule_type == RuleType.COMPILE:
                # Forward rule.cwd so PCH / module-interface rules emitted
                # with cwd=anchor_root keep their workspace-relative source
                # resolution at execute time (matches trace_backend.py:536).
                # Without this, bazel/cmake prebuild paths run the compiler
                # from the wrong cwd whenever anchor_root != current cwd —
                # latent today because affected scenarios have matching
                # cwds, but a real defect once cas-pchdir/pcmdir is shared
                # across workspaces with subdir invocations.
                execute_compile_rule(rule.output, rule.command, self.args, skip_if_exists=skip_if_exists, cwd=rule.cwd)
            else:
                # gcc's shell-pipeline header-unit form does its own producer-side
                # rename inside the pipeline; atomic_link's outer rewrite no-ops
                # (emits a one-time warning) but the rule still runs correctly.
                # NOTE: execute_link_rule does not currently accept a cwd
                # kwarg (atomic_link runs in the parent's cwd). HEADER_UNIT
                # rules emitted here use absolute paths and do not set
                # BuildRule.cwd, so this is safe today. If a future rule
                # routed through this branch sets a non-None cwd, the
                # locking layer will need a cwd= kwarg extension symmetric
                # to atomic_compile.
                execute_link_rule(rule.output, list(rule.command), self.args, skip_if_exists=skip_if_exists)

    # Graph rule types whose outputs land under exe_dir: linked/library
    # artefacts, backend copy artifacts, and the published symlinks/hard
    # links (in CAS-only mode the LINK output is the cas-exedir path and
    # the SYMLINK output is the bindir publish path).
    _ARTEFACT_RULE_TYPES = (
        RuleType.LINK,
        RuleType.STATIC_LIBRARY,
        RuleType.SHARED_LIBRARY,
        RuleType.COPY,
        RuleType.SYMLINK,
    )

    def _rmtree_would_destroy_workspace(self, directory: str) -> bool:
        """True when recursively deleting *directory* would take the
        workspace with it.

        A bindir of ``.`` or ``..`` (legal, preserved by the parse-time
        normalization in apptools) makes ``executable_dir()`` resolve to
        the invocation cwd or an ancestor of it, so an unscoped rmtree
        there deletes sources and ``.git``, not just build artifacts.
        The check is ancestor-or-equal on realpaths against both the cwd
        and the gitroot.
        """
        # NOT wrappedos: clean runs post-build and the input may be a
        # chdir-relative path (wrappedos skip cases 2 and 3).
        real = os.path.realpath(directory)
        for protected in (os.getcwd(), self._anchor_root):
            if not protected:
                continue
            try:
                # NOT wrappedos: getcwd changes across chdir-heavy tests.
                if os.path.commonpath([real, os.path.realpath(protected)]) == real:
                    return True
            except ValueError:
                continue
        return False

    def _remove_tree_or_outputs(
        self, directory: str, rule_types: tuple[str, ...], graph: BuildGraph | None = None
    ) -> None:
        """rmtree *directory*, or fall back to per-output removal when the
        tree contains the workspace.

        The fallback removes this build's graph outputs of *rule_types*
        individually (the same scoping the Makefile clean recipe uses)
        and prunes the directories those removals emptied, stopping
        (exclusive) at *directory*. *graph* defaults to ``self._graph``
        (populated by ``generate()``).
        """
        # NOT wrappedos: post-build existence check.
        if not os.path.isdir(directory):
            return
        if not self._rmtree_would_destroy_workspace(directory):
            shutil.rmtree(directory)
            return
        if graph is None:
            graph = self._graph
        if graph is None:
            return
        # NOT wrappedos: chdir-relative post-build paths (skip cases 2 and 3).
        real = os.path.realpath(directory)
        for rule in graph.rules:
            if rule.rule_type not in rule_types:
                continue
            # NOT wrappedos: build-output existence check after the build ran.
            if os.path.isfile(rule.output):
                os.remove(rule.output)
            parent = os.path.dirname(rule.output)
            # NOT wrappedos: chdir-relative post-build paths.
            while parent and os.path.realpath(parent) != real:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)

    def clean(self) -> None:
        """Remove build artifacts. Override for backend-specific cleanup.

        Both directory removals refuse to rmtree a tree that contains the
        workspace (``--bindir=.`` / ``..``) and degrade to removing this
        build's graph outputs instead; see ``_remove_tree_or_outputs``.
        """
        exe_dir = self.namer.executable_dir()
        obj_dir = self.namer.object_dir()
        if obj_dir == exe_dir:
            self._remove_tree_or_outputs(exe_dir, self._ARTEFACT_RULE_TYPES + (RuleType.COMPILE,))
        else:
            self._remove_tree_or_outputs(exe_dir, self._ARTEFACT_RULE_TYPES)
            self._remove_tree_or_outputs(obj_dir, (RuleType.COMPILE,))

    def realclean(self, graph: BuildGraph) -> None:
        """Remove bin/ entirely and selectively clean this build's objects from the object CAS.

        Unlike clean(), which removes the entire exe_dir and obj_dir trees,
        realclean() only removes individual build products listed in the graph
        from the obj_dir.  This is important when obj_dir is a shared location
        (e.g. cas-objdir/) used by multiple sub-projects -- we must not
        destroy other sub-projects' objects.

        The exe_dir is removed entirely since it is per-project — unless it
        contains the workspace (``--bindir=.`` / ``..``), in which case only
        this build's artefact outputs are removed.
        """
        exe_dir = self.namer.executable_dir()
        self._remove_tree_or_outputs(exe_dir, self._ARTEFACT_RULE_TYPES, graph)

        # Selectively remove only this build's products from the objdir.
        # `compile` covers both .o and PCH .gch outputs (PCH rules are emitted
        # as compile rules in build_graph()). `copy` covers backend-emitted
        # copy artifacts. .gch files in a PCH CAS cache outside obj_dir
        # are intentionally NOT cleaned: that cache is cross-variant and may
        # be in use by peer builds; use ct-trim-cache to age them out.
        # Mirrors makefile_backend._write_clean_rules realclean recipe.
        obj_dir = self.namer.object_dir()
        if obj_dir != exe_dir and os.path.isdir(obj_dir):
            for rule in graph.rules:
                if rule.rule_type in (
                    RuleType.COMPILE,
                    RuleType.LINK,
                    RuleType.STATIC_LIBRARY,
                    RuleType.SHARED_LIBRARY,
                    RuleType.COPY,
                ):
                    target = rule.output
                    if os.path.isfile(target):
                        os.remove(target)
            # Prune empty subdirectories (bottom-up) to mirror the Makefile
            # `find -type d -empty -delete` step.
            for dirpath, dirnames, filenames in os.walk(obj_dir, topdown=False):
                if dirpath == obj_dir:
                    continue
                if not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass

    def _copy_built_executables(self, build_output_dir: str) -> None:
        """Copy built executables from a build output dir to namer paths.

        Walks build_output_dir recursively to find executables, matching
        them by name (original or mangled) back to source files.
        Backends that produce outputs in a non-standard location (e.g.
        bazel-bin/, cmake-build/) call this after a successful build.
        """
        all_sources = list(self.args.filename or []) + list(self.args.tests or [])
        source_by_name: dict[str, str] = {}
        basename_counts: dict[str, int] = {}
        for source in all_sources:
            stem = os.path.splitext(os.path.basename(source))[0]
            basename_counts[stem] = basename_counts.get(stem, 0) + 1
        aliases_by_source: dict[str, list[str]] = {}
        for source in all_sources:
            dest_path = self.namer.executable_pathname(compiletools.wrappedos.realpath(source))
            # The native tool names the artefact after the mangled target
            # (mirrored: appalpha__main); the bare basename stays as a
            # fallback only while it is unambiguous.
            target_name = self._target_name_for(dest_path)
            source_by_name[target_name] = source
            stem = os.path.splitext(os.path.basename(source))[0]
            aliases_by_source[source] = [target_name, stem]
            if basename_counts[stem] == 1:
                source_by_name[stem] = source

        for dirpath, dirs, files in os.walk(build_output_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not d.endswith(".runfiles")]
            for fname in files:
                full = os.path.join(dirpath, fname)
                if not (os.path.isfile(full) and os.access(full, os.X_OK)):
                    continue
                if fname.endswith(".cmake"):
                    continue
                if fname not in source_by_name:
                    continue
                source = source_by_name.pop(fname)
                for alias in aliases_by_source[source]:
                    source_by_name.pop(alias, None)
                dest_path = self.namer.executable_pathname(compiletools.wrappedos.realpath(source))
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                compiletools.filesystem_utils.atomic_copy(full, dest_path)

    def build_graph(self) -> BuildGraph:
        """Populate a BuildGraph from hunter/namer data.

        This is the backend-agnostic logic shared by all backends.
        Subclasses call this, then pass the result to generate().
        """
        self.hunter.huntsource()
        graph = BuildGraph()

        all_sources = []
        if self.args.filename:
            all_sources.extend(self.args.filename)
        if self.args.tests:
            all_sources.extend(self.args.tests)

        if not all_sources and not self.args.static and not self.args.dynamic:
            return graph

        all_compile_sources = set()
        for source in all_sources:
            complete = self.hunter.required_source_files(source)
            all_compile_sources.update(complete)

        library_compile_sources = set()
        if self.args.static:
            for source in self.args.static:
                library_compile_sources.update(self.hunter.required_source_files(source))
        if self.args.dynamic:
            for source in self.args.dynamic:
                library_compile_sources.update(self.hunter.required_source_files(source))
        all_compile_sources.update(library_compile_sources)

        self._plan_directories(graph)

        self._plan_pch_rules(graph, all_compile_sources)

        compiler_kind = self._init_module_state()

        gcc_cache_active = self._plan_module_prepass(graph, all_compile_sources, compiler_kind)

        self._plan_header_unit_prepass(graph, all_compile_sources, gcc_cache_active)

        # Generate the gcc module-mapper file now that every named
        # module's .gcm path and every header-unit resolution is known.
        # No-op when not gcc+cache.
        self._write_gcc_module_mapper()

        self._plan_compile_rules(graph, all_compile_sources)

        library_outputs = self._plan_link_and_publish_rules(graph)

        self._plan_test_rules(graph, library_outputs)

        return graph

    def _plan_directories(self, graph: BuildGraph) -> None:
        """Phase B: emit the base objdir + executable-dir mkdir rules and
        record ``self._dynamic_sources`` (sources needing ``-fPIC``).

        Runs only on the non-early-return path, so ``self._dynamic_sources``
        is set exactly when build_graph proceeds past the empty-target guard.
        Mutates *graph* in place.
        """
        # Create objdir creation rule (needed by compile rules as order-only dep)
        graph.add_rule(
            BuildRule(
                output=self.args.cas_objdir,
                inputs=[],
                command=["mkdir", "-p", self.args.cas_objdir],
                rule_type="mkdir",
            )
        )

        # Executable-dir creation rules (needed by link rules as order-only
        # deps). The mirrored layout gives each source directory its own
        # bindir subdir, so emit one mkdir per distinct artefact dir.
        exe_dirs = {self.namer.executable_dir()}
        for source in list(self.args.filename or []) + list(self.args.tests or []):
            exe_dirs.add(self.namer.executable_dir(compiletools.wrappedos.realpath(source)))
        for source in list(self.args.static or []) + list(self.args.dynamic or []):
            exe_dirs.add(self.namer.executable_dir(compiletools.wrappedos.realpath(source)))
        for exe_dir in sorted(exe_dirs):
            if exe_dir == self.args.cas_objdir:
                continue
            graph.add_rule(
                BuildRule(
                    output=exe_dir,
                    inputs=[],
                    command=["mkdir", "-p", exe_dir],
                    rule_type="mkdir",
                )
            )

        # Track which sources are used for dynamic libraries (need -fPIC)
        if self.args.dynamic:
            self._dynamic_sources = set()
            for source in self.args.dynamic:
                self._dynamic_sources.update(self.hunter.required_source_files(source))
        else:
            self._dynamic_sources = set()

    def _plan_pch_rules(self, graph: BuildGraph, all_compile_sources: set[str]) -> None:
        """Phases C+D: discover PCH headers from magic flags, emit one compile
        rule per header (CAS-keyed .gch when cas-pchdir is active), and the
        per-hash pchdir mkdir rules.

        Sets ``self._pch_gch_paths`` and ``self._pch_include_dirs`` (read by
        the compile phase). Mutates *graph* in place.
        """
        # Discover PCH headers from magic flags and create PCH compile rules.
        # When pchdir is configured, .gch files are placed in a shared
        # content-addressable cache: <pchdir>/<command_hash>/<header>.gch
        import stringzilla as sz

        pchdir = getattr(self.args, "cas_pchdir", None)
        self._pch_gch_paths: dict[str, str] = {}  # header_abs -> gch_output
        self._pch_include_dirs: dict[str, str] = {}  # header_abs -> -I dir

        if pchdir:
            _warn_if_pchdir_not_cross_user_safe(pchdir, getattr(self.args, "verbose", 0))

        pch_headers: set[str] = set()
        for filename in all_compile_sources:
            magicflags = self.hunter.magicflags(filename)
            for pch_header in magicflags.get(sz.Str("PCH"), []):
                pch_headers.add(str(pch_header))

        pch_mkdir_dirs: set[str] = set()
        for pch_header in sorted(pch_headers):
            pch_magicflags = self.hunter.magicflags(pch_header)
            magic_cpp_flags = pch_magicflags.get(sz.Str("CPPFLAGS"), [])
            magic_cxx_flags = pch_magicflags.get(sz.Str("CXXFLAGS"), [])

            if pchdir:
                # CXXFLAGS_TOKENS strips -D/-U; cmdline-D macros are
                # captured per-PCH-header via _pch_scope_macro_hash so
                # an irrelevant -DAPP_NAME=... change doesn't pollute
                # the cache key. Same fix applied to per-TU object
                # hashing in Hunter.macro_state_hash.
                #
                # Uses args.flags.hash_relevant("cxx") which strips -D/-U
                # AND filters diagnostic-only flags in one pass; _pch_command_hash
                # trusts its caller to pre-filter the cxxflags_tokens parameter.
                cxxflags_tokens = self.args.flags.hash_relevant("cxx")
                scope_macro_hash = _pch_scope_macro_hash(self.hunter, pch_header)
                cmd_hash = _pch_command_hash(
                    self.args,
                    pch_header,
                    magic_cpp_flags,
                    magic_cxx_flags,
                    cxxflags_tokens=cxxflags_tokens,
                    scope_macro_hash=scope_macro_hash,
                    anchor_root=self._anchor_root,
                )
            else:
                cmd_hash = None
            gch_path = _gch_path(pch_header, pchdir=pchdir, command_hash=cmd_hash)
            self._pch_gch_paths[pch_header] = gch_path
            if pchdir and cmd_hash:
                self._pch_include_dirs[pch_header] = os.path.join(pchdir, cmd_hash)
                pch_mkdir_dirs.add(os.path.join(pchdir, cmd_hash))
                transitive = sorted(str(d) for d in self.hunter.header_dependencies(pch_header))
                _write_pch_manifest(
                    pchdir=pchdir,
                    cmd_hash=cmd_hash,
                    pch_header=pch_header,
                    transitive_headers=transitive,
                    cxx_command=self.args.CXX,
                    context=self.context,
                    anchor_root=self._anchor_root,
                )
                # Stage a copy of the .h alongside the .gch so the consumer's
                # `-include <cache>/<basename>` directive resolves correctly
                # in every gcc fallback path (e.g. when bazel's rules_cc adds
                # `-U_FORTIFY_SOURCE` / `-fstack-protector` etc. that
                # invalidate the cached PCH at consume time, gcc would error
                # "No such file or directory" trying to open the bare .h).
                # Hardlink first (zero disk-cost), copy fallback if EXDEV.
                # Idempotent: safe across racing ct-cake invocations.
                _stage_pch_header_alongside_gch(
                    pch_header,
                    os.path.join(pchdir, cmd_hash, os.path.basename(pch_header)),
                )

            pch_deps = [pch_header] + sorted(str(d) for d in self.hunter.header_dependencies(pch_header))
            # Workspace-relative source path + cwd=anchor_root keeps gcc's PCH
            # internal path-table free of per-user absolute prefixes. Without
            # this, the precompiled header records the absolute source path
            # (e.g. /home/alice/proj/stdafx.h) in its DWARF include-dir table,
            # and any consumer (including a peer on a shared cas-pchdir) inherits
            # alice's paths into their .o files via .debug_line_str. Anchor-empty
            # (no git_root) falls back to the absolute path — N0 in the
            # design's choice space; cross-user PCH sharing isn't promised
            # without an anchor. -ffile-prefix-map handles this for the regular
            # compile path, but does not reach gcc's PCH path-table.
            #
            # Only relativize when the header lives UNDER anchor_root —
            # outside-of-anchor paths (vendored sources, system headers used
            # as PCH) would relativize to a fragile ``../../...`` chain that
            # adds no cross-user benefit (the absolute is already shared
            # across users in those locations) and would break tests that
            # assert the absolute path round-trips.
            if self._anchor_root and _is_under(pch_header, self._anchor_root):
                pch_source_for_cmd = os.path.relpath(pch_header, self._anchor_root)
                rule_cwd: str | None = self._anchor_root
            else:
                pch_source_for_cmd = pch_header
                rule_cwd = None
            pch_cmd = (
                compiletools.utils.split_command_cached(self.args.CXX)
                + list(self.args.flags.cxx)
                + [str(f) for f in magic_cpp_flags]
                + [str(f) for f in magic_cxx_flags]
                + ["-x", "c++-header", pch_source_for_cmd, "-o", gch_path]
            )
            order_deps = [os.path.join(pchdir, cmd_hash)] if pchdir and cmd_hash else [self.args.cas_objdir]
            graph.add_rule(
                BuildRule(
                    output=gch_path,
                    inputs=pch_deps,
                    command=pch_cmd,
                    rule_type="compile",
                    order_only_deps=order_deps,
                    cwd=rule_cwd,
                )
            )

        for pch_dir in sorted(pch_mkdir_dirs):
            graph.add_rule(
                BuildRule(
                    output=pch_dir,
                    inputs=[],
                    command=["mkdir", "-p", pch_dir],
                    rule_type="mkdir",
                )
            )

    def _init_module_state(self) -> str:
        """Phase E: initialise C++20-module backend state and return the
        compiler kind ("gcc"/"clang"/other).

        Sets ``self._module_compiler_kind``, ``self._module_pcm_cache_root``,
        ``self._gcc_module_mapper_path``, and ``self._module_pcm_dir``. The
        returned ``compiler_kind`` is threaded into the module pre-pass so its
        truthiness matches the original inline value exactly.
        """
        # C++20 modules pre-pass.
        #
        # We compute, per discovered module name, the artefact a downstream
        # importer needs to wait on:
        #   - GCC: the interface unit's object file. Compiling it under
        #     -fmodules-ts also writes gcm.cache/<name>.gcm as a side effect,
        #     so an order-only dep on that .o gates importers correctly.
        #   - Clang: the interface unit's pre-compiled module artefact.
        #     Clang's flow is two stages -- `--precompile` source->.pcm,
        #     then `-c` .pcm->.o -- and importers read the .pcm directly
        #     via `-fmodule-file=NAME=PATH`.
        #
        # Where the .pcm lives depends on whether the cas-pcmdir cache is
        # active (the default; mirrors cas-pchdir's content-addressed
        # store). With the cache, each module's .pcm path includes a
        # command_hash that summarises everything affecting the BMI
        # bytes, so identical configurations share a cache entry across
        # rebuilds. Without the cache, .pcm files land in a flat
        # per-build dir under cas-objdir.
        compiler_kind = compiletools.apptools.compiler_kind(self.args.CXX)
        self._module_compiler_kind = compiler_kind
        # cas-pcmdir is meaningful for both compilers now: clang stores
        # its own ``.pcm`` files there; gcc stores its ``.gcm`` files
        # there via ``-fmodule-mapper`` redirection.
        self._module_pcm_cache_root = (
            getattr(self.args, "cas_pcmdir", None) if compiler_kind in ("clang", "gcc") else None
        )
        # Per-build mapper file path. Set up-front (rather than lazily
        # in `_write_gcc_module_mapper`) so per-rule emitters can
        # reference it before the file is materialised. The file itself
        # is written at the end of the modules pre-pass once every
        # mapper entry is known.
        # Place the mapper next to the makefile rather than under
        # cas-objdir. cas-objdir is shared across every build that
        # targets the same variant; two parallel `ct-cake` invocations
        # writing to ``<cas-objdir>/.module-mapper.txt`` would race
        # (last-rename-wins, but a gcc subprocess from invocation A
        # could see invocation B's overwrite). The makefile path
        # (``args.makefilename``) is per-invocation-unique by build
        # config, so co-locating the mapper with it pins one mapper
        # per generated makefile and avoids the race entirely.
        # Fall back to cas-objdir when ``makefilename`` is unset OR is
        # a bare basename with no dirname component (some non-make
        # backends and a few integration-test fixtures).
        if compiler_kind == "gcc" and self._module_pcm_cache_root:
            mapper_dir = self.args.cas_objdir
            mf = getattr(self.args, "makefilename", None)
            if mf:
                d = os.path.dirname(mf)
                if d:
                    mapper_dir = d
            self._gcc_module_mapper_path = os.path.join(mapper_dir, ".module-mapper.txt")
        else:
            self._gcc_module_mapper_path = None
        # The flat fallback dir is still computed even when cache is on
        # so that header-unit precompile rules (which currently bypass
        # the cache for simplicity) have somewhere to land. Header-unit
        # caching can be added later by mirroring the cmd_hash machinery
        # below.
        self._module_pcm_dir = os.path.join(self.args.cas_objdir, ".pcm") if compiler_kind == "clang" else None

        return compiler_kind

    def _plan_module_prepass(self, graph: BuildGraph, all_compile_sources: set[str], compiler_kind: str) -> bool:
        """Phase F: scan interface/impl units, populate the ``_module_iface_*``
        / ``_module_impl_obj`` state, emit per-hash pcm-dir mkdir rules, and
        return ``gcc_cache_active``.

        ``compiler_kind`` is threaded from :meth:`_init_module_state`; the
        returned ``gcc_cache_active`` flows into the header-unit pre-pass.
        Mutates *graph* in place.
        """
        module_iface_obj: dict[str, str] = {}
        module_iface_pcm: dict[str, str] = {}  # populated only for clang
        module_iface_gcm: dict[str, str] = {}  # populated for gcc + cache
        module_impl_obj: set[str] = set()  # objects of `module M;` impl units
        # Track the set of per-hash directories needing an mkdir rule
        # (cache mode) plus the flat fallback dir (always when clang
        # has any interface to precompile).
        pcm_mkdir_dirs: set[str] = set()
        gcc_cache_active = compiler_kind == "gcc" and bool(self._module_pcm_cache_root)
        for filename in all_compile_sources:
            iface_result = self.hunter._file_analysis_result(filename)
            if iface_result is None:
                continue
            for name in iface_result.module_exports:
                # Compute this source's object path the same way
                # _create_compile_rule does, so the order-only dep names
                # the same target the rule actually produces.
                module_iface_obj[name] = self._object_pathname_for_source(filename)
                if self._module_pcm_dir is not None:
                    pcm_path, pcm_dir = self._clang_module_pcm_destination(filename, name)
                    module_iface_pcm[name] = pcm_path
                    pcm_mkdir_dirs.add(pcm_dir)
                if gcc_cache_active:
                    gcm_path, gcm_dir = self._gcc_module_gcm_destination(filename, name)
                    module_iface_gcm[name] = gcm_path
                    pcm_mkdir_dirs.add(gcm_dir)
            # Module implementation units (`module M;`) produce only a .o (no
            # BMI side-effect). Record the object so cmake/bazel prebuild it
            # alongside interface units rather than recompiling it natively
            # (the native tool can't drive gcc's module mapper for it).
            if iface_result.module_implements:
                module_impl_obj.add(self._object_pathname_for_source(filename))

        self._module_iface_obj = module_iface_obj
        self._module_iface_pcm = module_iface_pcm
        self._module_iface_gcm = module_iface_gcm
        self._module_impl_obj = frozenset(module_impl_obj)

        for pcm_dir in sorted(pcm_mkdir_dirs):
            graph.add_rule(
                BuildRule(
                    output=pcm_dir,
                    inputs=[],
                    command=["mkdir", "-p", pcm_dir],
                    rule_type="mkdir",
                )
            )

        return gcc_cache_active

    def _plan_header_unit_prepass(
        self, graph: BuildGraph, all_compile_sources: set[str], gcc_cache_active: bool
    ) -> None:
        """Phase G: aggregate every ``import <h>;`` token across the build,
        emit one deduplicated precompile rule per token plus its mkdir, and
        populate the header-unit state dicts importer rules read.

        Sets ``self._header_unit_artefact``, ``self._gcc_header_unit_resolved``,
        and ``self._header_unit_extra_system_includes``. Mutates *graph* in
        place. ``gcc_cache_active`` is threaded from the module-state phase so
        its truthiness is identical to the original inline computation.
        """
        # Header units pre-pass.
        #
        # We aggregate every unique `import <h>;` / `import "h";` token
        # appearing across the build, then emit one precompile rule per
        # token (deduplicated). Each header unit's artefact path is
        # stored in `_header_unit_artefact[token]` so importer rules can
        # depend on it (order-only) and clang importers can pass
        # `-fmodule-file=<token>=<path>`.
        #
        # gcc puts the .gcm at `gcm.cache/<absolute-resolved-path>.gcm`
        # which we can't predict (it depends on -I/include resolution).
        # We give make a stamp file as the rule output and rely on `&&
        # touch <stamp>` (success_marker) to record success after gcc
        # writes the .gcm in its own location. The importer's actual
        # `import <h>;` resolves the .gcm via the same include path, so
        # the missing predictability is fine -- the stamp only sequences
        # the work, not the consumption.
        #
        # clang produces a real .pcm at a path we choose, so its
        # artefact IS the .pcm and there's no stamp shenanigan.
        header_unit_flat_dir = os.path.join(self.args.cas_objdir, ".hu")
        self._header_unit_artefact = {}  # rebind: per-build_graph state
        # Per-token absolute-header-path resolution (gcc only; populated
        # when gcc + cas-pcmdir are both active so the mapper file can
        # key entries by resolved path). Stores ALL spellings the
        # compiler may use as a lookup key -- canonical and
        # non-canonical -- because bazel's autoconfig appends
        # ``-fno-canonical-system-headers`` after our cxxopts and the
        # mapper has to hit under either flag set. Order is canonical
        # first for stability.
        self._gcc_header_unit_resolved: dict[str, list[str]] = {}
        all_header_imports: set[str] = set()
        # Union of per-source magic CPPFLAGS / CXXFLAGS system-include
        # tokens (``-isystem`` / ``-isysroot`` / ``-iframework`` /
        # ``-idirafter`` / ``--sysroot=``). The header-unit precompile
        # path otherwise only walks ``args.flags.cxx``, so a header
        # reached only through a per-source ``//#PKG-CONFIG=lib`` magic
        # flag (which ``magicflags._handle_pkg_config`` expands to
        # ``-isystem <pkg-include>``) would never become resolvable --
        # gcc fails with ``fatal error: <h>: No such file or directory``.
        # Same allowed flag families as ``args.flags.cxx`` because the
        # ``-isystem`` immutability contract still applies (see
        # ``_extract_system_include_path_flags``). Order-preserving
        # dedup so the precompile probe sees a stable flag list.
        import stringzilla as sz

        magic_system_includes: list[str] = []
        _seen_magic_si: set[str] = set()

        def _add_magic_si_tokens(tokens: tuple[str, ...]) -> None:
            for tok in tokens:
                if tok not in _seen_magic_si:
                    _seen_magic_si.add(tok)
                    magic_system_includes.append(tok)

        for filename in all_compile_sources:
            r = self.hunter._file_analysis_result(filename)
            if r is None:
                continue
            all_header_imports.update(r.module_header_imports)
            # A header unit reached only through a #include'd header still
            # needs a precompile rule + (gcc) mapper entry + artefact, so a
            # transitive-only consumer can resolve it. Symmetric with the
            # transitive named-module handling in _compiler_module_flags_for.
            all_header_imports.update(self._transitive_header_unit_imports(filename))
            # Gather per-source magic system-include flags. ``magicflags``
            # may raise on synthetic / non-existent paths in tests; the
            # TU compile path tolerates that downstream, but here the
            # pre-pass would crash the whole build. Fall back to skipping
            # this source rather than aborting -- the consumer's own
            # compile will surface a clearer error.
            try:
                mflags = self.hunter.magicflags(filename)
            except Exception:
                continue
            magic_cpp = [str(t) for t in mflags.get(sz.Str("CPPFLAGS"), [])]
            magic_cxx = [str(t) for t in mflags.get(sz.Str("CXXFLAGS"), [])]
            if magic_cpp:
                _add_magic_si_tokens(_extract_system_include_path_flags(magic_cpp))
            if magic_cxx:
                _add_magic_si_tokens(_extract_system_include_path_flags(magic_cxx))
        self._header_unit_extra_system_includes: tuple[str, ...] = tuple(magic_system_includes)
        if all_header_imports:
            # Determine the per-token destination dir up front so the
            # mkdir set is exactly the dirs we'll actually write into.
            # gcc routes through cas-pcmdir via the mapper file when the
            # cache is active; otherwise it falls back to a stamp under
            # the flat dir. clang routes its .pcm into the cas-pcmdir
            # cache when active, mirroring named modules.
            hu_mkdirs: set[str] = set()
            hu_destinations: dict[str, str] = {}
            for token in sorted(all_header_imports):
                dest_path, dest_dir = self._header_unit_destination(token, header_unit_flat_dir)
                hu_destinations[token] = dest_path
                hu_mkdirs.add(dest_dir)
                if gcc_cache_active:
                    # Pass the user's -std= so the dep walk speaks the
                    # same language the actual precompile will. Otherwise
                    # a gcc that rejects (say) -std=c++23 silently drops
                    # the mapper entry and the cache misses.
                    std_flag = next(
                        (t for t in self.args.flags.cxx if str(t).startswith("-std=")),
                        "-std=c++20",
                    )
                    # Pass the user's system-include flags so headers
                    # routed via ``-isystem`` / ``-isysroot`` /
                    # ``-iframework`` / ``-idirafter`` / ``--sysroot=``
                    # actually resolve. ``-I`` / ``-iquote`` are
                    # intentionally excluded -- see the immutability
                    # contract in :func:`_extract_system_include_path_flags`
                    # and ``src/compiletools/CLAUDE.md`` ("header-unit
                    # -isystem immutability contract"). Without any
                    # include flags at all, headers reached only
                    # through a project-supplied ``-isystem`` path
                    # would leave ``_gcc_header_unit_resolved`` empty
                    # and the precompile would silently misroute
                    # through the global-mapper path -- gcc reports
                    # the import as "unknown compiled module interface".
                    include_flags = (
                        _extract_system_include_path_flags(self.args.flags.cxx)
                        + self._header_unit_extra_system_includes
                    )
                    abs_paths = _resolve_system_header_abs_paths(
                        self.args.CXX,
                        token,
                        std_flag=str(std_flag),
                        include_flags=include_flags,
                    )
                    if abs_paths:
                        self._gcc_header_unit_resolved[token] = abs_paths
            for d in sorted(hu_mkdirs):
                graph.add_rule(
                    BuildRule(
                        output=d,
                        inputs=[],
                        command=["mkdir", "-p", d],
                        rule_type="mkdir",
                    )
                )
            for token in sorted(all_header_imports):
                rule = self._create_header_unit_precompile_rule(token, hu_destinations[token])
                graph.add_rule(rule)
                # Importers wait on this artefact path -- the .gcm
                # cache path for gcc+cache, the stamp for gcc no-cache,
                # the .pcm for clang.

    def _plan_compile_rules(self, graph: BuildGraph, all_compile_sources: set[str]) -> None:
        """Phase I: emit per-source compile rules (clang module interface
        two-rule split, or the plain compile rule) plus the per-used-bucket
        objdir mkdir rules. Mutates *graph* in place.

        Reads ``self._module_pcm_dir`` (set by the module-state init phase).
        """
        compile_bucket_dirs: set[str] = set()
        for filename in all_compile_sources:
            file_result = self.hunter._file_analysis_result(filename)
            module_exports = file_result.module_exports if file_result is not None else ()

            # For a clang interface unit we emit two rules: precompile
            # source -> .pcm, then compile .pcm -> .o. Both are needed:
            # the .pcm satisfies importers' -fprebuilt-module-path lookup;
            # the .o supplies the symbols at link time.
            if self._module_pcm_dir is not None and module_exports and len(module_exports) == 1:
                pcm_rule, obj_rule = self._create_clang_module_interface_rules(filename, module_exports[0])
                # The precompile rule itself needs to wait for any
                # partitions it imports (`export import :P;` in the
                # primary, or `import :P;` in another partition). Without
                # this edge clang fails the precompile with "module file
                # not found" since the partition .pcm hasn't been built.
                self._wire_module_inputs(pcm_rule, file_result, filename=filename)
                graph.add_rule(pcm_rule)
                self._wire_module_inputs(obj_rule, file_result, filename=filename)
                graph.add_rule(obj_rule)
                if obj_rule.order_only_deps:
                    compile_bucket_dirs.add(obj_rule.order_only_deps[0])
                continue
            elif self._module_pcm_dir is not None and len(module_exports) > 1:
                # A single TU exporting multiple module names is rare and
                # the build graph above only records the first .pcm path
                # anyway. Treat this as an error rather than silently
                # producing a confused build.
                raise ValueError(
                    f"{filename}: clang module rule emission expects at most one "
                    f"`export module NAME;` per TU; saw {list(module_exports)}"
                )

            rule = self._create_compile_rule(filename)
            self._wire_module_inputs(rule, file_result, filename=filename)
            graph.add_rule(rule)
            if rule.order_only_deps:
                compile_bucket_dirs.add(rule.order_only_deps[0])

        # One mkdir rule per *used* bucket, not per possible bucket.
        # Cold-cache cost is sub-100 ms total on local FS; avoiding the
        # unused 156-200 buckets keeps directory metadata operations
        # proportional to source breadth and stays cheap on shared
        # filesystems too.
        for bucket_dir in sorted(compile_bucket_dirs):
            if bucket_dir == self.args.cas_objdir:
                continue  # already covered by the bare-objdir mkdir above
            graph.add_rule(
                BuildRule(
                    output=bucket_dir,
                    inputs=[],
                    command=["mkdir", "-p", bucket_dir],
                    rule_type="mkdir",
                )
            )

    def _check_executable_collisions(self) -> None:
        """Raise when two distinct targets map to one output path.

        The mirrored bindir layout makes cross-directory basename
        collisions impossible, but same-directory stems (``main.cpp`` +
        ``main.c``) still share ``<dir>/main``. ``BuildGraph.add_rule``
        is last-write-wins, so without this check one link rule would
        silently vanish: one binary never built, exit code still 0.
        """
        candidates: list[tuple[str, str]] = []
        for source in list(self.args.filename or []) + list(self.args.tests or []):
            real = compiletools.wrappedos.realpath(source)
            candidates.append((self.namer.executable_pathname(real), real))
        if self.args.static:
            real = compiletools.wrappedos.realpath(self.args.static[0])
            candidates.append((self.namer.staticlibrary_pathname(real), real))
        if self.args.dynamic:
            real = compiletools.wrappedos.realpath(self.args.dynamic[0])
            candidates.append((self.namer.dynamiclibrary_pathname(real), real))

        output_to_source: dict[str, str] = {}
        for output, real in candidates:
            prior = output_to_source.get(output)
            if prior is not None and prior != real:
                remedy = (
                    "Rename one source, or drop --force-flat-exe-layout to restore "
                    "the source-mirrored layout that separates them."
                    if getattr(self.args, "force_flat_exe_layout", False)
                    else "Rename one source — same-directory sources sharing a stem map to a single output path."
                )
                raise ValueError(
                    f"Executable output collision: {output!r} would be produced by both "
                    f"{prior!r} and {real!r}. {remedy}"
                )
            output_to_source[output] = real

        # A strict path-prefix collision (``foo.cpp`` -> ``<bindir>/foo``
        # the file, ``foo/bar.cpp`` -> ``<bindir>/foo/bar`` needing
        # directory ``<bindir>/foo``) fails natively with an opaque "Not
        # a directory". Sorting by path components makes a strict prefix
        # adjacent to its extensions (plain string sort would not:
        # ``bin/foo-x`` sorts between ``bin/foo`` and ``bin/foo/bar``),
        # so one adjacent-pair pass catches every such collision.
        outputs = sorted(output_to_source, key=lambda p: p.split(os.sep))
        for shorter, longer in itertools.pairwise(outputs):
            if longer.startswith(shorter + os.sep):
                raise ValueError(
                    f"Executable output collision: {shorter!r} (from "
                    f"{output_to_source[shorter]!r}) is needed as a directory for {longer!r} "
                    f"(from {output_to_source[longer]!r}). Rename one source — a source file "
                    f"and a source directory sharing a name map to conflicting output paths."
                )

    def _target_name_for(self, output_path: str) -> str:
        """Native-tool (cmake/bazel) target name for a bindir artefact.

        Derived from the bindir-relative path with ``__`` joining path
        components, so mirrored same-basename outputs get distinct
        target names while root-level artefacts keep their bare names.
        The ``__`` join can alias (``a__b/main`` vs ``a/b__main``);
        aliases raise rather than silently merging two native targets.
        """
        exe_dir = self.namer.executable_dir()
        rel = os.path.relpath(output_path, exe_dir)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            # Output outside the bindir (custom rule paths); the bare
            # basename matches what the native tool would use anyway.
            rel = os.path.basename(output_path)
        name = mangle_target_name(rel.replace(os.sep, "__"))
        registry = getattr(self, "_target_names_registry", None)
        if registry is None:
            registry = {}
            self._target_names_registry = registry
        prior = registry.get(name)
        if prior is not None and prior != output_path:
            raise ValueError(
                f"Target-name alias: outputs {prior!r} and {output_path!r} both "
                f"mangle to native target name {name!r}. Rename one source directory."
            )
        registry[name] = output_path
        return name

    def _plan_link_and_publish_rules(self, graph: BuildGraph) -> list[str]:
        """Phase J: emit static/shared library, link, publish-symlink, and
        cas-exedir bucket mkdir rules.

        Returns ``library_outputs`` — the user-facing publish paths (symlink
        rule output, or legacy direct output) the ``build`` phony depends on.
        Mutates *graph* in place.
        """
        self._check_executable_collisions()
        # All three artefact-producing helpers (link / static_library /
        # shared_library) now return list[BuildRule]: in CAS-only mode
        # the list is [producer-rule, publish-symlink-rule]; in
        # native-CAS-backend mode it's a single legacy rule. The
        # ``library_outputs`` set tracks the user-facing publish path
        # (symlink rule output, or the legacy direct output) so the
        # link rule can build ``-l<name>`` references that downstream
        # consumers can resolve via ``-L<exe_dir>``.
        library_outputs: list[str] = []
        cas_exe_bucket_dirs: set[str] = set()

        def _add_artefact_rules(rules: list[BuildRule], producer_types: tuple[str, ...]) -> str:
            """Add *rules* to the graph and return the user-facing output
            path (the symlink rule's output if present, else the lone
            producer rule's output). Producer-rule order_only_deps are
            harvested into ``cas_exe_bucket_dirs`` for the cas-exedir
            mkdir loop below.
            """
            user_facing_output: str | None = None
            for r in rules:
                graph.add_rule(r)
                if r.rule_type in producer_types:
                    cas_exe_bucket_dirs.update(r.order_only_deps)
                if r.rule_type == RuleType.SYMLINK:
                    user_facing_output = r.output
            if user_facing_output is None:
                user_facing_output = rules[-1].output
            return user_facing_output

        if self.args.static:
            library_outputs.append(_add_artefact_rules(self._create_static_library_rule(), ("static_library",)))
        if self.args.dynamic:
            library_outputs.append(_add_artefact_rules(self._create_shared_library_rule(), ("shared_library",)))

        if self.args.filename:
            for source in self.args.filename:
                _add_artefact_rules(self._create_link_rule(source, library_outputs=library_outputs), ("link",))

        if self.args.tests:
            for source in self.args.tests:
                _add_artefact_rules(self._create_link_rule(source, library_outputs=library_outputs), ("link",))

        # Per-bucket mkdir for the cas-exedir tree, mirroring the per-bucket
        # mkdir loop above for cas-objdir. Only emit for buckets actually
        # used by producer rules (small set: usually one bucket per artefact).
        # Skip the bare ``cas_exedir`` root — covered by the user/CI's
        # general directory-create dance, not by per-rule mkdir.
        for bucket_dir in sorted(cas_exe_bucket_dirs):
            if not bucket_dir:
                continue
            graph.add_rule(
                BuildRule(
                    output=bucket_dir,
                    inputs=[],
                    command=["mkdir", "-p", bucket_dir],
                    rule_type="mkdir",
                )
            )

        return library_outputs

    def _plan_test_rules(self, graph: BuildGraph, library_outputs: list[str]) -> None:
        """Phase K: emit the ``build`` phony, per-test execution rules, the
        ``runtests`` phony, and the top-level ``all`` aggregate.

        ``library_outputs`` (produced by the link/publish phase) feeds the
        ``build`` phony's prerequisites. Mutates *graph* in place.
        """
        build_deps = []
        if self.args.filename:
            build_deps.extend(
                self.namer.executable_pathname(compiletools.wrappedos.realpath(s)) for s in self.args.filename
            )
        test_exe_paths = []
        if self.args.tests:
            test_exe_paths = [
                self.namer.executable_pathname(compiletools.wrappedos.realpath(s)) for s in self.args.tests
            ]
            build_deps.extend(test_exe_paths)
        build_deps.extend(library_outputs)
        graph.add_rule(BuildRule(output="build", inputs=build_deps, command=None, rule_type="phony"))

        all_deps = ["build"]

        if test_exe_paths:
            # Create per-test execution rules so build files can run tests standalone
            cas_only_results = not getattr(self.args, "use_mtime", False) and not self._self_manages_exe_placement()
            # When --test-xml-dir is set the test recipes pass
            # ``--gtest_output=xml:<dir>/<exe>.xml`` (etc.) to the test exe.
            # Emit an explicit mkdir rule for that directory and hang every
            # test rule off it as an order-only dep. All per-test XML files
            # share the single ``<xml-dir>/<variant>`` directory.
            xml_bucket_dir = ""
            if getattr(self.args, "test_xml_dir", None):
                # dir component of _xml_path_for is exe-independent, so [0] is representative
                xml_bucket_dir = os.path.dirname(self._xml_path_for(test_exe_paths[0]))
                graph.add_rule(
                    BuildRule(
                        output=xml_bucket_dir,
                        inputs=[],
                        command=["mkdir", "-p", xml_bucket_dir],
                        rule_type="mkdir",
                    )
                )
            test_result_paths = []
            test_rules: list[tuple[str, BuildRule]] = []
            for source, exe_path in zip(self.args.tests, test_exe_paths):
                # In CAS-only mode, place .result next to the CAS exe entry
                # so success markers are content-addressed: two builds that
                # produce byte-identical exes share the marker, and the
                # mtime-vs-published-exe race (the published exe inherits
                # the cached entry's old mtime via os.link) is sidestepped
                # entirely.
                if cas_only_results:
                    publish_rule = graph.get_rule(exe_path)
                    if publish_rule and publish_rule.rule_type == RuleType.SYMLINK:
                        cas_exe_path = publish_rule.inputs[0]
                    else:
                        cas_exe_path = exe_path
                    result_path = cas_exe_path + ".result"
                    rule_inputs: list[str] = []
                    rule_order_only = [exe_path]
                else:
                    result_path = exe_path + ".result"
                    rule_inputs = [exe_path]
                    rule_order_only = []
                if xml_bucket_dir:
                    # Order-only: the XML dir must exist before the test runs,
                    # but its mtime must not retrigger the test.
                    rule_order_only = rule_order_only + [xml_bucket_dir]
                test_cmd, framework = self._test_command_for(source, exe_path)
                # When a framework is detected and --test-xml-dir is set the
                # test recipe emits a JUnit XML file as a side effect. Make
                # the XML path the rule's *output* (rather than the .result
                # marker) so the native scheduler re-runs the test if the XML
                # is deleted out from under it. The .result marker stays the
                # success_marker (touched on rc==0) regardless.
                rule_output = result_path
                if xml_bucket_dir and framework is not None:
                    rule_output = self._xml_path_for(exe_path)
                test_rule = BuildRule(
                    output=rule_output,
                    inputs=rule_inputs,
                    command=test_cmd,
                    rule_type="test",
                    order_only_deps=rule_order_only,
                    success_marker=result_path,
                )
                graph.add_rule(test_rule)
                test_result_paths.append(rule_output)
                test_rules.append((source, test_rule))

            # --serialise-tests: chain the test rules so only one runs at a
            # time — but still during the build, not after. Each rule (in
            # source-sorted order) gains a dependency on the previous rule's
            # ``output``, which every backend's scheduler honours natively.
            # The dep goes in ``inputs`` under --use-mtime (so the previous
            # marker's mtime gates the next test) and ``order_only_deps``
            # otherwise (presence-only — the CAS-only default). The previous
            # rule's ``output`` is used, not its ``success_marker``: for a
            # framework test ``output`` is the JUnit XML and only ``output``
            # is a real graph target the scheduler can resolve.
            if getattr(self.args, "serialisetests", False) and len(test_rules) > 1:
                use_mtime = getattr(self.args, "use_mtime", False)
                chained = sorted(test_rules, key=lambda sr: sr[0])
                for (_prev_src, prev_rule), (_src, rule) in itertools.pairwise(chained):
                    dep_list = rule.inputs if use_mtime else rule.order_only_deps
                    if prev_rule.output not in dep_list:
                        dep_list.append(prev_rule.output)

            graph.add_rule(BuildRule(output="runtests", inputs=test_result_paths, command=None, rule_type="phony"))
            all_deps.append("runtests")

        graph.add_rule(BuildRule(output="all", inputs=all_deps, command=None, rule_type="phony"))

    def _result_marker_path(self, exe_path: str) -> str:
        """Return the success-marker path for ``exe_path``.

        In CAS-only mode (``--use-mtime=False``, default) for backends that
        publish via the cas-exedir layer, the marker lives at
        ``<cas_path>.result`` — sibling to the content-addressed exe — so
        the marker is keyed by exe content and survives the inode-mtime
        confusion introduced by the hard-link publish.

        In legacy mode (``--use-mtime=True``) or for backends that
        self-manage exe placement (cmake/bazel, where
        ``_self_manages_exe_placement()`` is True and there is no
        separate publish-symlink rule), falls back to
        ``<exe_path>.result`` — bit-identical to the pre-fix behaviour.

        .. note:: Self-managed-placement backends (cmake / bazel)
           currently fall back to legacy ``<exe_path>.result`` semantics
           by *omission*,
           not by design intent — they don't emit a separate
           publish-symlink rule that ``_result_marker_path`` could resolve
           through. A future change could add an equivalent content-keyed
           marker for those backends (their own change-detection layers
           know the artefact identity); the present design just doesn't
           require it because the bug being fixed (hard-link inode mtime
           confusion) is specific to the cas-exedir publish path.
        """
        if getattr(self.args, "use_mtime", False) or self._self_manages_exe_placement():
            return exe_path + ".result"
        graph = self._graph
        if graph is not None:
            rule = graph.get_rule(exe_path)
            if rule is not None and rule.rule_type == RuleType.SYMLINK and rule.inputs:
                return rule.inputs[0] + ".result"
        # Unexpected fallback: CAS-only mode but no publish-symlink rule
        # found. In production ``build_graph`` always populates
        # ``self._graph`` before a consumer asks for a marker path, so
        # reaching here means either the graph wasn't populated (a custom
        # ``execute`` override in an out-of-tree backend) or the publish rule
        # was filtered out of the graph. Surface it at verbose>=2 so the
        # silent downgrade to legacy semantics is at least diagnosable.
        if getattr(self.args, "verbose", 0) >= 2:
            print(
                f"_result_marker_path: no publish-symlink rule for {exe_path}; "
                f"falling back to legacy <exe>.result (CAS-side marker disabled)",
                file=sys.stderr,
            )
        return exe_path + ".result"

    def _xml_path_for(self, exe_path: str) -> str:
        """Per-test JUnit XML path under ``--test-xml-dir``.

        Layout: ``<test-xml-dir>/<variant>/<target_name>.xml``, keyed on
        the registry-checked ``_target_name_for`` name so mirrored test
        exes (``appalpha/main``, ``appbeta/main``) get distinct XML files
        while the xml dir stays single-level. A naive ``_``-flattening of
        the bindir-relative path is non-injective (``a_b/main`` vs
        ``a/b_main``) and would silently drop one test rule via
        ``BuildGraph.add_rule``'s last-write-wins. Caller is responsible
        for ensuring ``--test-xml-dir`` is set.
        """
        xml_dir = self.args.test_xml_dir
        variant = getattr(self.args, "variant", "") or ""
        return os.path.join(xml_dir, variant, self._target_name_for(exe_path) + ".xml")

    def _args_signature(self) -> str:
        """Deterministic ``Namespace(k=v, ...)`` repr for the build-file header.

        Filters underscore-prefixed attrs so opaque objects (parser,
        context) whose repr embeds a memory address don't poison the
        signature — two consecutive invocations with identical CLI args
        must produce byte-identical signatures so ``_build_file_uptodate``
        can short-circuit.
        """
        items = sorted((k, v) for k, v in vars(self.args).items() if not k.startswith("_"))
        return "Namespace(" + ", ".join(f"{k}={v!r}" for k, v in items) + ")"

    def _build_file_path(self) -> str:
        """Path the backend writes its generated build file to.

        Subclasses store this on ``args`` under varying names (``makefilename``,
        ``ninja_filename``, ...) — override to point at the right one.
        Default returns ``build_filename()`` for backends that emit a
        fixed-name file in the cwd.
        """
        return self.build_filename()

    def _build_file_header_token(self) -> str:
        """First-line comment that backends must write at the top of the
        generated build file. ``_build_file_uptodate`` compares this
        against the existing first line to detect arg drift.

        Uses the static ``build_filename()`` (backend identity) rather
        than ``_build_file_path()``: arg drift is detected through the
        signature; the path string is incidental and a custom
        ``--makefilename=foo.mk`` shouldn't change the header text.
        """
        return f"# {self.build_filename()} generated by {self._args_signature()}"

    def _build_file_uptodate(self, graph: BuildGraph) -> bool:
        """Check whether the generated build file is still current.

        Compares the args-signature header against the existing first
        line and walks every build-rule input's mtime against the build
        file's mtime. Inputs that are themselves outputs of other
        in-graph rules are skipped (their mtime tracks the build, not
        source freshness). Phony and mkdir rules have no real inputs.

        Returns False (regenerate) if the file is missing, the header
        differs, or any source input is newer than the build file.

        Backends that don't write a build file (or whose build file has
        no header line they control) can override to return False
        unconditionally.
        """
        path = self._build_file_path()
        try:
            file_mtime = compiletools.wrappedos.getmtime(path)
        except OSError:
            if self.args.verbose > 7:
                print(f"Regenerating {path} (does not exist).")
            return False

        expected = self._build_file_header_token()
        with open(path, encoding="utf-8") as f:
            previous = f.readline().strip()
        if previous != expected:
            if self.args.verbose > 7:
                print(f"Regenerating {path}.")
                print(f'Previous generation line was "{previous}".')
                print(f'Current  generation line  is "{expected}".')
            return False
        if self.args.verbose > 9:
            print(f"{path} header line is identical.  Testing mod time of all the files now.")

        graph_outputs = graph.outputs
        skip_types = {RuleType.PHONY, RuleType.MKDIR}
        seen: set[str] = set()
        for rule in graph.rules:
            if rule.rule_type in skip_types:
                continue
            for dep in rule.inputs:
                if dep in graph_outputs or dep in seen:
                    continue
                seen.add(dep)
                try:
                    dep_mtime = compiletools.wrappedos.getmtime(dep)
                except OSError:
                    continue
                if dep_mtime >= file_mtime:
                    if self.args.verbose > 7:
                        print(f"Regenerating {path}.")
                        print(f"mtime {dep_mtime} for {dep} is newer than mtime for {path}")
                    return False
                elif self.args.verbose > 9:
                    print(
                        f"mtime {dep_mtime} for {dep} is older than mtime for {path}. This wont trigger regeneration."
                    )

        if self.args.verbose > 9:
            print(f"{path} is up to date.  Not recreating.")
        return True

    def _validate_umask_for_file_locking(self) -> None:
        """Log warning if umask may affect multi-user file-locking mode."""
        current_umask = os.umask(0)
        os.umask(current_umask)  # Restore immediately

        if (current_umask & 0o060) and self.args.verbose >= 1:
            print(
                f"Warning: file-locking enabled with restrictive umask {oct(current_umask)}\n"
                f"  Single-user mode: Works fine (you can always remove your own locks)\n"
                f"  Multi-user mode: Requires umask 0002 or 0007 for cross-user lock cleanup\n"
                f"  If using multi-user cache, set: umask 0002",
                file=sys.stderr,
            )

    def _setup_file_locking(self) -> None:
        """Configure file-locking infrastructure for this backend.

        Sets self._filesystem_type to the detected filesystem type when
        file_locking is enabled, or None when disabled.
        """
        if getattr(self.args, "file_locking", False):
            if not check_lock_helper_available():
                report_lock_helper_missing()
            self._filesystem_type = compiletools.filesystem_utils.get_filesystem_type(self.args.cas_objdir)
            if self.args.verbose >= 3:
                print(f"Detected filesystem type: {self._filesystem_type}")
            self._validate_umask_for_file_locking()
        else:
            self._filesystem_type = None

    def _apply_build_only_changed(self, graph: BuildGraph) -> BuildGraph:
        """Filter graph to changed files if --build-only-changed is set.

        Always updates self._graph and returns the (possibly filtered) graph.
        """
        build_only_changed = getattr(self.args, "build_only_changed", None)
        if isinstance(build_only_changed, str):
            changed = set(build_only_changed.split())
            graph = graph.filter_to_changed(changed, verbose=self.args.verbose)
        self._graph = graph
        return graph

    def _wrap_compile_cmd(self, command: list[str], cwd: str | None = None) -> str:
        """Return the command string for a compile rule, lock-wrapped if needed.

        Locates ``-o target`` in the command by index (not position) so a
        trailing token after the output path doesn't desync the wrap. When
        file_locking is enabled, the -o and target are stripped and
        ct-lock-helper wraps the remainder. Mirrors ShakeBackend's compile
        path (commit a3c67675).

        Tokens are joined via ``shlex.join`` so shell-active characters
        (notably the ``<`` / ``>`` in clang header-unit flags like
        ``-fmodule-file=<vector>=...``) survive /bin/sh parsing. The
        rule.command tokens themselves stay shell-naive — argv-executing
        backends rely on that.

        ``cwd`` (when set) is woven into the recipe via ``cd <cwd> && ``
        placed *inside* the lock wrapper so the lockfile path stays
        absolute and cwd-independent. The PCH precompile rule sets cwd to
        keep gcc's PCH path-table workspace-relative; see BuildRule.cwd.
        """
        try:
            o_idx = command.index("-o")
        except ValueError as e:
            raise AssertionError(f"compile rule missing -o flag: {command}") from e

        cd_prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""

        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return cd_prefix + shlex.join(command)

        compile_part = command[:o_idx] + command[o_idx + 2 :]
        target = command[o_idx + 1]

        return wrap_compile_with_lock(cd_prefix + shlex.join(compile_part), target, self.args, self._filesystem_type)

    def _wrap_link_cmd(self, command: list[str]) -> str:
        """Return the command string for a link rule, lock-wrapped if needed.

        Unlike _wrap_compile_cmd, the command is passed through as-is
        (including -o flag) since atomic_link does not manipulate output paths.
        Same shlex.join contract applies: shell-active tokens survive recipe
        rendering without poisoning rule.command for argv backends.
        """
        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return shlex.join(command)

        # Extract target from -o flag for locking.
        # build_graph.py always emits -o in link/library commands; if absent,
        # fall back to unwrapped to avoid silently mis-targeting the lock.
        try:
            o_idx = command.index("-o")
            target = command[o_idx + 1]
        except (ValueError, IndexError):
            return shlex.join(command)

        return wrap_link_with_lock(shlex.join(command), target, self.args, self._filesystem_type)

    # ------------------------------------------------------------------
    # Shared file-emitting-backend helpers (make + ninja)
    #
    # The Makefile and Ninja writers render the same BuildGraph semantics
    # in different syntaxes. The pieces below are the semantic decisions
    # they must agree on; the per-backend writers keep only the syntax.

    @staticmethod
    def _is_framework_test(rule: BuildRule) -> bool:
        """True for a framework-detected TEST rule.

        Such a rule's ``output`` is its JUnit XML report path (so the build
        tool reruns the test when the XML is deleted) while
        ``success_marker`` is the separate ``.result`` stamp. Tests with no
        framework keep ``output == success_marker``.
        """
        return bool(rule.rule_type == RuleType.TEST and rule.output != rule.success_marker and rule.success_marker)

    @classmethod
    def _framework_test_markers(cls, graph: BuildGraph) -> list[str]:
        """``success_marker`` paths of every framework TEST rule in *graph*.

        The ``runtests`` aggregate must require these stamps as well as the
        XML reports — a framework test writes its XML and *then* exits
        non-zero on failure, so a preserved failed XML alone would satisfy
        the aggregate and silently skip the re-run.
        """
        return [rule.success_marker for rule in graph.rules if cls._is_framework_test(rule) and rule.success_marker]

    @staticmethod
    def _runtests_inputs(rule: BuildRule, framework_test_success_markers: list[str]) -> list[str]:
        """Inputs for the ``runtests`` phony aggregate.

        The rule's own inputs plus any framework-test success stamps not
        already present (see ``_framework_test_markers``).
        """
        inputs = list(rule.inputs)
        inputs.extend(m for m in framework_test_success_markers if m not in inputs)
        return inputs

    def _cas_demotes_inputs(self, rule: BuildRule) -> bool:
        """True when CAS-only mode demotes this rule's inputs to order-only.

        A CAS producer's cached path encodes the cache key, so artefact
        existence is the sole rebuild signal — the build tool must build
        the inputs first but not retrigger the producer on their mtime.
        """
        return not self.args.use_mtime and rule.rule_type in CAS_PRODUCER_TYPES

    @staticmethod
    def _cas_ordering_deps(rule: BuildRule) -> list[str]:
        """Combined order-only dep list for a CAS-demoted producer rule."""
        return list(rule.order_only_deps) + cas_demoted_order_only(rule)

    def _recipe_command_str(self, rule: BuildRule, compile_command: list[str] | None = None) -> str:
        """Render *rule*'s command as a lock-wrapped shell string.

        The shared recipe dispatch: COMPILE goes through
        ``_wrap_compile_cmd`` (honouring ``rule.cwd``), the link family
        through ``_wrap_link_cmd``, everything else through
        ``render_shell_recipe``. ``compile_command`` overrides
        ``rule.command`` for COMPILE rules only — ninja passes the
        depfile-augmented argv (``-MMD -MF <out>.d``) for ordinary
        compiles while module-interface compiles keep the bare command.

        Callers must only pass rules with a command (both file-emitting
        backends gate on ``rule.command`` before rendering a recipe).
        """
        assert rule.command is not None, "recipe rendering requires rule.command"
        rt = rule.rule_type
        if rt == RuleType.COMPILE:
            cmd = rule.command if compile_command is None else compile_command
            return self._wrap_compile_cmd(cmd, cwd=rule.cwd)
        if rt in (RuleType.LINK, RuleType.SHARED_LIBRARY, RuleType.STATIC_LIBRARY):
            return self._wrap_link_cmd(rule.command)
        return render_shell_recipe(rule)

    def _all_outputs_current(self, graph: BuildGraph) -> bool:
        """Pre-check: all compile outputs exist and all link sigs match?

        A ``True`` result short-circuits the build entirely — the backend
        skips invoking the native build tool and reports success. This
        contract assumes ``rule.output`` paths are the actual on-disk
        artifacts; backends that emit outputs into an external build
        directory (cmake's ``cmake-build/``, bazel's ``bazel-bin/``,
        etc.) must override this method, since the namer-derived paths
        in ``graph.rules`` will not exist even when a successful build
        has just completed. ``cmake_backend`` and ``bazel_backend``
        already override accordingly.

        Returns False when the graph has no compile/link rules, since the
        graph may not capture all build steps (e.g. library builds).

        Honours ``args.use_mtime``: when the user opts into legacy
        mtime semantics, this short-circuit must NOT preempt make/ninja
        — otherwise touching a source would never trigger a rebuild
        (cached artefacts always exist by construction). Return False
        unconditionally and let the native build tool do its own
        prereq-mtime comparison.
        """
        if getattr(self.args, "use_mtime", False):
            return False
        has_build_rules = False
        for rule in graph.rules:
            if rule.rule_type == RuleType.COMPILE:
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
            elif rule.rule_type in (RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
                if _read_link_sig(rule.output) != compute_link_signature(rule):
                    return False
            elif rule.rule_type == RuleType.SYMLINK:
                # Publish-as-hardlink rules are part of the build's
                # observable contract — bin/<name> must exist for the
                # build to count as "current". Without this check, an
                # interactive ``rm -rf bin/`` followed by ``make``
                # would short-circuit (cas-exe still exists) and never
                # repopulate bin/.
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
            elif rule.rule_type == RuleType.TEST:
                # Tests run in the build phase. A framework-detected test with
                # --test-xml-dir has two observable success conditions: the XML
                # report exists and the .result marker was touched. Failed XML
                # reports are intentionally preserved, so XML alone is not
                # enough to short-circuit a later build.
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
                if rule.success_marker and not os.path.exists(rule.success_marker):
                    return False
        return has_build_rules

    def _record_link_signatures(self, graph: BuildGraph) -> None:
        """Persist a content-addressable signature for every link/library
        rule whose output exists on disk.

        Backends that self-manage exe placement (cmake/bazel — see
        ``_self_manages_exe_placement``) write their actual binaries to
        a tool-managed location (``cmake-build/``, ``bazel-bin/``,
        FUSE-tracked path) rather than the graph-declared ``rule.output``.
        For those, a missing ``rule.output`` is expected and benign —
        silently skip.

        For backends that use compiletools' cas-exedir layer (the
        make/ninja/shake common case), a missing ``rule.output``
        after the build completed is a SYMPTOM, not normal: either the
        link command silently failed without a non-zero exit, or some
        downstream publish stage moved the file. Either way the next
        build's ``_all_outputs_current`` check then fails (no linksig
        present → False) and the link recipe re-fires — diagnostic,
        not silent. Log at ``verbose>=1`` so operators can spot it
        instead of chasing a "build keeps relinking" mystery (I5).
        """
        self_managed = self._self_manages_exe_placement()
        for rule in graph.rules:
            if rule.rule_type in (RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
                if not os.path.exists(rule.output):
                    if not self_managed and getattr(self.args, "verbose", 0) >= 1:
                        # I5: surface the unexpected case where a
                        # cas-exedir-using backend's link rule has no
                        # on-disk output at sig-recording time.
                        print(
                            f"  WARN: link rule output missing at signature time: {rule.output} "
                            f"(rule_type={rule.rule_type}). Next build will re-link.",
                            file=sys.stderr,
                        )
                    continue
                _write_link_sig(rule.output, compute_link_signature(rule))

    def _system_module_extra_flags(self, filename: str) -> list[str]:
        """Compiler-specific extras for system-provided module sources.

        - Clang's libc++ ``std.cppm`` declares ``module std;`` -- a name
          that's reserved in user code -- and triggers
          ``-Wreserved-module-identifier`` unless suppressed. The
          ``-stdlib=libc++`` flag is also required so the precompile
          finds libc++'s headers (the same flag is auto-injected on
          importers via ``_compiler_module_flags_for`` below).
        - GCC's ``bits/std.cc`` ships under the same toolchain whose
          driver is invoking it, so no extra flags are needed -- the
          existing ``-fmodules-ts`` plus the standard include path
          handle it.
        """
        system_modules = self.hunter.system_modules() if hasattr(self.hunter, "system_modules") else {}
        if filename not in set(system_modules.values()):
            return []
        kind = self._module_compiler_kind
        if kind == "clang":
            self._compile_used_libcxx = True
            return ["-stdlib=libc++", "-Wno-reserved-module-identifier"]
        return []

    def _build_imports_std(self) -> bool:
        """Return True when any TU in this build imports the std module.

        Cached on first call. Used by ``_compiler_module_flags_for`` to
        decide whether clang importers need ``-stdlib=libc++`` injected.
        """
        if self._build_imports_std_cached is not None:
            return self._build_imports_std_cached
        result = False
        if hasattr(self.hunter, "system_modules"):
            system_modules = self.hunter.system_modules()
            result = "std" in system_modules
        self._build_imports_std_cached = result
        return result

    def _transitive_header_unit_imports(self, filename: str | None) -> set[str]:
        """Header-unit tokens (``<h>`` / ``"h"``) reaching a TU transitively.

        A ``.cpp`` whose only ``import <h>;`` arrives through a
        ``#include "wrap.h"`` has an empty own ``module_header_imports``,
        but after preprocessing the import is part of the TU and its
        compile must resolve the header unit. This walks the TU's
        transitive headers (``header_dependencies``), skipping the TU
        itself, and unions each header's ``module_header_imports`` — the
        header-unit analogue of the ``transitive_named_imports`` check
        already done for named modules. Returns an empty set for a
        ``None`` filename (caller may pass one in the pre-pass).
        """
        tokens: set[str] = set()
        if filename is None:
            return tokens
        for dep in self.hunter.header_dependencies(filename):
            dep_str = str(dep)
            if dep_str == filename:
                continue
            dep_result = self.hunter._file_analysis_result(dep_str)
            if dep_result is None:
                continue
            tokens.update(dep_result.module_header_imports)
        return tokens

    def _compiler_module_flags_for(self, filename: str) -> list[str]:
        """Per-TU C++20 modules flags for the detected compiler.

        Returns the flag tokens that must be appended to the compile
        command for any TU that touches the module graph (exports,
        implements, or imports a named module). Empty list when the TU
        is unrelated to modules or the compiler is unknown.

        - GCC: ``-fmodules-ts``.
        - Clang: ``-fprebuilt-module-path=<pcm_dir>`` for whole-module
          ``import M;`` lookups, plus one ``-fmodule-file=M:P=<path>``
          per known partition. The ``-fprebuilt-module-path`` flag does
          NOT find partitions (clang requires explicit per-partition
          mapping), so we blanket-emit every partition's mapping. It's
          extra noise on the command line but keeps the rule emission
          per-TU rather than scanning each TU's transitive partition use.
        """
        result = self.hunter._file_analysis_result(filename)
        if result is None:
            return []
        touches_modules = bool(
            result.module_exports or result.module_implements or result.module_imports or result.module_header_imports
        )
        # A header reached through `#include` may itself `import M;` or
        # `import <h>;` — the consumer compile must still enter modules
        # mode (gcc: -fmodules-ts; clang: -fmodules / -fprebuilt-module-path
        # / -fmodule-file=) even though the TU's own analysis result is
        # empty. Both *named* imports (`import M;`, `import M:P;`) and
        # *header-unit* imports (`import <h>;`, `import "h";`) from
        # transitive headers open the gate; the clang header-unit
        # consume-flags themselves are emitted below from the transitive
        # token union. A bare ``import :P;`` in a non-module header has no
        # resolvable owning module and is malformed C++ anyway, so it's
        # excluded from the named check.
        transitive_named_imports = False
        for dep in self.hunter.header_dependencies(filename):
            dep_str = str(dep)
            if dep_str == filename:
                continue
            dep_result = self.hunter._file_analysis_result(dep_str)
            if dep_result is None:
                continue
            if any(not imp.startswith(":") for imp in dep_result.module_imports) or dep_result.module_header_imports:
                transitive_named_imports = True
                break
        touches_modules = touches_modules or transitive_named_imports
        if not touches_modules:
            return []
        kind = self._module_compiler_kind
        if kind == "gcc":
            extras = ["-fmodules-ts"]
            mapper = self._gcc_module_mapper_path
            if mapper:
                # Importers and precompiles BOTH need -fmodule-mapper so
                # they read/write .gcm files at the cas-pcmdir paths the
                # mapper specifies; without it gcc would write into its
                # default `gcm.cache/` and importers would look in the
                # cache (where the mapper points) for a file that isn't
                # there.
                extras.append(f"-fmodule-mapper={mapper}")
                # Suppress DW_AT_producer flag-string recording so the
                # absolute mapper path (which lives next to the per-build
                # makefile, hence outside any -ffile-prefix-map prefix)
                # doesn't leak into every consumer's .o. Without this,
                # alice's bindir path ends up in bob's .o via DWARF and
                # cas-objdir cross-user byte-identity breaks. The cost is
                # a slightly less informative DW_AT_producer (just the
                # gcc version + target triple, no flag list) — a fair
                # trade for cross-user CAS soundness, and only applied
                # to module-using compiles.
                extras.append("-gno-record-gcc-switches")
            return extras
        if kind == "clang":
            extras: list[str] = []
            # `-fprebuilt-module-path` is only useful when all .pcm files
            # live in the same flat directory, which is the non-cache
            # case. With cas-pcmdir each .pcm is under its own
            # `<command_hash>/` subdir, so the flat scan would find
            # nothing -- the per-module `-fmodule-file=` mappings emitted
            # by `_clang_partition_module_file_flags` carry the lookup
            # in that mode. The gate also fires when only a transitive
            # header imports a module, since the consumer still needs
            # the lookup path.
            if (
                (result.module_imports or result.module_implements or transitive_named_imports)
                and self._module_pcm_dir
                and not self._module_pcm_cache_root
            ):
                extras.append("-fprebuilt-module-path=" + self._module_pcm_dir)
            extras.extend(self._clang_partition_module_file_flags())
            # Header units: clang's `import <h>;` lookup is gated by
            # `-fmodules` even when the actual BMI is supplied via
            # `-fmodule-file=`; without -fmodules the importer rejects
            # it as "not known to be a header unit". The flag is safe to
            # add for any clang TU that mentions header units. Each
            # `-fmodule-file=<token>=<pcm>` then maps a specific header.
            # The token form (`<vector>` / `"foo.h"`) contains
            # shell-active characters (`<` / `>` look like redirection
            # to /bin/sh) -- but quoting belongs at the recipe-rendering
            # layer (_wrap_compile_cmd uses shlex.join), not here.
            # BuildRule.command is also passed verbatim to subprocess.Popen
            # by the argv-executing backend (Shake), which would reject
            # a pre-shell-quoted token as an unknown flag.
            # Per-TU narrowing (rather than the blanket-all approach
            # used for partitions) is cheap because each TU's
            # header-unit list is short. A header unit reaching the TU
            # only through a #include'd header (empty own
            # `module_header_imports`) still needs these flags — after
            # preprocessing the import is part of this TU — so union the
            # transitive header-unit tokens, symmetric with the
            # transitive named-module handling above.
            header_imports = set(result.module_header_imports)
            header_imports.update(self._transitive_header_unit_imports(filename))
            if header_imports:
                extras.append("-fmodules")
                for token in sorted(header_imports):
                    pcm = self._header_unit_artefact.get(token)
                    if pcm is not None:
                        extras.append(f"-fmodule-file={token}={pcm}")
            # Importer must use the same -stdlib as the precompile of
            # any module/header-unit it imports -- otherwise its
            # ``<vector>`` resolves to libstdc++ and the libc++-built BMI
            # is rejected as "not known to be a header unit". Gate
            # mirrors ``_create_header_unit_precompile_rule`` for HUs and
            # ``_system_module_extra_flags`` for ``import std;``.
            cxxflags_has_libcxx = "-stdlib=libc++" in self.args.flags.cxx
            needs_libcxx = "std" in result.module_imports or bool(header_imports)
            if self._build_imports_std() and needs_libcxx and not cxxflags_has_libcxx:
                extras.append("-stdlib=libc++")
                self._compile_used_libcxx = True
            return extras
        return []

    def _clang_partition_module_file_flags(self) -> list[str]:
        """Build the per-module ``-fmodule-file=NAME=PATH`` flag list.

        Sorted for determinism so the makefile diff stays stable across
        runs (the underlying registry is a dict and would otherwise emit
        in insertion order).

        When cas-pcmdir is on, EVERY known module is emitted (named
        primaries and partitions alike), since each .pcm sits under its
        own ``<command_hash>/`` subdir and ``-fprebuilt-module-path``
        can't find it. When cas-pcmdir is off the flat per-build dir
        does work with ``-fprebuilt-module-path``, so we limit the
        per-module flags to partitions (the same Phase 3 behaviour) to
        keep the command line short. The function is named "partition"
        for historical reasons; the cache mode generalises it.
        """
        if not self._module_iface_pcm:
            return []
        cache_active = bool(self._module_pcm_cache_root)
        return [
            f"-fmodule-file={name}={path}"
            for name, path in sorted(self._module_iface_pcm.items())
            if cache_active or ":" in name
        ]

    def _wire_module_inputs(self, rule: BuildRule, file_result, filename: str | None = None) -> None:
        """Append BMI/stamp inputs from `rule` to its module dependencies.

        Mutates ``rule.inputs`` in place. The artefact a downstream TU
        waits on differs by compiler:

        - GCC: the interface unit's object file. Building it under
          ``-fmodules-ts`` writes ``gcm.cache/<name>.gcm`` as a side
          effect, and that's where the importer's ``import M;`` lookup
          finds the CMI -- so gating on .o is sufficient.
        - Clang: the interface unit's ``.pcm`` file (the BMI). Importers
          read it directly via ``-fprebuilt-module-path`` /
          ``-fmodule-file``; gating on the .pcm rather than the .o lets
          the .pcm-to-.o conversion run in parallel with importer
          compiles.

        Partition-relative imports (``import :basic;``) and
        ``module_implements`` entries are resolved against the file's
        own primary module name, mirroring Hunter's source-discovery
        resolver. Without this resolution, ``:basic`` would miss the
        registry key ``math:basic`` and the importer would race the
        partition's compile -> ``imports must be built before being
        imported``.

        For an importer of the primary module ``M``, we also wire edges
        to every partition of ``M`` -- the partitions' ``.pcm``/``.o``
        files must exist when the importer compiles, since the primary
        ``.pcm`` references them.

        All BMI / .o / .stamp targets join ``rule.inputs`` uniformly.
        ``order_only_deps`` stays reserved for *bucket directories*
        the prebuild loop / trace backend will ``mkdir``; putting an
        artefact path there would silently clobber the artefact into
        a directory under any backend that mkdir's order-only deps
        (the original C++20-modules trace_backend defect class). The
        make/ninja CAS-only path lifts these inputs to the order-only
        ``|`` clause via ``ordering_inputs_for_compile`` (matched on
        ``_COMPILE_ORDERING_INPUT_EXTS``, which now covers .gch /
        .pcm / .gcm / .o / .stamp), so rebuild semantics in CAS-only
        mode are unchanged. In ``--use-mtime=True`` mode the targets
        become real prereqs — touching the producer's .o now
        correctly triggers the importer rebuild (a latent bug under
        the prior ``order_only_deps`` shape, which silently swallowed
        BMI mtime changes).
        """
        if file_result is None:
            return
        own_module = self.hunter._own_module_name(file_result)
        kind = self._module_compiler_kind
        # Target selection for named modules. The target must be a
        # path that actually has a producing rule in the graph -- only
        # then can downstream backends (make ``no rule to make target``
        # check; trace_backend's recursive ``_build_async``) wait on
        # it.
        #
        # * clang -- ``_module_iface_pcm`` holds the .pcm BMI path,
        #   which is the output of the clang precompile rule.
        # * gcc -- ``_module_iface_obj`` holds the producer's .o
        #   path, which IS a rule output. The .gcm cache file (in
        #   ``_module_iface_gcm``) is a *side effect* of that .o
        #   compile under ``-fmodules-ts`` + ``-fmodule-mapper=`` --
        #   no rule produces it directly, so make would error with
        #   "no rule to make target <name>.gcm" if the importer
        #   listed the .gcm. The .o has the same wait semantics
        #   (existence implies the .gcm is on disk).
        target_map: Mapping[str, str] = self._module_iface_pcm if kind == "clang" else self._module_iface_obj

        def _add_dep(target: str | None) -> None:
            """Append *target* to ``rule.inputs`` (deduped, skipping self).

            All module dep targets land in ``inputs``: the CAS-only
            filter (``ordering_inputs_for_compile``) preserves every
            suffix in ``_COMPILE_ORDERING_INPUT_EXTS`` (.gch / .pcm /
            .gcm / .o / .stamp), and trace_backend recurses into
            ``inputs`` via ``_build_async`` to build the producer
            before the consumer compiles.
            """
            if target is None or target == rule.output:
                return
            if target not in rule.inputs:
                rule.inputs.append(target)

        def _wire_for(src_result, src_own_module: str | None) -> None:
            """Wire imports + header-unit deps of one analysis result.

            Used uniformly for the TU itself and for each transitive
            header. Header-unit edges go through ``_header_unit_artefact``
            (compiler-agnostic stamp/.pcm lookup); named-module edges
            go through ``target_map`` (.o for gcc, .pcm for clang) with
            partition expansion for primary-module imports.
            """
            for raw in tuple(src_result.module_imports) + tuple(src_result.module_implements):
                r = self.hunter._resolve_module_import(raw, src_own_module)
                if r is None:
                    continue
                _add_dep(target_map.get(r))
                # Importer of a primary M depends transitively on M's
                # partitions; over-includes M's own partition exports
                # too -- ``_add_dep`` dedups.
                if ":" not in r:
                    for part_name in target_map:
                        if part_name.startswith(r + ":"):
                            _add_dep(target_map.get(part_name))
            if self._header_unit_artefact:
                for token in src_result.module_header_imports:
                    _add_dep(self._header_unit_artefact.get(token))

        _wire_for(file_result, own_module)

        # Transitive headers: a .C/.cpp whose only `import M;` arrives
        # through `#include "wrap.H"` still needs a BMI edge from M's
        # producer into its compile rule -- otherwise the importer
        # races the producer under `-j`. Each header's imports resolve
        # against THAT header's own module; a header almost never
        # declares one, so bare `:P` from a non-module header resolves
        # to None and is skipped -- matching the plan's decision to
        # ignore bare-partition imports in transitive headers.
        if filename is None:
            return
        for dep in self.hunter.header_dependencies(filename):
            dep_str = str(dep)
            if dep_str == filename:
                continue
            dep_result = self.hunter._file_analysis_result(dep_str)
            if dep_result is None:
                continue
            _wire_for(dep_result, self.hunter._own_module_name(dep_result))

    def _header_unit_destination(self, token: str, flat_dir: str) -> tuple[str, str]:
        """Resolve the per-token artefact path and its parent dir.

        Returns ``(artefact_path, dir_for_mkdir)``.

        - **gcc**: ``flat_dir/<stem>.stamp``. The real ``.gcm`` lands
          under ``gcm.cache/<abs-path>.gcm`` (compiler-managed, can't be
          redirected without ``-fmodule-mapper=``); the stamp only
          sequences make's graph. Phase 7b will replace this with a
          mapper-driven cache path.
        - **clang, cache off**: ``flat_dir/<stem>.pcm``.
        - **clang, cache on**: ``<cas-pcmdir>/<command_hash>/<stem>.pcm``.
          Hash inputs match the named-module cache (compiler identity,
          flags, magic flags) plus a stage marker that prevents
          collisions with same-named modules.

        ``<stem>`` comes from ``_header_unit_safe_stem(token)``.
        """
        kind = self._module_compiler_kind
        stem = _header_unit_safe_stem(token)
        if kind == "clang":
            cache_root = self._module_pcm_cache_root
            if cache_root:
                cmd_hash = self._compute_clang_header_unit_command_hash(token)
                pcm_path = _cas_pcm_path(stem + ".pcm", cache_root, cmd_hash)
                hash_dir = os.path.join(cache_root, cmd_hash)
                # Header-unit manifests bucket by token (e.g. ``<vector>``)
                # so the same header in different variants/projects shares
                # a bucket and keep_count is enforced per token.
                _write_pcm_manifest(
                    pcmdir=cache_root,
                    cmd_hash=cmd_hash,
                    bucket_key=token,
                    transitive_headers=[],
                    cxx_command=self.args.CXX,
                    stage="clang_header_unit",
                    context=self.context,
                    anchor_root=self._anchor_root,
                )
                return pcm_path, hash_dir
            return os.path.join(flat_dir, stem + ".pcm"), flat_dir
        if kind == "gcc" and self._module_pcm_cache_root:
            # gcc + cache: route the .gcm into cas-pcmdir via the
            # mapper file. The rule's output IS the .gcm cache path
            # (gcc writes it directly), not a stamp.
            dest = self._gcc_header_unit_gcm_destination(token)
            if dest is not None:
                cache_root = self._module_pcm_cache_root
                assert cache_root is not None
                # Recover the cmd_hash from the dest's parent dir name
                # rather than re-hashing.
                cmd_hash = os.path.basename(dest[1])
                _write_pcm_manifest(
                    pcmdir=cache_root,
                    cmd_hash=cmd_hash,
                    bucket_key=token,
                    transitive_headers=[],
                    cxx_command=self.args.CXX,
                    stage="gcc_header_unit",
                    context=self.context,
                    anchor_root=self._anchor_root,
                )
                return dest
        # gcc (and any unknown compiler) without cache stays on the
        # flat-dir stamp.
        return os.path.join(flat_dir, stem + ".stamp"), flat_dir

    def _compute_clang_header_unit_command_hash(self, token: str) -> str:
        """command_hash for a clang header-unit precompile.

        Uses the bare token as the source-identity input (``<vector>``
        differs from ``<string>``); ``compiler_identity`` -- captured
        as part of the command_hash -- catches the underlying header's
        actual content via the toolchain binary's mtime/size, since
        system headers come from the compiler install and a libc++
        update implies a new compiler. We deliberately don't resolve
        ``<vector>`` to its abs path -- that would cost a per-token
        ``clang -E`` subprocess for negligible additional safety.

        ``-stdlib=libc++`` is folded into ``extra_flags`` only when the
        actual precompile command will inject it (i.e. the build imports
        std AND CXXFLAGS doesn't already carry it). Mirrors the gating
        in ``_create_header_unit_precompile_rule`` so the cache key
        reflects exactly the flag set the compiler will see.
        """
        cxxflags_tokens = self.args.flags.hash_relevant("cxx")
        cxxflags_has_libcxx = "-stdlib=libc++" in self.args.flags.cxx
        injects_libcxx = self._build_imports_std() and not cxxflags_has_libcxx
        # Fold the unioned magic system-include flags into the cache key
        # the same way the precompile rule will see them (appended to
        # ``args.flags.cxx`` in ``_create_header_unit_precompile_rule``).
        # Without this, two builds whose pkg-config-derived ``-isystem``
        # paths differ would collide on the same cmd_hash. ``getattr``
        # guards callers that may run before the pre-pass populates it.
        extra_si = list(getattr(self, "_header_unit_extra_system_includes", ()))
        return _pcm_command_hash(
            self.args,
            source_path=token,
            transitive_content_hash="",  # implicit in compiler_identity
            cxxflags_tokens=list(cxxflags_tokens) + extra_si,
            magic_cpp_flags=[],  # header units don't carry per-file magic
            magic_cxx_flags=[],
            extra_flags=["-stdlib=libc++"] if injects_libcxx else [],
            stage="clang_header_unit",
            anchor_root=self._anchor_root,
        )

    def _create_header_unit_precompile_rule(self, token: str, artefact_path: str) -> BuildRule:
        """Emit one precompile rule for a header-unit token.

        ``artefact_path`` is the resolved destination from
        :meth:`_header_unit_destination` -- the .pcm under cas-pcmdir
        for clang+cache, the flat-dir .pcm for clang without cache, or
        the flat-dir .stamp for gcc.

        - **gcc**: ``g++ -fmodules-ts -c -x c++-system-header <header>``
          writes the ``.gcm`` to ``gcm.cache/<resolved-abs-path>.gcm``
          (path depends on header-search resolution, so we can't name it
          as the make output). The rule's output is the stamp file
          touched via ``success_marker`` after the precompile succeeds;
          importers depend on that stamp for sequencing. Phase 7b
          replaces this with a mapper-redirected cache path.
        - **clang**: ``clang++ --precompile -xc++-system-header
          <header> -o <pcm>`` produces the .pcm at the path we picked.

        We deliberately drop magic per-TU CPPFLAGS / CXXFLAGS because
        the header unit is global across the build -- no single user
        TU's magic flags can apply without risking inconsistent
        precompiles. The one exception is **system-include search-path
        flags** (``-isystem`` / ``-isysroot`` / ``-iframework`` /
        ``-idirafter`` / ``--sysroot=``) collected from magic
        CPPFLAGS / CXXFLAGS into
        ``self._header_unit_extra_system_includes`` by the pre-pass:
        without them, a header reached only through a per-source
        ``//#PKG-CONFIG=lib`` magic flag (which
        ``magicflags._handle_pkg_config`` expands to
        ``-isystem <pkg-include>``) cannot resolve at precompile time
        and gcc dies with ``fatal error: <h>: No such file or
        directory``. The ``-isystem`` immutability contract still
        holds (the user is opting that header into the
        "doesn't-mutate-between-builds" promise; see
        ``_extract_system_include_path_flags``), so this is a safe
        widening, not a refactor of the global-vs-per-TU split.
        """
        kind = self._module_compiler_kind
        bare = _header_unit_arg(token)

        common_cmd = (
            compiletools.utils.split_command_cached(self.args.CXX)
            + list(self.args.flags.cxx)
            + list(getattr(self, "_header_unit_extra_system_includes", ()))
        )

        artefact_dir = os.path.dirname(artefact_path)

        if kind == "clang":
            # When the build imports std, importers compile with -stdlib=libc++
            # (injected by _compiler_module_flags_for). The precompile must
            # match -- otherwise clang's BMI verification rejects on the
            # stdlib axis. _compute_clang_header_unit_command_hash already
            # folds -stdlib=libc++ into the cmd_hash under this same gate;
            # this keeps the actual command consistent with what the cache
            # key claims.
            stdlib_extras = (
                ["-stdlib=libc++"] if self._build_imports_std() and "-stdlib=libc++" not in self.args.flags.cxx else []
            )
            if stdlib_extras:
                self._compile_used_libcxx = True
            cmd = common_cmd + stdlib_extras + ["-xc++-system-header", "--precompile", bare, "-o", artefact_path]
            self._header_unit_artefact[token] = artefact_path
            return BuildRule(
                output=artefact_path,
                inputs=[],  # source is a system header - filesystem lookup, no graph edge
                command=cmd,
                rule_type="compile",
                order_only_deps=[artefact_dir],
            )
        # gcc (and unknown kinds). Two modes:
        # - cache off: stamp output. gcc auto-places .gcm under
        #   gcm.cache/<absolute-resolved-path>.gcm and the stamp is
        #   touched via success_marker for make's bookkeeping. No
        #   producer-side rename is needed because gcm.cache is
        #   per-cwd, not shared across builds.
        # - cache on: artefact_path IS the .gcm cache path that
        #   importers read via the global -fmodule-mapper. The
        #   precompile rule uses a per-rule mini-mapper (one entry
        #   pointing the resolved header at <artefact>.compiletools.tmp)
        #   so gcc writes the .gcm to a sibling temp path, which the
        #   recipe then mv -fs into place. Producer-side temp+rename
        #   is the same invariant the object CAS upholds (see CLAUDE.md
        #   "Locking System") -- without it, two concurrent peer
        #   ct-cake invocations precompiling the same header could let
        #   an importer mmap-read a half-written .gcm. The global
        #   mapper still names <artefact> directly, so importers find
        #   the renamed-into-place .gcm.
        # The rule_type is "header_unit" (rather than "compile")
        # because gcc silently ignores `-o` under
        # `-x c++-system-header`, so we can't lean on
        # `_wrap_compile_cmd`'s -o-driven lock+temp+rename pipeline.
        self._header_unit_artefact[token] = artefact_path
        mapper = self._gcc_module_mapper_path
        abs_paths = self._gcc_header_unit_resolved.get(token)
        if mapper and abs_paths:
            # Suffix mini-mapper and tmp with the build_graph process's PID
            # so two concurrent peer ct-cake invocations targeting the same
            # cmd_hash slot don't share a tmp inode (would risk torn-write
            # interleaving under O_TRUNC) or overwrite each other's
            # mini-mapper. Each invocation writes its own files; the mv
            # is atomic on the shared destination, and concurrent mvs
            # converge on identical bytes (same cmd_hash => same compiler
            # + same flags => deterministic .gcm).
            unique = os.getpid()
            tmp_path = f"{artefact_path}.compiletools.tmp.{unique}"
            mini_mapper = f"{artefact_path}.precompile-mapper.{unique}.txt"
            os.makedirs(artefact_dir, exist_ok=True)
            # Emit one mapper key per spelling the compiler may produce
            # (canonical + non-canonical) -- both point at tmp_path so
            # gcc writes the .gcm there regardless of which form its
            # active flag set produces. Same multi-key shape as the
            # global mapper writer in _write_gcc_module_mapper.
            with open(mini_mapper, "w") as f:
                f.writelines(f"{p} {tmp_path}\n" for p in abs_paths)
            cmd = common_cmd + [
                "-fmodules-ts",
                "-c",
                "-x",
                "c++-system-header",
                bare,
                f"-fmodule-mapper={mini_mapper}",
            ]
            pipeline = f"{shlex.join(cmd)} && mv -f {shlex.quote(tmp_path)} {shlex.quote(artefact_path)}"
            return BuildRule(
                output=artefact_path,
                inputs=[],
                command=["sh", "-c", pipeline],
                rule_type="header_unit",
                order_only_deps=[artefact_dir],
                success_marker=artefact_path,
            )
        # Fall through: cache-off OR cache-on without a resolved abs path
        # (couldn't precompute via -M; the global mapper has no entry for
        # this token, so gcc lands the .gcm in gcm.cache/<resolved>.gcm
        # at compile time).
        cmd = common_cmd + ["-fmodules-ts", "-c", "-x", "c++-system-header", bare]
        if mapper:
            cmd.append(f"-fmodule-mapper={mapper}")
        return BuildRule(
            output=artefact_path,
            inputs=[],
            command=cmd,
            rule_type="header_unit",
            order_only_deps=[artefact_dir],
            success_marker=artefact_path,
        )

    def _gcc_module_gcm_destination(self, source_filename: str, module_name: str) -> tuple[str, str]:
        """Resolve the .gcm cache path and parent dir for a gcc named module.

        Returns ``(gcm_path, gcm_dir)`` where ``gcm_path`` is
        ``<cas-pcmdir>/<command_hash>/<filename-stem>.gcm``. The
        ``-fmodule-mapper`` file maps the module name to this path so
        gcc writes the .gcm directly into the cache rather than its
        default ``gcm.cache/<name>.gcm`` per-cwd location.

        Only called when gcc + cas-pcmdir are both active.
        """
        cache_root = self._module_pcm_cache_root
        assert cache_root is not None, "gcc gcm destination requires cas-pcmdir"
        cmd_hash = self._compute_pcm_command_hash(source_filename, stage="gcc_module_interface", extra_flags=[])
        # ``_module_pcm_filename`` returns ``<name>.pcm``; swap the suffix
        # for ``.gcm`` so the same name-escape rules apply (partition
        # ``:`` -> ``^^``) and we get a stable on-disk filename.
        gcm_filename = _module_pcm_filename(module_name)[: -len(".pcm")] + ".gcm"
        gcm_path = _cas_pcm_path(gcm_filename, cache_root, cmd_hash)
        per_hash_dir = os.path.join(cache_root, cmd_hash)
        _write_pcm_manifest(
            pcmdir=cache_root,
            cmd_hash=cmd_hash,
            bucket_key=compiletools.wrappedos.realpath(source_filename),
            transitive_headers=sorted(str(d) for d in self.hunter.header_dependencies(source_filename)),
            cxx_command=self.args.CXX,
            stage="gcc_module_interface",
            context=self.context,
            anchor_root=self._anchor_root,
        )
        return gcm_path, per_hash_dir

    def _gcc_header_unit_gcm_destination(self, token: str) -> tuple[str, str] | None:
        """Resolve the .gcm cache path and parent dir for a gcc header unit.

        Returns ``(gcm_path, gcm_dir)``. Only called when gcc +
        cas-pcmdir are both active. Uses the same single-command_hash
        layout as named modules: ``<cache_root>/<cmd_hash>/<stem>.gcm``.
        The hash inputs include the bare token as the source identity;
        ``compiler_identity`` (folded into command_hash) catches actual
        header content via the toolchain binary's mtime/size.
        """
        cache_root = self._module_pcm_cache_root
        assert cache_root is not None
        # Fold the unioned magic system-include flags into the cmd_hash
        # the same way the precompile rule will see them (appended to
        # ``args.flags.cxx`` in ``_create_header_unit_precompile_rule``).
        # Without this, two builds whose pkg-config-derived ``-isystem``
        # paths differ would collide on the same cmd_hash and import
        # the wrong BMI -- gcc's consume-time check is flag-aware, not
        # content-aware. ``getattr`` guards the bare-precompile call path
        # that may run before the pre-pass populates the attribute.
        extra_si = list(getattr(self, "_header_unit_extra_system_includes", ()))
        cmd_hash = _pcm_command_hash(
            self.args,
            source_path=token,
            transitive_content_hash="",  # implicit in compiler_identity
            cxxflags_tokens=list(self.args.flags.hash_relevant("cxx")) + extra_si,
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            extra_flags=[],
            stage="gcc_header_unit",
            anchor_root=self._anchor_root,
        )
        stem = _header_unit_safe_stem(token)
        gcm_path = _cas_pcm_path(stem + ".gcm", cache_root, cmd_hash)
        per_hash_dir = os.path.join(cache_root, cmd_hash)
        return gcm_path, per_hash_dir

    def _write_gcc_module_mapper(self) -> None:
        """Materialize the -fmodule-mapper file for the current build.

        Called after ``_module_iface_gcm`` and ``_header_unit_artefact``
        are populated. The mapper file lives at
        ``<cas-objdir>/.module-mapper.txt`` (per-build, regenerated
        each ``ct-cake`` invocation). Each line is ``<key> <gcm-path>``
        where the key is:

        - The module name for named modules (``math``, ``math:basic``).
        - The absolute resolved header path for header units (gcc looks
          header units up by their resolved path, not by the angle/quote
          token in the source).

        Header units that fail to resolve to an absolute path are
        omitted -- gcc then falls back to its default
        ``gcm.cache/<abs-path>.gcm`` placement, which is still correct
        but bypasses the cas cache for those entries.
        """
        if self._module_compiler_kind != "gcc" or not self._module_pcm_cache_root:
            return
        mapper_path = self._gcc_module_mapper_path
        assert mapper_path is not None  # invariant: gcc + cache => path was set
        os.makedirs(os.path.dirname(mapper_path), exist_ok=True)
        lines: list[str] = []
        for name in sorted(self._module_iface_gcm):
            lines.append(f"{name} {self._module_iface_gcm[name]}")
        for token in sorted(self._gcc_header_unit_resolved):
            abs_paths = self._gcc_header_unit_resolved[token]
            gcm_path = self._header_unit_artefact.get(token)
            if not gcm_path:
                continue
            # One line per spelling (canonical + non-canonical), all
            # pointing at the same .gcm. See _resolve_system_header_abs_paths
            # for why both spellings exist: bazel's autoconfig appends
            # ``-fno-canonical-system-headers`` after our cxxopts, so the
            # importer's lookup may use either form depending on whose
            # flag won the ordering race.
            for abs_path in abs_paths:
                if abs_path:
                    lines.append(f"{abs_path} {gcm_path}")
        # Atomic write so concurrent ct-cake invocations don't see a
        # partial mapper. We don't need a lock here -- worst case two
        # invocations write the same content and one rename wins.
        tmp = mapper_path + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, mapper_path)

    def _clang_module_pcm_destination(self, source_filename: str, module_name: str) -> tuple[str, str]:
        """Resolve the .pcm path and its parent dir for a clang interface unit.

        Returns ``(pcm_path, pcm_dir)``. When the cas-pcmdir cache is
        active, ``pcm_dir`` is ``<cas_pcmdir>/<command_hash>/`` and
        ``pcm_path`` is ``<that_dir>/<module-filename>.pcm``. When the
        cache is off, ``pcm_dir`` is the flat per-build dir and
        ``pcm_path`` is ``<flat_dir>/<module-filename>.pcm``.

        ``command_hash`` is computed from the same inputs that drive the
        precompile bytes -- compiler identity, hash-relevant flags,
        per-file magic flags, and the transitive content reachable from
        the source -- so identical configurations share a cache entry
        across rebuilds.
        """
        flat_dir = self._module_pcm_dir
        assert flat_dir is not None  # only called in clang mode
        pcm_filename = _module_pcm_filename(module_name)
        cache_root = self._module_pcm_cache_root
        if not cache_root:
            return os.path.join(flat_dir, pcm_filename), flat_dir
        cmd_hash = self._compute_pcm_command_hash(source_filename, stage="clang_module_interface", extra_flags=[])
        pcm_path = _cas_pcm_path(pcm_filename, cache_root, cmd_hash)
        per_hash_dir = os.path.join(cache_root, cmd_hash)
        # Write the trim-time sidecar manifest now (before the rule even
        # runs) so trim_cache can reason about cache reachability without
        # depending on a successful build first. Bucketed by source
        # realpath so cross-variant builds of the same .cppm coexist
        # under keep_count=1.
        _write_pcm_manifest(
            pcmdir=cache_root,
            cmd_hash=cmd_hash,
            bucket_key=compiletools.wrappedos.realpath(source_filename),
            transitive_headers=sorted(str(d) for d in self.hunter.header_dependencies(source_filename)),
            cxx_command=self.args.CXX,
            stage="clang_module_interface",
            context=self.context,
            anchor_root=self._anchor_root,
        )
        return pcm_path, per_hash_dir

    def _compute_pcm_command_hash(self, source_filename: str, stage: str, extra_flags: list[str]) -> str:
        """Compute the cas-pcmdir command_hash for a precompile.

        Folds the source's content hash and dep_hash into the command's
        canonical key so any drift -- in the source, any included
        header, the compiler, or any flag the compiler reads -- yields
        a different cache path. PCM relies on the compiler's BMI
        verification at consume time as the safety net for the rare
        hash-collision case (see ``_pcm_command_hash`` docstring).
        """
        import stringzilla as sz

        deplist = self.hunter.header_dependencies(source_filename)
        dep_hash = self.namer.compute_dep_hash(deplist)
        try:
            source_hash = compiletools.global_hash_registry.get_file_hash(source_filename, self.context)
        except (FileNotFoundError, OSError):
            source_hash = ""
        magicflags = self.hunter.magicflags(source_filename)
        magic_cpp = magicflags.get(sz.Str("CPPFLAGS"), [])
        magic_cxx = magicflags.get(sz.Str("CXXFLAGS"), [])
        cxxflags_tokens = self.args.flags.hash_relevant("cxx")
        return _pcm_command_hash(
            self.args,
            source_path=compiletools.wrappedos.realpath(source_filename),
            transitive_content_hash=f"{source_hash}:{dep_hash}",
            cxxflags_tokens=cxxflags_tokens,
            magic_cpp_flags=magic_cpp,
            magic_cxx_flags=magic_cxx,
            extra_flags=extra_flags,
            stage=stage,
            anchor_root=self._anchor_root,
        )

    def _create_clang_module_interface_rules(self, filename: str, module_name: str) -> tuple[BuildRule, BuildRule]:
        """Emit the (precompile, compile) rule pair for one clang interface unit.

        Clang's modules flow is two-stage: first ``--precompile`` turns
        the source into a ``.pcm`` (the BMI that importers consume), then
        ``-c`` compiles the ``.pcm`` into a ``.o`` for linking. We emit
        both rules here:

          1. ``clang++ ... -x c++-module --precompile <filename> -o <pcm>``
          2. ``clang++ ... -c <pcm> -o <obj>`` -- depends on the .pcm.

        The single pre-existing object-cache key (``obj_name``, derived
        via ``namer.object_pathname``) is reused so the .o lands at the
        same path it would for a non-module clang TU; this keeps cache
        addressing consistent across compiler swaps.
        """
        import stringzilla as sz

        deplist = self.hunter.header_dependencies(filename)
        prerequisites = [filename] + sorted([str(dep) for dep in deplist])
        magicflags = self.hunter.magicflags(filename)
        magic_cpp_flags = magicflags.get(sz.Str("CPPFLAGS"), [])
        magic_cxx_flags = magicflags.get(sz.Str("CXXFLAGS"), [])
        dep_hash = self.namer.compute_dep_hash(deplist)
        macro_state_hash = self.hunter.macro_state_hash(filename, dep_hash=dep_hash)
        obj_name = self.namer.object_pathname(filename, macro_state_hash, dep_hash)

        assert self._module_pcm_dir is not None  # only called in clang mode
        # Pull the destination from the registry the pre-pass already
        # populated. When cas-pcmdir is on this is the cached
        # ``<cas_pcmdir>/<command_hash>/<name>.pcm`` path; when off it's
        # the flat per-build path. Falling back to the flat layout keeps
        # the rule emission correct even if the registry lookup misses
        # for some reason (e.g., a multi-export TU that bypassed the
        # pre-pass — though we reject those upstream).
        pcm_path = self._module_iface_pcm.get(
            module_name,
            os.path.join(self._module_pcm_dir, _module_pcm_filename(module_name)),
        )
        pcm_dir = os.path.dirname(pcm_path)

        common_cmd = (
            compiletools.utils.split_command_cached(self.args.CXX)
            + list(self.args.flags.cxx)
            + [str(flag) for flag in magic_cpp_flags]
            + [str(flag) for flag in magic_cxx_flags]
        )

        # System-provided module sources (libc++'s std.cppm) need their
        # own extras (-stdlib=libc++, -Wno-reserved-module-identifier)
        # injected into BOTH the precompile and the .pcm-to-.o stage so
        # the same flags drive every cc1 invocation.
        common_cmd = common_cmd + self._system_module_extra_flags(filename)

        # The primary interface unit may `export import :P;`, which means
        # its --precompile invocation needs to find the partition .pcm
        # files. -fprebuilt-module-path doesn't resolve partition names
        # in clang, so we hand it the same per-partition mapping that
        # importers get.
        partition_flags = self._clang_partition_module_file_flags()

        # Same anchor-relative source + cwd discipline as _create_pch_rules:
        # without it, clang's .pcm bakes the absolute source path into its
        # internal path-table, leaking per-user paths into every consumer's
        # .o via .debug_line_str. See BuildRule.cwd / _create_pch_rules.
        if self._anchor_root and _is_under(filename, self._anchor_root):
            pcm_source_for_cmd = os.path.relpath(filename, self._anchor_root)
            pcm_rule_cwd: str | None = self._anchor_root
        else:
            pcm_source_for_cmd = filename
            pcm_rule_cwd = None
        precompile_cmd = (
            common_cmd
            + partition_flags
            + [
                "-x",
                "c++-module",
                "--precompile",
                pcm_source_for_cmd,
                "-o",
                pcm_path,
            ]
        )
        # Clang produces the .pcm with its own consumer in mind; pcm_dir
        # is the order-only mkdir gate.
        pcm_rule = BuildRule(
            output=pcm_path,
            inputs=prerequisites,
            command=precompile_cmd,
            rule_type="compile",
            order_only_deps=[pcm_dir],
            cwd=pcm_rule_cwd,
        )

        # Stage 2: compile the .pcm into the linkable .o. The .pcm is the
        # only real input -- include weight is unchanged from the source
        # form because the same translation unit's headers were processed
        # during stage 1.
        #
        # The same ``-fmodule-file=NAME=PATH`` flags are required here
        # too: clang needs to resolve any partition references that were
        # baked into the .pcm in stage 1 (e.g. a primary that did
        # ``export import :basic;``). Without them stage 2 fails with
        # ``failed to find module file for module 'M:P'``.
        bucket_dir = os.path.dirname(obj_name)
        obj_cmd = common_cmd + partition_flags + ["-c", pcm_path, "-o", obj_name]
        obj_rule = BuildRule(
            output=obj_name,
            inputs=[pcm_path],
            command=obj_cmd,
            rule_type="compile",
            order_only_deps=[bucket_dir],
        )
        return pcm_rule, obj_rule

    def _create_compile_rule(self, filename: str) -> BuildRule:
        """Create a compile BuildRule for a single source file."""
        deplist = self.hunter.header_dependencies(filename)
        prerequisites = [filename] + sorted([str(dep) for dep in deplist])

        import stringzilla as sz

        magicflags = self.hunter.magicflags(filename)

        # Add PCH .gch dependency if this source uses a precompiled header.
        # Wire the cached .gch into the consumer compile via `-include`.
        pch_include_flags: list[str] = []
        for pch_header in magicflags.get(sz.Str("PCH"), []):
            pch_header_str = str(pch_header)
            gch_path = self._pch_gch_paths.get(pch_header_str, _gch_path(pch_header_str))
            if gch_path not in prerequisites:
                prerequisites.append(gch_path)
            include_dir = self._pch_include_dirs.get(pch_header_str)
            if include_dir:
                # Must be `-include <cache>/<header>`, NOT `-I <cache>`.
                # GCC's `#include "header"` resolution searches the
                # source-file directory before any `-I` dir, so `-I <cache>`
                # is bypassed whenever the PCH header coexists with the
                # consumer source — common for private per-TU PCH. The
                # absolute `-include` form opens <cache>/<header>.gch
                # unconditionally (PCH lookup is sibling-to-resolved-path).
                # See examples-features/pch_bypass_bug/.
                staged_h = os.path.join(include_dir, os.path.basename(pch_header_str))
                pch_include_flags.extend(["-include", staged_h])

        dep_hash = self.namer.compute_dep_hash(deplist)
        macro_state_hash = self.hunter.macro_state_hash(filename, dep_hash=dep_hash)
        obj_name = self.namer.object_pathname(filename, macro_state_hash, dep_hash)

        magic_cpp_flags = magicflags.get(sz.Str("CPPFLAGS"), [])
        if compiletools.utils.is_c_source(filename):
            magic_c_flags = magicflags.get(sz.Str("CFLAGS"), [])
            compile_cmd = (
                compiletools.utils.split_command_cached(self.args.CC)
                + list(self.args.flags.c)
                + pch_include_flags
                + [str(flag) for flag in magic_cpp_flags]
                + [str(flag) for flag in magic_c_flags]
            )
        else:
            magic_cxx_flags = magicflags.get(sz.Str("CXXFLAGS"), [])
            compile_cmd = (
                compiletools.utils.split_command_cached(self.args.CXX)
                + list(self.args.flags.cxx)
                + pch_include_flags
                + [str(flag) for flag in magic_cpp_flags]
                + [str(flag) for flag in magic_cxx_flags]
            )
            # C++20 modules: any TU that participates in the module
            # graph (exports, implements, or imports a named module) needs
            # the compiler's modules-mode flag injected. We inject here
            # rather than in args.flags so non-module TUs in the same
            # build aren't tagged with -fmodules-ts (which would invalidate
            # their object cache) and so the user's ct.conf stays compiler-
            # agnostic at the C++20 level.
            module_extra = self._compiler_module_flags_for(filename)
            compile_cmd.extend(module_extra)
            # System-provided module sources (e.g. libc++'s std.cppm)
            # need extra flags that the user can't reasonably set in
            # their ct.conf, since the system source isn't visible to
            # them. Inject here, AFTER per-TU module flags so any later
            # extras override.
            compile_cmd.extend(self._system_module_extra_flags(filename))

        if self.args.dynamic and filename in self._dynamic_sources:
            compile_cmd.append("-fPIC")

        # gcc < 15 doesn't recognize the ``.cppm`` extension as C++ source —
        # without an explicit ``-x c++`` it treats ``math.cppm`` as a linker
        # input and emits "linker input file unused because linking not
        # done" under -c, leaving no .o for the producer-side rename to
        # land. gcc 15+ added native .cppm recognition, so the coercion is
        # a no-op there. (Verified 2026-05-13 across gcc-12.3.0, gcc-13.2.0,
        # gcc-14.3.0, gcc-15.2.0, gcc-16.1.0.) Scope the override to this
        # single source by placing it immediately before the filename. Only
        # inject for the gcc CXX path; clang has its own --precompile flow
        # for .cppm interface units (see _create_clang_module_interface_rules).
        source_prefix: list[str] = []
        if (
            self._module_compiler_kind == "gcc"
            and not compiletools.utils.is_c_source(filename)
            and filename.endswith(".cppm")
        ):
            source_prefix = ["-x", "c++"]

        compile_cmd.extend(["-c", *source_prefix, filename, "-o", obj_name])

        # The bucket dir is the immediate parent of the sharded object path
        # (``<objdir>/<file_hash[:2]>``). Gating only on the per-target
        # bucket — not on the bare objdir — splits concurrent rename
        # contention across 256 directory inodes instead of serializing
        # every writer on the same parent.
        bucket_dir = os.path.dirname(obj_name)
        return BuildRule(
            output=obj_name,
            inputs=prerequisites,
            command=compile_cmd,
            rule_type="compile",
            order_only_deps=[bucket_dir],
        )

    def _merge_ldflags_for_sources(self, sources: list[str]) -> list[str]:
        """Collect per-file LDFLAGS and hard orderings, then merge via topo sort.

        Consumer side of the ``_HARD_ORDERINGS_KEY`` contract. The producer
        is ``magicflags._handle_pkg_config`` (see the comment block above
        ``magicflags._HARD_ORDERINGS_KEY`` for the full producer-side
        contract).

        Per-file invariants:

        * The ``_HARD_ORDERINGS_KEY`` sentinel MUST be popped (or filtered)
          out of the per-file ``magic_flags`` dict before that dict is
          consumed elsewhere as a flat flag list — otherwise the sentinel
          leaks out as a fake compiler flag. This method reads the key
          via ``magic_flags.get(_HARD_ORDERINGS_KEY, [])``; any other
          consumer of the per-file flags dict must do the same.
        * The aggregated value forwarded to
          ``utils.merge_ldflags_with_topo_sort(hard_orderings=...)`` is a
          ``list[tuple[str, str]]`` of pairwise ``(pred_lib, succ_lib)``
          constraints. Library names appear without the ``-l`` prefix,
          matching what ``_handle_pkg_config`` produces.
        * Source-file provenance is preserved in a parallel
          ``hard_ordering_sources`` list whose indices align 1:1 with the
          flattened ``hard_orderings`` list. ``merge_ldflags_with_topo_sort``
          uses these source paths in cycle-error messages so the user
          can find the contradictory ``//#PKG-CONFIG=`` annotations.
        """
        import stringzilla as sz

        per_file_ldflags = []
        ldflags_source_files = []
        hard_orderings = []
        hard_ordering_sources = []
        for s in sources:
            magic_flags = self.hunter.magicflags(s)
            file_ldflags = magic_flags.get(sz.Str("LDFLAGS"), [])
            if file_ldflags:
                per_file_ldflags.append(list(file_ldflags))
                ldflags_source_files.append(s)
            for pred, succ in magic_flags.get(_HARD_ORDERINGS_KEY, []):
                hard_orderings.append((str(pred), str(succ)))
                hard_ordering_sources.append(s)

        return compiletools.utils.merge_ldflags_with_topo_sort(
            per_file_ldflags,
            source_files=ldflags_source_files,
            hard_orderings=hard_orderings or None,
            hard_ordering_sources=hard_ordering_sources or None,
        )

    def _link_libcxx_extras_if_needed(self, merged_ldflags: list[str], ld_extra: list[str]) -> list[str]:
        """Return ``["-stdlib=libc++"]`` when the link driver must select
        libc++ to match objects already compiled against it; else ``[]``.

        Gated on clang as the link driver: a g++ link rejects
        ``-stdlib=libc++`` as an unrecognized option, which is the right
        diagnostic for a mixed-toolchain misconfiguration rather than a
        silent libstdc++ link of libc++ objects.
        """
        if not self._compile_used_libcxx:
            return []
        if "-stdlib=libc++" in ld_extra or "-stdlib=libc++" in merged_ldflags:
            return []
        if compiletools.apptools.compiler_kind(self.args.LD) != "clang":
            return []
        return ["-stdlib=libc++"]

    def _object_pathname_for_source(self, source: str) -> str:
        """Compute an object-file path for ``source`` using the per-TU
        cache-key scope filter.

        Centralises the (dep_hash, macro_state_hash) computation so that
        the dep_hash is always passed into ``hunter.macro_state_hash`` --
        without it the cmdline ``-D`` scope filter is skipped and the
        hash falls back to including every cmdline ``-D`` macro (the
        pre-fix pollution behaviour).
        """
        dep_hash = self.namer.compute_dep_hash(self.hunter.header_dependencies(source))
        macro_state_hash = self.hunter.macro_state_hash(source, dep_hash=dep_hash)
        return self.namer.object_pathname(source, macro_state_hash, dep_hash)

    @classmethod
    def _self_manages_exe_placement(cls) -> bool:
        """Whether this backend manages its own linker-artefact placement
        (executables, static libraries, shared libraries) and so should
        NOT be wrapped in compiletools' cas-exedir layer.

        False (default) — backend writes whatever path the graph IR
        names, so compiletools' cas-exedir layer applies. The producer
        rule (link / static_library / shared_library) writes to a
        content-addressable ``<cas-exedir>/<shard>/<name>_<key>.<ext>``
        with ``<ext>`` ∈ ``{.exe, .a, .so}``, paired with a downstream
        ``symlink`` rule that publishes the user-facing ``bin/<variant>/<name>``
        as a hard link (with symlink fallback) to the cached artefact.
        This is the case for Make/Ninja/Shake.

        True — backend writes binaries to a tool-managed location
        (cmake's out-of-source build tree, bazel's sandboxed action-
        cache output) and post-build-copies / publishes them to the
        user-facing path itself. Routing through cas-exedir would just
        dangle because the backend never writes there. All three rule
        types use the legacy single-rule shape that writes directly to
        the user-facing path.

        Note: this is a *placement-management* predicate, not a claim
        of content-addressability. Bazel does back it with a real CAS
        (action cache keyed by input hashes); cmake does not — cmake's
        incremental rebuild is mtime-and-depgraph based. Both qualify
        for the same dispatch because both reasons (real CAS, or
        tool-owned out-of-source tree) make the cas-exedir wrapper
        wrong, not because both have a CAS.
        """
        return False

    @classmethod
    def _has_native_cas_obj(cls) -> bool:
        """Whether this backend already has its own content-addressable
        cache for compile artefacts (``.o`` files) and so does NOT route
        every compile through compiletools' cas-objdir layer.

        False (default) — every compile produces a ``.o`` under
        compiletools' cas-objdir (the path-canonical CAS key keyed by
        file_hash + dep_hash + macro_state_hash). Make/Ninja/Shake
        all populate cas-objdir for every TU. CMake also returns False
        here even though ``_self_manages_exe_placement()`` is True:
        cmake uses ``cas-objdir/cmake-build/`` as its out-of-source
        build tree, so ``.o`` files do land under cas-objdir.

        True — backend has its own action cache for compiles and only
        writes to cas-objdir for the narrow set of artefacts that must
        cross the action-cache boundary (e.g. bazel's C++20 named-module
        interface ``.o`` staging via ``_bazel_obj_workspace_relative``).
        Samples without named-module exports will leave cas-objdir
        empty under such a backend.
        """
        return False

    def _honors_use_mtime(self) -> bool:
        """Whether this backend's emitted rules honor ``args.use_mtime``.

        ``--use-mtime`` controls how compile/link prerequisites are
        emitted: in CAS-only mode (``False``, the default) sources are
        dropped from prereqs and CAS-artefact existence is the rebuild
        signal; in legacy mode (``True``) classical mtime semantics
        apply so touching a source triggers a rebuild. Only the rule
        emitters in ``makefile_backend`` and ``ninja_backend`` actually
        switch on this flag — Make and Ninja are the two backends that
        consume the prereq list as a literal mtime comparison.

        Other backends ignore the flag because their underlying
        change-detection isn't mtime-based:

        * **CMake** delegates to its own out-of-source incremental
          tracking (cmake-build/) and copies built artefacts to
          ``topbindir`` post-build.
        * **Bazel** uses its content-addressable action cache.
        * **Shake** uses verifying traces (content hashes).

        For all three, ``--use-mtime=True`` cannot deliver "touch the
        source to force a rebuild" semantics — a touch without a
        content change is invisible to a content-hash-based rebuild
        check. Backends in this group leave this method at the False
        default; ``BuildBackend.__init__`` then warns the user that
        their explicit opt-in is being silently ignored.
        """
        return False

    def _compute_artefact_key_hash(self, payload: dict) -> str:
        """Hash a CAS-key payload deterministically. Centralised so the
        executable / static-library / shared-library key formats stay
        in lockstep. Use ``sort_keys=True`` on encode so the final
        digest is independent of insertion order in *payload*.
        """
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _build_publish_rule(
        self,
        cas_path: str,
        user_path: str,
        *,
        source_realpath: str | None = None,
    ) -> BuildRule:
        """Construct the ``symlink`` rule that publishes a CAS artefact
        at its user-facing path.

        Recipe: ``ct-cas-publish --cas-path X --user-path Y [--source-realpath S]``.
        See ``compiletools/cas_publish.py`` for the full contract; in
        short, it does ``link(X, tmp); rename(tmp, Y)`` (POSIX-atomic,
        no missing-Y window for concurrent readers — fixes I1) and
        falls back to ``symlink(X, tmp); rename(tmp, Y)`` ONLY on
        ``EXDEV`` (other ``OSError``s surface visibly — fixes I2).

        ``source_realpath`` (when provided) is written into a sidecar
        manifest at ``<cas_path>.manifest`` and consumed by
        ``trim_cache.trim_exedir`` to bucket entries by source identity
        instead of by basename — fixes the C4 collision where two
        executables both named ``main`` would prematurely evict each
        other.

        Race semantics: two peer ``ct-cake`` invocations publishing the
        SAME ``user_path`` from DIFFERENT ``cas_path``s race on the
        rename. POSIX ``rename(tmp, user_path)`` is atomic; the final
        winner is unspecified but both targets are byte-equivalent when
        their CAS keys collide so any winner is correct. Processes
        holding ``user_path`` open during a re-publish keep the prior
        inode (open file descriptors pin the inode); next exec picks
        up the new inode.
        """
        publish_cmd = [
            "ct-cas-publish",
            "--cas-path",
            cas_path,
            "--user-path",
            user_path,
        ]
        if source_realpath:
            publish_cmd += ["--source-realpath", source_realpath]
        return BuildRule(
            output=user_path,
            inputs=[cas_path],
            command=publish_cmd,
            rule_type="symlink",
            order_only_deps=[os.path.dirname(user_path)],
        )

    def _create_link_rule(self, source: str, library_outputs: list[str] | None = None) -> list[BuildRule]:
        """Build the link rule(s) for an executable target.

        When ``_self_manages_exe_placement()`` returns False (the
        default for Make/Ninja/Shake), returns a two-element list:
          [0] The link rule whose output is the content-addressable
              ``<cas-exedir>/<shard>/<name>_<linkkey>.exe``. ``linkkey``
              hashes the canonicalized link command and the linker
              identity, so two link invocations with identical
              content-relevant inputs share the cache entry across
              workspaces.
          [1] A ``symlink`` rule whose output is the user-facing
              ``bin/<variant>/<name>`` (the path users have always run).
              See ``_build_publish_rule`` for the publish recipe and
              its race semantics.

        When ``_self_manages_exe_placement()`` returns True
        (cmake/bazel), returns a single-element list with the legacy
        ``bin/<name>`` link rule.

        .. note:: Signature change from earlier compiletools versions:
           previously returned a single ``BuildRule``. The two-element
           shape is required so the cas-exedir layer can distinguish
           the producer rule (cas-exe) from the publish step
           (bin/<name>). Out-of-tree backend subclasses overriding
           ``_create_link_rule`` must update accordingly.

        Callers iterate the returned list and add each rule to the
        graph (callers also continue to reference the user-facing
        ``executable_pathname`` for downstream test/build deps; the
        symlink rule's output IS that path).
        """
        completesources = self.hunter.required_source_files(source)
        real_source = compiletools.wrappedos.realpath(source)
        exename = self.namer.executable_pathname(real_source)

        object_names = compiletools.utils.ordered_unique([self._object_pathname_for_source(s) for s in completesources])

        merged_ldflags = self._merge_ldflags_for_sources(completesources)
        ld_argv = compiletools.utils.split_command_cached(self.args.LD)

        extra_link_argv: list[str] = []
        link_inputs_for_graph = list(object_names)
        if library_outputs:
            # Libraries mirror their source dirs under bindir, so each
            # library contributes its own -L directory (ordered-unique).
            lib_dirs = compiletools.utils.ordered_unique(
                [os.path.dirname(lib_output) for lib_output in library_outputs]
            )
            extra_link_argv.extend(f"-L{lib_dir}" for lib_dir in lib_dirs)
            for lib_output in library_outputs:
                lib_basename = os.path.basename(lib_output)
                if lib_basename.startswith("lib"):
                    lib_name = lib_basename[3:]  # strip "lib" prefix
                    lib_name = os.path.splitext(lib_name)[0]  # strip extension
                    extra_link_argv.append(f"-l{lib_name}")
                link_inputs_for_graph.append(lib_output)

        ld_extra = list(self.args.flags.ld) if self.args.flags.ld else []
        ld_extra.extend(self._link_libcxx_extras_if_needed(merged_ldflags, ld_extra))

        # Round 3: rewrite workspace-rooted paths in the EMITTED link
        # argv so embedded RPATHs / version-script paths / -L paths
        # produce byte-identical binaries across users with different
        # checkout paths. Cache key still uses canonicalize_for_cache_key
        # below (sentinel form); only the actual command is target-
        # substituted. anchor_root is captured per backend instance and
        # is empty when no gitroot resolves -- in that case
        # canonicalize_for_command is the identity, leaving the argv
        # unchanged.
        ffile_prefix_map_target = getattr(self.args, "ffile_prefix_map_target", ".")
        merged_ldflags_for_cmd = compiletools.apptools.canonicalize_for_command(
            list(merged_ldflags), self._anchor_root, target=ffile_prefix_map_target
        )
        ld_extra_for_cmd = compiletools.apptools.canonicalize_for_command(
            ld_extra, self._anchor_root, target=ffile_prefix_map_target
        )
        extra_link_argv_for_cmd = compiletools.apptools.canonicalize_for_command(
            extra_link_argv, self._anchor_root, target=ffile_prefix_map_target
        )
        # wild-B axis: append -B<absolute_dir> after the canonicalised
        # portions so canonicalize_for_command never sees it (rewriting an
        # absolute -B path to "./..." breaks under subdir invocation, since
        # link rules don't run with cwd=anchor_root and gcc would silently
        # fall through to the default linker — see _normalize_wild_linker).
        # The wild-B variant is already in the link key via canonical_bindir,
        # so the per-user wild path doesn't need to enter the key payload.
        wild_b_argv = _wild_b_link_argv(self.args)

        if self._self_manages_exe_placement():
            # Backend already has its own CAS layer — emit the legacy
            # single-rule shape that writes directly to bin/<name>.
            link_cmd = (
                ld_argv
                + ["-o", exename]
                + list(object_names)
                + merged_ldflags_for_cmd
                + extra_link_argv_for_cmd
                + ld_extra_for_cmd
                + wild_b_argv
            )
            return [
                BuildRule(
                    output=exename,
                    inputs=link_inputs_for_graph,
                    command=link_cmd,
                    rule_type="link",
                    order_only_deps=[os.path.dirname(exename)],
                )
            ]

        # link_key_hash inputs: linker identity + LDFLAGS + objects +
        # libs + ld extras + bindir basename (defence against
        # rpath/$ORIGIN linker scripts that bake bindir into the binary
        # — a cross-bindir collision would otherwise silently produce a
        # cache hit with the wrong embedded RPATH) + link_environment
        # (env vars the linker reads or that flow through to the binary
        # bytes: SOURCE_DATE_EPOCH bakes into .note.gnu.build-id, the
        # *_LIBRARY_PATH families control which libfoo.so resolves -lfoo).
        # Source path is NOT in the hash — different sources naturally
        # hash to different link keys via their object names, and
        # including the source path would defeat workspace-portability.
        anchor_root = self._anchor_root
        link_key_payload = {
            "linker_identity": _compiler_identity(ld_argv[0], anchor_root=anchor_root) if ld_argv else "",
            "ld_argv": compiletools.apptools.canonicalize_paths_for_cache_key(ld_argv, anchor_root),
            "objects": sorted(compiletools.apptools.canonicalize_paths_for_cache_key(object_names, anchor_root)),
            "merged_ldflags": compiletools.apptools.canonicalize_for_cache_key(list(merged_ldflags), anchor_root),
            "extra_link_argv": compiletools.apptools.canonicalize_for_cache_key(extra_link_argv, anchor_root),
            "library_outputs": sorted(
                compiletools.apptools.canonicalize_paths_for_cache_key(library_outputs or [], anchor_root)
            ),
            "ld_extra": compiletools.apptools.canonicalize_for_cache_key(ld_extra, anchor_root),
            # canonical_bindir: the artefact's own anchor-relative mirrored
            # directory, not just a basename. The original
            # ``bindir_basename`` defence (C5) was wrong-headed in two
            # ways: (1) ``$ORIGIN``-relative RPATH is resolved at runtime
            # by ld.so against wherever the binary lives, so identical
            # RPATH text behaves identically across bindirs of the same
            # shape — basename gives no extra discrimination; (2) two
            # SIBLING bindirs ``bin/blank`` and ``out/blank`` shared
            # basename and silently collided. The canonicalised full
            # publish directory disambiguates without breaking
            # workspace-portability (gitroot-A/bin/blank and
            # gitroot-B/bin/blank canonicalise to the same string).
            "canonical_bindir": compiletools.apptools.canonicalize_path_for_cache_key(
                self.namer.executable_dir(real_source), anchor_root
            ),
            "link_environment": _link_environment_snapshot(),
        }
        link_key_hash = self._compute_artefact_key_hash(link_key_payload)

        cas_exe_path = self.namer.cas_exe_pathname(real_source, link_key_hash)
        cas_exe_bucket = os.path.dirname(cas_exe_path)

        link_cmd = (
            ld_argv
            + ["-o", cas_exe_path]
            + list(object_names)
            + merged_ldflags_for_cmd
            + extra_link_argv_for_cmd
            + ld_extra_for_cmd
            + wild_b_argv
        )

        link_rule = BuildRule(
            output=cas_exe_path,
            inputs=link_inputs_for_graph,
            command=link_cmd,
            rule_type="link",
            order_only_deps=[cas_exe_bucket],
        )
        return [link_rule, self._build_publish_rule(cas_exe_path, exename, source_realpath=real_source)]

    def _get_library_object_names(self, sources: list[str]) -> tuple[list[str], list[str]]:
        """Get object file names and source files for library targets.

        Returns:
            (object_names, all_source_files) tuple.
        """
        # Use ordered_union instead of set() to preserve deterministic
        # ordering — required for stable link commands and CA cache hits.
        all_source_files = compiletools.utils.ordered_union(
            *(self.hunter.required_source_files(source) for source in sources)
        )

        object_names = compiletools.utils.ordered_unique(
            [self._object_pathname_for_source(s) for s in all_source_files]
        )
        return object_names, all_source_files

    def _create_static_library_rule(self) -> list[BuildRule]:
        """Build the static-library rule(s) for ``args.static``.

        When ``_self_manages_exe_placement()`` returns False (the
        default for Make/Ninja/Shake), returns a two-element list:
          [0] The ``ar`` rule whose output is the content-addressable
              ``<cas-exedir>/<shard>/lib<name>_<libkey>.a``. ``libkey``
              hashes the canonicalized object set + ar argv, so two
              ``ar`` invocations with identical content-relevant
              inputs share the cache entry across workspaces.
          [1] A ``symlink`` rule publishing the user-facing
              ``bin/<variant>/lib<name>.a`` as a hard link (with
              symlink fallback) to the cas-static-library entry.

        When ``_self_manages_exe_placement()`` returns True, returns a
        single-element list with the legacy direct-output shape.

        Same lift-to-order-only treatment in make/ninja backends as
        the link rule when ``--use-mtime=False`` (default).
        """
        sourcefilename = compiletools.wrappedos.realpath(self.args.static[0])
        object_names, _ = self._get_library_object_names(self.args.static)
        lib_path = self.namer.staticlibrary_pathname(sourcefilename)

        if self._self_manages_exe_placement():
            lib_cmd = ["ar", "-src", lib_path] + list(object_names)
            return [
                BuildRule(
                    output=lib_path,
                    inputs=list(object_names),
                    command=lib_cmd,
                    rule_type="static_library",
                    order_only_deps=[os.path.dirname(lib_path)],
                )
            ]

        anchor_root = self._anchor_root
        # ar_binary: honour args.AR if provided; otherwise the literal
        # "ar" gets resolved against PATH at exec time. The identity
        # captures the binary the user actually runs (binutils version
        # determines BSD vs SysV archive format, compressed-debug
        # encoding, deterministic-mode default — all observable in the
        # output bytes and therefore part of the cache key contract).
        ar_binary = getattr(self.args, "AR", None) or "ar"
        ar_argv_prefix = [ar_binary, "-src"]
        lib_key_payload = {
            "ar_argv_prefix": compiletools.apptools.canonicalize_paths_for_cache_key(ar_argv_prefix, anchor_root),
            "ar_identity": _compiler_identity(ar_binary, anchor_root=anchor_root),
            "objects": sorted(compiletools.apptools.canonicalize_paths_for_cache_key(object_names, anchor_root)),
        }
        lib_key_hash = self._compute_artefact_key_hash(lib_key_payload)
        cas_lib_path = self.namer.cas_staticlibrary_pathname(sourcefilename, lib_key_hash)
        cas_lib_bucket = os.path.dirname(cas_lib_path)

        lib_cmd = ar_argv_prefix + [cas_lib_path] + list(object_names)
        lib_rule = BuildRule(
            output=cas_lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="static_library",
            order_only_deps=[cas_lib_bucket],
        )
        return [lib_rule, self._build_publish_rule(cas_lib_path, lib_path, source_realpath=sourcefilename)]

    def _create_shared_library_rule(self) -> list[BuildRule]:
        """Build the shared-library rule(s) for ``args.dynamic``.

        Symmetric with ``_create_link_rule`` — see that docstring for
        the ``_self_manages_exe_placement`` decision and the publish-symlink
        contract. The CAS path is
        ``<cas-exedir>/<shard>/lib<name>_<libkey>.so``; ``libkey`` is
        derived from the same payload as the executable link key
        (linker identity + LDFLAGS + objects), so two shared libraries
        with identical content-relevant link inputs share a cache
        entry.
        """
        sourcefilename = compiletools.wrappedos.realpath(self.args.dynamic[0])
        object_names, all_source_files = self._get_library_object_names(self.args.dynamic)
        lib_path = self.namer.dynamiclibrary_pathname(sourcefilename)

        merged_ldflags = self._merge_ldflags_for_sources(all_source_files)
        ld_argv = compiletools.utils.split_command_cached(self.args.LD)
        ld_extra = list(self.args.flags.ld) if (self.args.LDFLAGS and self.args.flags.ld) else []
        ld_extra.extend(self._link_libcxx_extras_if_needed(merged_ldflags, ld_extra))

        # Round 3: rewrite workspace-rooted paths in the EMITTED argv;
        # see _create_link_rule for rationale. anchor_root captured per
        # backend instance.
        ffile_prefix_map_target = getattr(self.args, "ffile_prefix_map_target", ".")
        merged_ldflags_for_cmd = compiletools.apptools.canonicalize_for_command(
            list(merged_ldflags), self._anchor_root, target=ffile_prefix_map_target
        )
        ld_extra_for_cmd = compiletools.apptools.canonicalize_for_command(
            ld_extra, self._anchor_root, target=ffile_prefix_map_target
        )
        # See _create_link_rule for rationale.
        wild_b_argv = _wild_b_link_argv(self.args)

        if self._self_manages_exe_placement():
            lib_cmd = (
                ld_argv
                + ["-shared", "-o", lib_path]
                + list(object_names)
                + merged_ldflags_for_cmd
                + ld_extra_for_cmd
                + wild_b_argv
            )
            return [
                BuildRule(
                    output=lib_path,
                    inputs=list(object_names),
                    command=lib_cmd,
                    rule_type="shared_library",
                    order_only_deps=[os.path.dirname(lib_path)],
                )
            ]

        anchor_root = self._anchor_root
        lib_key_payload = {
            "linker_identity": _compiler_identity(ld_argv[0], anchor_root=anchor_root) if ld_argv else "",
            "ld_argv": compiletools.apptools.canonicalize_paths_for_cache_key(ld_argv, anchor_root),
            "shared": True,
            "objects": sorted(compiletools.apptools.canonicalize_paths_for_cache_key(object_names, anchor_root)),
            "merged_ldflags": compiletools.apptools.canonicalize_for_cache_key(list(merged_ldflags), anchor_root),
            "ld_extra": compiletools.apptools.canonicalize_for_cache_key(ld_extra, anchor_root),
            "canonical_bindir": compiletools.apptools.canonicalize_path_for_cache_key(
                self.namer.executable_dir(sourcefilename), anchor_root
            ),
            "link_environment": _link_environment_snapshot(),
        }
        lib_key_hash = self._compute_artefact_key_hash(lib_key_payload)
        cas_lib_path = self.namer.cas_dynamiclibrary_pathname(sourcefilename, lib_key_hash)
        cas_lib_bucket = os.path.dirname(cas_lib_path)

        lib_cmd = (
            ld_argv
            + ["-shared", "-o", cas_lib_path]
            + list(object_names)
            + merged_ldflags_for_cmd
            + ld_extra_for_cmd
            + wild_b_argv
        )
        lib_rule = BuildRule(
            output=cas_lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="shared_library",
            order_only_deps=[cas_lib_bucket],
        )
        return [lib_rule, self._build_publish_rule(cas_lib_path, lib_path, source_realpath=sourcefilename)]


# Backward-compat alias: ``compiler_identity`` was promoted to
# ``compiletools.apptools`` so it can be shared with ``preprocessing_cache``
# (per-TU object cache key). Keep the original name available so the
# link-key / ar-identity call sites in this module remain unchanged. The
# PCH helpers that also referenced this alias moved to ``backend_pch``,
# which carries its own identical alias.
_compiler_identity = compiletools.apptools.compiler_identity
