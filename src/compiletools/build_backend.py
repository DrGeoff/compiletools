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
import functools
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from typing import NamedTuple, TypeVar

import compiletools.apptools
import compiletools.diagnostics
import compiletools.filesystem_utils
import compiletools.git_utils
import compiletools.global_hash_registry
import compiletools.namer
import compiletools.test_framework
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_graph import BuildGraph, BuildRule, RuleType
from compiletools.locking import execute_compile_rule, execute_link_rule
from compiletools.magicflags import _HARD_ORDERINGS_KEY
from compiletools.test_framework import TestFramework


class ObjInfo(NamedTuple):
    """Per-object compile metadata extracted from a BuildGraph compile rule."""

    source: str
    headers: list[str]
    copts: list[str]


# Sentinel string used to escape characters that are unsafe in make
# targets / filesystem paths (``:`` for module partition separator,
# ``/`` for header-name path separator). ``^^`` is chosen because:
#   - underscore (``_``) collides: identifiers and headers commonly
#     contain it, so escaping ``/`` to ``_`` could make ``<sys/socket.h>``
#     and a real ``<sys_socket.h>`` map to the same filename;
#   - hyphen (``-``) is technically allowed in some module-name proposals
#     and can also appear in real header filenames;
#   - ``^^`` does not appear in any C identifier, any module-name token
#     (which is a dotted ``[A-Za-z_][A-Za-z0-9_]*``), or any reasonable
#     header path, so collisions are vanishingly unlikely;
#   - ``^`` has no special meaning to make outside the ``$^`` automatic
#     variable (which requires the ``$``), so doubled ``^^`` is safe.
_NAME_ESCAPE = "^^"


# File extensions of build artefacts that compile rules consume but
# whose mtime must not invalidate the cached object. PCH (.gch), C++20
# module BMIs (.pcm/.gcm), gcc named-module producer objects (.o, the
# carrier of the .gcm side-effect), and gcc no-cache header-unit
# stamps (.stamp) are all produced by sibling rules; the consumer
# compile rule needs them to *exist* before it runs (build ordering),
# but their mtime is irrelevant — content changes are captured via
# the consumer's own dep_hash, which is encoded in the CAS object
# name. In CAS-only mode (``not args.use_mtime``), make and ninja
# backends lift these from normal prerequisites to order-only deps so
# build ordering is preserved without triggering spurious rebuilds.
#
# Why ``.o`` is here even though it's also a normal compile output:
# a named-module importer's compile rule lists the interface unit's
# .o in its ``inputs`` (added by ``_wire_module_inputs``). Without
# .o in this list the CAS-only path's ``ordering_inputs_for_compile``
# filter would drop it, leaving the importer to race the producer.
# Plain compile rules don't list other .o files in their inputs, so
# the entry is effectively a no-op outside the module path.
_COMPILE_ORDERING_INPUT_EXTS = (".gch", ".pcm", ".gcm", ".o", ".stamp")


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


def ordering_inputs_for_compile(inputs: list[str]) -> list[str]:
    """Return inputs that must remain as ordering deps in CAS-only mode.

    See ``_COMPILE_ORDERING_INPUT_EXTS`` for the complete list of artefact
    types that need build-ordering preservation. Ordinary sources/headers
    are dropped (their content participates in the CAS object name).
    """
    return [inp for inp in inputs if inp.endswith(_COMPILE_ORDERING_INPUT_EXTS)]


# Producer rule types whose outputs are content-addressable: their cached
# path encodes the relevant cache key (per-TU object hash for compile,
# link/ar key for link/library) so existence on disk is the sole rebuild
# signal in CAS-only mode. Backends that emit these rules demote inputs
# to order-only so the build tool doesn't retrigger the producer when
# inputs change but the resulting bytes are already cached.
CAS_PRODUCER_TYPES = frozenset(
    {
        RuleType.COMPILE,
        RuleType.LINK,
        RuleType.STATIC_LIBRARY,
        RuleType.SHARED_LIBRARY,
    }
)


def cas_demoted_order_only(rule) -> list[str]:
    """Inputs that become order-only deps for a CAS producer rule.

    Compile rules keep PCH/BMI artefacts as ordering deps and drop
    sources/headers entirely. Link/library rules demote all object
    inputs (the link key already covers them).
    """
    if rule.rule_type == RuleType.COMPILE:
        return ordering_inputs_for_compile(rule.inputs)
    return list(rule.inputs)


# Environment variables the linker reads (or that flow through to the
# binary bytes). Folded into linker-artefact CAS keys so two runs with
# different values don't share a cache entry that bakes the wrong byte
# pattern. Keep this list narrow — adding rarely-used vars dilutes hit
# rate without buying real safety. Justifications:
#   * SOURCE_DATE_EPOCH: bakes into .note.gnu.build-id and __DATE__/__TIME__;
#     reproducible-builds standard.
#   * LD_LIBRARY_PATH / LIBRARY_PATH: linker library search paths;
#     different values can resolve -lfoo to different libfoo.so.
#   * LD_PRELOAD: pathological wrapper case (dlopen-injecting linker shim).
_LINK_ENVIRONMENT_VARS = (
    "SOURCE_DATE_EPOCH",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "LD_PRELOAD",
)


def _link_environment_snapshot() -> dict[str, str]:
    """Snapshot of link-relevant env vars at call time.

    Stable across invocations within a single ct-cake run (the env
    doesn't change mid-run). Two CI runs with different values produce
    different snapshots → different cache keys. Empty/unset vars
    explicitly contribute the empty string so 'absent' and 'set to ""'
    hash identically — one less attack surface for cache-poisoning via
    env trickery.
    """
    return {var: os.environ.get(var, "") for var in _LINK_ENVIRONMENT_VARS}


def _module_pcm_filename(module_name: str) -> str:
    """Return a make-safe ``.pcm`` filename for a possibly-partitioned module.

    ``:`` is illegal in a Makefile target (make parses ``a:b`` as ``a``
    depends on ``b``), so we map the partition separator to ``^^`` for
    the on-disk filename. The clang ``-fmodule-file=NAME=PATH`` flag
    uses the real (colon-bearing) module name on the lookup side, so
    the filename is purely a storage detail. See ``_NAME_ESCAPE`` for
    why ``^^`` rather than ``-`` / ``_``.
    """
    return module_name.replace(":", _NAME_ESCAPE) + ".pcm"


def _header_unit_arg(token: str) -> str:
    """Strip the surrounding ``<...>`` or ``"..."`` from a header-unit token.

    The bare header name is what gcc's ``-x c++-system-header`` and
    clang's ``-xc++-system-header`` expect as the source argument.
    Anything else (callers should validate upstream that the token is a
    well-formed header reference) passes through unchanged.
    """
    if len(token) >= 2 and ((token[0], token[-1]) in (("<", ">"), ('"', '"'))):
        return token[1:-1]
    return token


def _header_unit_safe_stem(token: str) -> str:
    """Return a filesystem/make-safe stem for a header-unit token.

    Escape both ``/`` (path separator in nested system headers like
    ``<sys/socket.h>``) and ``:`` (which a make target parser would
    misread) to ``^^``. ``<vector>`` -> ``vector``;
    ``<sys/socket.h>`` -> ``sys^^socket.h``. See ``_NAME_ESCAPE`` for
    the rationale (we deliberately don't use ``_`` or ``-`` since
    those characters legitimately appear in real header filenames and
    would alias different headers to the same on-disk name).
    """
    bare = _header_unit_arg(token)
    return bare.replace("/", _NAME_ESCAPE).replace(":", _NAME_ESCAPE)


def _touch(path: str) -> None:
    """Create file (if missing) or bump its mtime (if present).

    The os.utime call is load-bearing: open(path, "a") only sets mtime
    when creating a new file; existing files need explicit utime to
    register a fresh mtime (used as build/test success markers).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        os.utime(path, None)


def compute_link_signature(rule: BuildRule) -> str:
    """Hash sorted input names + command. Input names are content-addressed."""
    key = json.dumps({"inputs": sorted(rule.inputs), "command": rule.command}, sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()


def _read_link_sig(output: str) -> str | None:
    try:
        with open(output + ".ct-sig") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_link_sig(output: str, sig: str) -> None:
    with open(output + ".ct-sig", "w") as f:
        f.write(sig)


def split_compound_args(args: list[str]) -> list[str]:
    """Split compound space-separated arguments (e.g. CXXFLAGS as one string).

    Uses shlex to correctly handle quoted values like -DFOO='bar baz'.
    """
    result = []
    for arg in args:
        if " " in arg:
            try:
                result.extend(shlex.split(arg))
            except ValueError:
                result.extend(arg.split())
        else:
            result.append(arg)
    return result


def extract_copts(command: list[str], *, strip_includes: bool = False) -> list[str]:
    """Extract compiler flags from a compile command.

    Strips the compiler binary, -c, source file, -o, and output file.
    When strip_includes is True, drops all -I/-isystem/-iquote flags
    (needed by Bazel which manages include paths itself).
    When False, recombines space-separated ``-I <dir>`` into ``-I<dir>``.
    """
    if not command:
        return []
    args = split_compound_args(command[1:])
    copts = []
    skip_next = False
    include_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if include_next:
            if not strip_includes:
                copts.append(f"-I{arg}")
            include_next = False
            continue
        if arg == "-c":
            continue
        if arg == "-o":
            skip_next = True
            continue
        if arg == "-I":
            include_next = True
            continue
        if strip_includes:
            if arg.startswith(("-isystem", "-iquote")):
                if arg in ("-isystem", "-iquote"):
                    skip_next = True
                continue
            if arg.startswith("-I") and len(arg) > 2:
                continue
        if not arg.startswith("-"):
            continue
        copts.append(arg)
    return copts


def extract_include_paths(command: list[str]) -> list[str]:
    """Extract include-path arguments from a compile command.

    Returns the path values from -I / -iquote / -isystem (and the
    two-token "-I path" form). Used by the Bazel backend to re-emit
    include paths via cc_binary(includes=[...]) since Bazel manages
    include paths itself and extract_copts(strip_includes=True) drops
    them.
    """
    if not command:
        return []
    args = split_compound_args(command[1:])
    paths: list[str] = []
    include_next = False
    for arg in args:
        if include_next:
            paths.append(arg)
            include_next = False
            continue
        if arg == "-I":
            include_next = True
            continue
        if arg.startswith("-I") and len(arg) > 2:
            paths.append(arg[2:])
            continue
        if arg in ("-isystem", "-iquote"):
            include_next = True
            continue
        if arg.startswith("-isystem"):
            paths.append(arg[len("-isystem") :].lstrip("="))
            continue
        if arg.startswith("-iquote"):
            paths.append(arg[len("-iquote") :].lstrip("="))
            continue
    return paths


def extract_linkopts(command: list[str], object_files: set[str]) -> list[str]:
    """Extract linker flags from a link command.

    Strips the linker binary, -o, output executable, and object file paths.
    Object-file matching is normalised via ``os.path.normpath`` on both
    sides so that ``./obj/foo.o`` and ``obj/foo.o`` are treated as the
    same file — without this, the divergent form would leak into
    linkopts and break Bazel/CMake link rules.
    """
    if not command:
        return []
    normalised_objects = {os.path.normpath(o) for o in object_files}
    args = split_compound_args(command[1:])
    linkopts = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-o":
            skip_next = True
            continue
        if os.path.normpath(arg) in normalised_objects:
            continue
        linkopts.append(arg)
    return linkopts


def build_obj_info(graph: BuildGraph, *, strip_includes: bool = False) -> dict[str, ObjInfo]:
    """Build mapping from object file path to ObjInfo(source, headers, copts).

    Args:
        graph: The BuildGraph to extract compile rules from.
        strip_includes: When True, drop -I/-isystem/-iquote flags from copts
            (needed by Bazel which manages include paths itself).
    """
    obj_info: dict[str, ObjInfo] = {}
    for rule in graph.rules_by_type("compile"):
        source = rule.inputs[0] if rule.inputs else ""
        # Filter out build-ordering artefacts -- PCH (.gch) and C++20
        # module BMIs (.pcm/.gcm). These appear in `inputs` so the
        # make/ninja CAS-only path can lift them to order-only deps and
        # so trace_backend recurses into the producer rules, but they
        # are not source/header files that cmake/bazel should list as
        # cc_binary srcs.
        headers = (
            [h for h in rule.inputs[1:] if not h.endswith(_COMPILE_ORDERING_INPUT_EXTS)] if len(rule.inputs) > 1 else []
        )
        copts = extract_copts(rule.command, strip_includes=strip_includes) if rule.command else []
        obj_info[rule.output] = ObjInfo(source, headers, copts)
    return obj_info


def mangle_target_name(basename: str) -> str:
    """Convert a filename to a valid build-system target name."""
    return basename.replace(".", "_").replace("-", "_")


def aggregate_rule_sources(
    rule: BuildRule,
    obj_info: dict[str, ObjInfo],
) -> tuple[list[str], list[str]]:
    """Collect source files and deduplicated copts from a rule's object inputs.

    Returns (source_and_header_files, deduplicated_copts).
    """
    srcs: list[str] = []
    all_copts: list[str] = []
    seen_copts: set[str] = set()
    for obj in rule.inputs:
        info = obj_info.get(obj)
        if info is None:
            continue
        if info.source:
            srcs.append(info.source)
        srcs.extend(info.headers)
        for c in info.copts:
            if c not in seen_copts:
                all_copts.append(c)
                seen_copts.add(c)
    return srcs, all_copts


def _toposort_rules(rules_by_output: dict[str, BuildRule]) -> list[BuildRule]:
    """Topologically sort build rules by their ``inputs`` dependency edges.

    Only edges whose target is itself a key in *rules_by_output* are
    treated as ordering constraints. Edges to external artefacts (source
    files, pre-existing headers) are ignored. Raises ``ValueError`` on a
    cycle (which would indicate a malformed build graph).

    Used by ``BuildBackend._prebuild_aux_artefacts`` to order named-module
    interface compile rules so partitions compile before the primary
    interface units that import them.
    """
    in_degree: dict[str, int] = {output: 0 for output in rules_by_output}
    dependents: dict[str, list[str]] = {output: [] for output in rules_by_output}
    for output, rule in rules_by_output.items():
        for inp in rule.inputs:
            if inp in rules_by_output:
                in_degree[output] += 1
                dependents[inp].append(output)

    ready: deque[str] = deque(sorted(out for out, deg in in_degree.items() if deg == 0))
    result: list[BuildRule] = []
    while ready:
        out = ready.popleft()
        result.append(rules_by_output[out])
        for dep in sorted(dependents[out]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready.append(dep)

    if len(result) != len(rules_by_output):
        unprocessed = sorted(set(rules_by_output.keys()) - {r.output for r in result})
        descriptors = [f"{out} (type={rules_by_output[out].rule_type})" for out in unprocessed]
        raise ValueError(f"_toposort_rules: cycle detected among named-module interface rules: {descriptors}")
    return result


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
    _module_compiler_kind: str | None = None
    _module_pcm_cache_root: str | None = None
    _module_pcm_dir: str | None = None
    _module_iface_obj: dict[str, str] = {}  # noqa: RUF012
    _module_iface_pcm: dict[str, str] = {}  # noqa: RUF012
    _module_iface_gcm: dict[str, str] = {}  # noqa: RUF012
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
        self._module_iface_obj: dict[str, str] = {}
        self._module_iface_pcm: dict[str, str] = {}
        self._module_iface_gcm: dict[str, str] = {}
        self._header_unit_artefact: dict[str, str] = {}
        self._gcc_module_mapper_path: str | None = None
        self._gcc_header_unit_resolved: dict[str, list[str]] = {}
        self._build_imports_std_cached: bool | None = None
        self._compile_used_libcxx = False

        # Warn if the user explicitly opted into legacy mtime semantics
        # but this backend can't deliver them. The flag is a make/ninja
        # implementation detail; other backends fall back to their
        # native (content-hash or self-managed) rebuild logic.
        # ``is True`` (not truthy) so a MagicMock attribute on a stub
        # backend in tests doesn't trip the warning.
        if getattr(args, "use_mtime", False) is True and not self._honors_use_mtime():
            print(
                f"WARNING: --use-mtime=True is set but the {self.name()!r} backend does not "
                "honor mtime-based rebuilds; the flag will be ignored. Only the 'make' and "
                "'ninja' backends honor --use-mtime — others use content-hash-based "
                "(bazel, shake, slurm) or self-managed (cmake) change detection.",
                file=sys.stderr,
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
            self._run_tests()
            return
        if self._graph is not None and self._all_outputs_current(self._graph):
            return
        self._execute_build(target)
        if self._graph is not None:
            self._record_link_signatures(self._graph)

    @abc.abstractmethod
    def _execute_build(self, target: str) -> None:
        """Backend-specific build invocation (subprocess call to native tool)."""

    def _prebuild_aux_artefacts(self) -> None:
        """Locally execute aux artefact producer rules before the native backend runs.

        Backends that emit one ``cc_binary`` / ``add_executable`` per LINK
        rule (CMake, Bazel) hand source-to-binary compilation to the
        native tool, but the PCH, header-unit, and named-module interface
        producer rules sit outside that chain — the native tool never sees
        them. Running them here lands the artefacts on disk so the per-TU
        compile commands the native tool subsequently runs find them via the
        already-baked ``-fmodule-file=`` / ``-fmodule-mapper=`` /
        ``-I <pchdir>/<hash>`` flags. Locking via ``atomic_compile`` /
        ``atomic_link`` lets peer ct-cake invocations sharing a CAS dir
        cooperate.

        Slurm submits all compiles as a flat job array with no DAG
        ordering, so named-module interface compile rules (which write
        ``.gcm`` / ``.pcm`` as side effects of producing their ``.o``)
        must also be executed locally in Phase 0 before the flat array
        is submitted. Without this, importer compiles race with interface
        compiles in the array and GCC reports "failed to read compiled
        module".

        Execution order for named-module interface rules: we topologically
        sort by ``rule.inputs`` within the interface-rule set so partitions
        are compiled before primary interfaces that import them (clang's
        precompile stage; gcc's -fmodule-mapper compile).
        """
        graph = self._graph
        if graph is None:
            return
        pch_rules = [r for r in graph.rules_by_type(RuleType.COMPILE) if r.output.endswith(".gch")]
        aux_rules = pch_rules + graph.rules_by_type(RuleType.HEADER_UNIT)

        # Named-module interface compile rules: those whose outputs appear
        # in _module_iface_obj (gcc .o, and clang .o from pcm-to-o stage)
        # or _module_iface_pcm (clang precompile .pcm stage). We must NOT
        # include _module_iface_gcm entries separately -- those .gcm paths
        # are side effects of the same gcc compile rule whose .o is already
        # in _module_iface_obj; double-executing would corrupt the output.
        # Topological sort within this set ensures partitions (whose .pcm/.o
        # appear in other interface rules' inputs) run before primary
        # interface units that import them.
        module_iface_outputs: set[str] = set(self._module_iface_obj.values()) | set(self._module_iface_pcm.values())
        if module_iface_outputs:
            iface_rules_by_output: dict[str, BuildRule] = {}
            for rule in graph.rules_by_type(RuleType.COMPILE):
                if rule.output in module_iface_outputs:
                    iface_rules_by_output[rule.output] = rule
            module_iface_rules = _toposort_rules(iface_rules_by_output)
            aux_rules = module_iface_rules + aux_rules

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

        verbose = getattr(self.args, "verbose", 0)
        for rule in aux_rules:
            # Pre-lock fast-path mirrors trace_backend._do_build:365-383.
            # The skip_if_exists=True below closes the TOCTOU window inside
            # the lock; this skip avoids the lock entirely on warm builds.
            if os.path.exists(rule.output):
                continue
            assert rule.command is not None, f"aux rule {rule.output} has no command"
            if verbose >= 1:
                print(" ".join(rule.command), file=sys.stderr)
            if rule.rule_type == RuleType.COMPILE:
                execute_compile_rule(rule.output, rule.command, self.args, skip_if_exists=True)
            else:
                # gcc's shell-pipeline header-unit form does its own producer-side
                # rename inside the pipeline; atomic_link's outer rewrite no-ops
                # (emits a one-time warning) but the rule still runs correctly.
                execute_link_rule(rule.output, list(rule.command), self.args, skip_if_exists=True)

    def clean(self) -> None:
        """Remove build artifacts. Override for backend-specific cleanup."""
        exe_dir = self.namer.executable_dir()
        obj_dir = self.namer.object_dir()
        if os.path.isdir(exe_dir):
            shutil.rmtree(exe_dir)
        if obj_dir != exe_dir and os.path.isdir(obj_dir):
            shutil.rmtree(obj_dir)

    def realclean(self, graph: BuildGraph) -> None:
        """Remove bin/ entirely and selectively clean this build's objects from the object CAS.

        Unlike clean(), which removes the entire exe_dir and obj_dir trees,
        realclean() only removes individual build products listed in the graph
        from the obj_dir.  This is important when obj_dir is a shared location
        (e.g. cas-objdir/) used by multiple sub-projects -- we must not
        destroy other sub-projects' objects.

        The exe_dir is still removed entirely since it is per-project.
        """
        exe_dir = self.namer.executable_dir()
        if os.path.isdir(exe_dir):
            shutil.rmtree(exe_dir)

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
        source_by_basename: dict[str, str] = {}
        for source in all_sources:
            exe_basename = os.path.splitext(os.path.basename(source))[0]
            mangled = mangle_target_name(exe_basename)
            source_by_basename[exe_basename] = source
            source_by_basename[mangled] = source

        for dirpath, dirs, files in os.walk(build_output_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not d.endswith(".runfiles")]
            for fname in files:
                full = os.path.join(dirpath, fname)
                if not (os.path.isfile(full) and os.access(full, os.X_OK)):
                    continue
                if fname.endswith(".cmake"):
                    continue
                if fname not in source_by_basename:
                    continue
                source = source_by_basename.pop(fname)
                exe_basename = os.path.splitext(os.path.basename(source))[0]
                mangled = mangle_target_name(exe_basename)
                source_by_basename.pop(exe_basename, None)
                source_by_basename.pop(mangled, None)
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

        # Create objdir creation rule (needed by compile rules as order-only dep)
        graph.add_rule(
            BuildRule(
                output=self.args.cas_objdir,
                inputs=[],
                command=["mkdir", "-p", self.args.cas_objdir],
                rule_type="mkdir",
            )
        )

        # Create executable dir creation rule (needed by link rules as order-only dep)
        exe_dir = self.namer.executable_dir()
        if exe_dir != self.args.cas_objdir:
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

            pch_deps = [pch_header] + sorted(str(d) for d in self.hunter.header_dependencies(pch_header))
            pch_cmd = (
                compiletools.utils.split_command_cached(self.args.CXX)
                + list(self.args.flags.cxx)
                + [str(f) for f in magic_cpp_flags]
                + [str(f) for f in magic_cxx_flags]
                + ["-x", "c++-header", pch_header, "-o", gch_path]
            )
            order_deps = [os.path.join(pchdir, cmd_hash)] if pchdir and cmd_hash else [self.args.cas_objdir]
            graph.add_rule(
                BuildRule(
                    output=gch_path,
                    inputs=pch_deps,
                    command=pch_cmd,
                    rule_type="compile",
                    order_only_deps=order_deps,
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

        module_iface_obj: dict[str, str] = {}
        module_iface_pcm: dict[str, str] = {}  # populated only for clang
        module_iface_gcm: dict[str, str] = {}  # populated for gcc + cache
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
                deplist = self.hunter.header_dependencies(filename)
                dep_hash = self.namer.compute_dep_hash(deplist)
                macro_state_hash = self.hunter.macro_state_hash(filename, dep_hash=dep_hash)
                module_iface_obj[name] = self.namer.object_pathname(filename, macro_state_hash, dep_hash)
                if self._module_pcm_dir is not None:
                    pcm_path, pcm_dir = self._clang_module_pcm_destination(filename, name)
                    module_iface_pcm[name] = pcm_path
                    pcm_mkdir_dirs.add(pcm_dir)
                if gcc_cache_active:
                    gcm_path, gcm_dir = self._gcc_module_gcm_destination(filename, name)
                    module_iface_gcm[name] = gcm_path
                    pcm_mkdir_dirs.add(gcm_dir)

        self._module_iface_obj = module_iface_obj
        self._module_iface_pcm = module_iface_pcm
        self._module_iface_gcm = module_iface_gcm

        for pcm_dir in sorted(pcm_mkdir_dirs):
            graph.add_rule(
                BuildRule(
                    output=pcm_dir,
                    inputs=[],
                    command=["mkdir", "-p", pcm_dir],
                    rule_type="mkdir",
                )
            )

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
        self._header_unit_artefact: dict[str, str] = {}
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
        for filename in all_compile_sources:
            r = self.hunter._file_analysis_result(filename)
            if r is None:
                continue
            all_header_imports.update(r.module_header_imports)
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
                    abs_paths = _resolve_system_header_abs_paths(self.args.CXX, token, std_flag=str(std_flag))
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

        # Generate the gcc module-mapper file now that every named
        # module's .gcm path and every header-unit resolution is known.
        # No-op when not gcc+cache.
        self._write_gcc_module_mapper()

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
                self._wire_module_inputs(pcm_rule, file_result)
                graph.add_rule(pcm_rule)
                self._wire_module_inputs(obj_rule, file_result)
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
            self._wire_module_inputs(rule, file_result)
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
            testprefix_parts = []
            if getattr(self.args, "TESTPREFIX", ""):
                testprefix_parts = self.args.TESTPREFIX.split()

            cas_only_results = not getattr(self.args, "use_mtime", False) and not self._has_native_cas_exe()
            test_result_paths = []
            for exe_path in test_exe_paths:
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
                test_cmd = testprefix_parts + [exe_path]
                graph.add_rule(
                    BuildRule(
                        output=result_path,
                        inputs=rule_inputs,
                        command=test_cmd,
                        rule_type="test",
                        order_only_deps=rule_order_only,
                        success_marker=result_path,
                    )
                )
                test_result_paths.append(result_path)

            graph.add_rule(BuildRule(output="runtests", inputs=test_result_paths, command=None, rule_type="phony"))
            all_deps.append("runtests")

        graph.add_rule(BuildRule(output="all", inputs=all_deps, command=None, rule_type="phony"))

        return graph

    def _result_marker_path(self, exe_path: str) -> str:
        """Return the success-marker path for ``exe_path``.

        In CAS-only mode (``--use-mtime=False``, default) for backends that
        publish via the cas-exedir layer, the marker lives at
        ``<cas_path>.result`` — sibling to the content-addressed exe — so
        the marker is keyed by exe content and survives the inode-mtime
        confusion introduced by the hard-link publish.

        In legacy mode (``--use-mtime=True``) or for native-CAS backends
        (cmake/bazel, where ``_has_native_cas_exe()`` is True and there
        is no separate publish-symlink rule), falls back to
        ``<exe_path>.result`` — bit-identical to the pre-fix behaviour.

        .. note:: Native-CAS backends (cmake / bazel) currently fall
           back to legacy ``<exe_path>.result`` semantics by *omission*,
           not by design intent — they don't emit a separate
           publish-symlink rule that ``_result_marker_path`` could resolve
           through. A future change could add an equivalent content-keyed
           marker for those backends (their own change-detection layers
           know the artefact identity); the present design just doesn't
           require it because the bug being fixed (hard-link inode mtime
           confusion) is specific to the cas-exedir publish path.
        """
        if getattr(self.args, "use_mtime", False) or self._has_native_cas_exe():
            return exe_path + ".result"
        graph = self._graph
        if graph is not None:
            rule = graph.get_rule(exe_path)
            if rule is not None and rule.rule_type == RuleType.SYMLINK and rule.inputs:
                return rule.inputs[0] + ".result"
        # Unexpected fallback: CAS-only mode but no publish-symlink rule
        # found. In production ``_run_tests`` always runs after
        # ``build_graph`` populates ``self._graph``, so reaching here means
        # either the graph wasn't populated (a custom ``execute`` override
        # in an out-of-tree backend) or the publish rule was filtered out
        # of the graph. Surface it at verbose>=2 so the silent downgrade
        # to legacy semantics is at least diagnosable.
        if getattr(self.args, "verbose", 0) >= 2:
            print(
                f"_result_marker_path: no publish-symlink rule for {exe_path}; "
                f"falling back to legacy <exe>.result (CAS-side marker disabled)",
                file=sys.stderr,
            )
        return exe_path + ".result"

    def _xml_path_for(self, exe_path: str) -> str:
        """Per-test JUnit XML path under ``--test-xml-dir``.

        Layout: ``<test-xml-dir>/<variant>/<exe_basename>.xml``. Caller
        is responsible for ensuring ``--test-xml-dir`` is set.
        """
        xml_dir = self.args.test_xml_dir
        variant = getattr(self.args, "variant", "") or ""
        return os.path.join(xml_dir, variant, os.path.basename(exe_path) + ".xml")

    def _run_tests(self) -> None:
        """Run test executables built from args.tests.

        Provides a backend-agnostic way to run tests with:
        - Result-file markers: skips tests whose marker is current. In
          CAS-only mode the marker lives next to the content-addressed
          exe (existence is sufficient — the path is content-keyed so its
          presence proves this exact exe content was previously tested).
          In legacy ``--use-mtime`` mode the marker lives next to the
          published exe and the existing ``mtime(result) >= mtime(exe)``
          check applies.
        - Parallel execution: uses ThreadPoolExecutor with args.parallel workers.
        - Serialisation: when args.serialisetests is set, forces sequential execution.
        - TESTPREFIX: honours args.TESTPREFIX (e.g., valgrind) by prepending to
          the test command.
        - JUnit XML: when ``--test-xml-dir`` is set, detects each test's
          framework (gtest / doctest / Catch2) from its transitive headers
          and appends the framework-specific XML-emit argv after exe_path.
          A test whose .result marker is current but whose XML file is
          missing is re-run to regenerate the XML; tests with no detected
          framework keep the legacy skip behaviour.
        """
        if not self.args.tests:
            return

        test_pairs = [
            (source, self.namer.executable_pathname(compiletools.wrappedos.realpath(source)))
            for source in self.args.tests
        ]

        xml_dir = getattr(self.args, "test_xml_dir", None)

        # Detect framework once per test (only when --test-xml-dir is set).
        # Cached on self._test_frameworks so _run_single_test can look up
        # the same TestFramework without re-running detection inside the
        # parallel worker.
        self._test_frameworks: dict[str, TestFramework | None] = {}
        if xml_dir:
            for source, exe_path in test_pairs:
                headers = [str(h) for h in self.hunter.header_dependencies(source)]
                framework = compiletools.test_framework.detect_framework(headers, source)
                self._test_frameworks[exe_path] = framework
                if framework is None and self.args.verbose >= 1:
                    print(
                        f"{source}: no known unit-test framework detected; skipping XML output",
                        file=sys.stderr,
                    )

            # Pre-create the variant subdirectory so parallel workers don't
            # race in os.makedirs. Lazy: nothing is created when xml_dir is
            # unset, and nothing happens here when args.tests is empty
            # (early return above).
            variant = getattr(self.args, "variant", "") or ""
            os.makedirs(os.path.join(xml_dir, variant), exist_ok=True)

        # Marker location and currency rule come from master:
        # ``_result_marker_path`` resolves the CAS-side marker for cas-exedir
        # backends (presence-only, content-keyed) and falls back to the
        # legacy ``<exe>.result`` for ``--use-mtime`` and native-CAS
        # backends (mtime check). The XML predicate added by --test-xml-dir
        # composes on top: a test whose result marker is current but whose
        # XML file is missing must re-run to regenerate the XML.
        legacy_mtime = getattr(self.args, "use_mtime", False) or self._has_native_cas_exe()

        tests_to_run = []
        for _source, exe_path in test_pairs:
            result_file = self._result_marker_path(exe_path)
            if legacy_mtime:
                result_current = (
                    os.path.exists(result_file)
                    and os.path.exists(exe_path)
                    and os.path.getmtime(result_file) >= os.path.getmtime(exe_path)
                )
            else:
                # CAS-only: marker is content-addressed, presence is sufficient.
                result_current = os.path.exists(result_file)
            if not result_current:
                tests_to_run.append(exe_path)
                continue
            if xml_dir and self._test_frameworks.get(exe_path) is not None:
                if not os.path.exists(self._xml_path_for(exe_path)):
                    tests_to_run.append(exe_path)
                    continue
            if self.args.verbose >= 2:
                print(f"Skipping up-to-date test: {exe_path}", file=sys.stderr)

        if not tests_to_run:
            if self.args.verbose >= 1:
                print("All tests up-to-date, nothing to run.", file=sys.stderr)
            return

        parallel = getattr(self.args, "parallel", 1)
        if getattr(self.args, "serialisetests", False):
            parallel = 1

        testprefix = getattr(self.args, "TESTPREFIX", "")

        if parallel > 1:
            self._run_tests_parallel(tests_to_run, testprefix, parallel)
        else:
            self._run_tests_sequential(tests_to_run, testprefix)

    def _run_single_test(self, exe_path: str, testprefix: str) -> tuple[str, int, str, str]:
        """Run a single test executable. Returns (exe_path, returncode, stdout, stderr).

        When ``--timing`` is enabled, records a per-test rule on the
        BuildTimer so the post-build report breaks ``test_execution`` down
        to individual test wall-clock times.  Recording happens inside the
        worker (which may be a thread under parallel execution); the
        timer's lock makes ``record_rule`` thread-safe.

        When ``--test-xml-dir`` is set and the test's framework was
        detected, appends the framework-specific XML-emit argv *after*
        ``exe_path`` so prefix tools like valgrind / strace -f / taskset
        forward the trailing argv to the child process correctly.
        """
        cmd = []
        if testprefix:
            cmd.extend(testprefix.split())
        cmd.append(exe_path)

        if getattr(self.args, "test_xml_dir", None):
            framework = self._test_frameworks.get(exe_path)
            if framework is not None:
                cmd.extend(framework.xml_argv(self._xml_path_for(exe_path)))

        timer = self._timer
        start = time.monotonic() if timer else 0.0
        result = subprocess.run(cmd, capture_output=True, text=True)
        if timer:
            elapsed = time.monotonic() - start
            timer.record_rule(
                rule_type="test",
                target=exe_path,
                source=exe_path,
                elapsed_s=elapsed,
                start_s=start,
                end_s=start + elapsed,
            )
        return exe_path, result.returncode, result.stdout, result.stderr

    def _run_tests_sequential(self, tests_to_run: list[str], testprefix: str) -> None:
        """Run tests one at a time, printing output immediately."""
        failures = []
        for exe_path in tests_to_run:
            if self.args.verbose >= 1:
                print(f"... {exe_path}")
            exe_path, rc, stdout, stderr = self._run_single_test(exe_path, testprefix)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            if rc != 0:
                failures.append(exe_path)
            else:
                # Touch the .result file to mark success
                _touch(self._result_marker_path(exe_path))

        if failures:
            raise RuntimeError(f"Test failures: {', '.join(failures)}")

    def _run_tests_parallel(self, tests_to_run: list[str], testprefix: str, parallel: int) -> None:
        """Run tests in parallel, buffering output and printing in order."""
        import concurrent.futures

        failures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(self._run_single_test, exe_path, testprefix): exe_path for exe_path in tests_to_run
            }
            # Collect results as they complete; reorder below to match submission order
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

        # Sort by original order and print
        order = {path: i for i, path in enumerate(tests_to_run)}
        results.sort(key=lambda r: order[r[0]])
        for exe_path, rc, stdout, stderr in results:
            if self.args.verbose >= 1:
                print(f"... {exe_path}")
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            if rc != 0:
                failures.append(exe_path)
            else:
                _touch(self._result_marker_path(exe_path))

        if failures:
            raise RuntimeError(f"Test failures: {', '.join(failures)}")

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

    def _wrap_compile_cmd(self, command: list[str]) -> str:
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
        """
        try:
            o_idx = command.index("-o")
        except ValueError as e:
            raise AssertionError(f"compile rule missing -o flag: {command}") from e

        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return shlex.join(command)

        compile_part = command[:o_idx] + command[o_idx + 2 :]
        target = command[o_idx + 1]

        return wrap_compile_with_lock(shlex.join(compile_part), target, self.args, self._filesystem_type)

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
        return has_build_rules

    def _record_link_signatures(self, graph: BuildGraph) -> None:
        """Persist a content-addressable signature for every link/library
        rule whose output exists on disk.

        Backends with a native CAS layer (cmake/bazel — see
        ``_has_native_cas_exe``) write their actual binaries to a tool-
        managed location (``cmake-build/``, ``bazel-bin/``, FUSE-tracked
        path) rather than the graph-declared ``rule.output``. For those,
        a missing ``rule.output`` is expected and benign — silently
        skip.

        For backends without a native CAS layer (the make/ninja/shake/
        slurm common case), a missing ``rule.output`` after the build
        completed is a SYMPTOM, not normal: either the link command
        silently failed without a non-zero exit, or some downstream
        publish stage moved the file. Either way the next build's
        ``_all_outputs_current`` check then fails (no linksig present
        → False) and the link recipe re-fires — diagnostic, not silent.
        Log at ``verbose>=1`` so operators can spot it instead of
        chasing a "build keeps relinking" mystery (I5).
        """
        native_cas = self._has_native_cas_exe()
        for rule in graph.rules:
            if rule.rule_type in (RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
                if not os.path.exists(rule.output):
                    if not native_cas and getattr(self.args, "verbose", 0) >= 1:
                        # I5: surface the unexpected case where a non-
                        # native-CAS backend's link rule has no on-disk
                        # output at sig-recording time.
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
            return extras
        if kind == "clang":
            extras: list[str] = []
            # `-fprebuilt-module-path` is only useful when all .pcm files
            # live in the same flat directory, which is the non-cache
            # case. With cas-pcmdir each .pcm is under its own
            # `<command_hash>/` subdir, so the flat scan would find
            # nothing -- the per-module `-fmodule-file=` mappings emitted
            # by `_clang_partition_module_file_flags` carry the lookup
            # in that mode.
            if (
                (result.module_imports or result.module_implements)
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
            # by argv-executing backends (Shake, Slurm), which would reject
            # a pre-shell-quoted token as an unknown flag.
            # Per-TU narrowing (rather than the blanket-all approach
            # used for partitions) is cheap because each TU's
            # header-unit list is short.
            if result.module_header_imports:
                extras.append("-fmodules")
                for token in result.module_header_imports:
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
            needs_libcxx = "std" in result.module_imports or bool(result.module_header_imports)
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

    def _wire_module_inputs(self, rule: BuildRule, file_result) -> None:
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
        target_map: dict[str, str] = self._module_iface_pcm if kind == "clang" else self._module_iface_obj

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

        resolved: list[str] = []
        for raw in tuple(file_result.module_imports) + tuple(file_result.module_implements):
            r = self.hunter._resolve_module_import(raw, own_module)
            if r is None:
                continue
            resolved.append(r)
            # Importer of a primary M depends transitively on M's
            # partitions; over-includes M's own partition exports too
            # (which are already in `resolved` if listed) -- the dedup
            # below handles that.
            if ":" not in r:
                for part_name in target_map:
                    if part_name.startswith(r + ":"):
                        resolved.append(part_name)

        for name in resolved:
            _add_dep(target_map.get(name))

        # Header-unit imports: regardless of compiler, importers must
        # wait for the header-unit precompile to finish (gcc cache: the
        # .gcm itself; gcc no-cache: a .stamp file touched after the
        # precompile; clang: the .pcm itself).
        if self._header_unit_artefact:
            for token in file_result.module_header_imports:
                _add_dep(self._header_unit_artefact.get(token))

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
        return _pcm_command_hash(
            self.args,
            source_path=token,
            transitive_content_hash="",  # implicit in compiler_identity
            cxxflags_tokens=cxxflags_tokens,
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
        precompiles.
        """
        kind = self._module_compiler_kind
        bare = _header_unit_arg(token)

        common_cmd = compiletools.utils.split_command_cached(self.args.CXX) + list(self.args.flags.cxx)

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
        cmd_hash = _pcm_command_hash(
            self.args,
            source_path=token,
            transitive_content_hash="",  # implicit in compiler_identity
            cxxflags_tokens=self.args.flags.hash_relevant("cxx"),
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            extra_flags=[],
            stage="gcc_header_unit",
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

        precompile_cmd = (
            common_cmd
            + partition_flags
            + [
                "-x",
                "c++-module",
                "--precompile",
                filename,
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

        # Compute include_weight for SLURM memory estimation.
        # len(quoted_headers) from FileAnalyzer correlates with peak RSS (r=0.85)
        # because each quoted include transitively pulls in framework templates.
        # analyze_file is already cached from the header dep walk -- zero cost.
        from compiletools.file_analyzer import analyze_file
        from compiletools.global_hash_registry import get_file_hash

        try:
            content_hash = get_file_hash(filename, self.context)
            analysis = analyze_file(content_hash, self.context)
            include_weight = len(analysis.quoted_headers)
        except (FileNotFoundError, OSError, RuntimeError) as e:
            print(
                f"WARNING: could not analyze {filename!r} for include_weight ({type(e).__name__}: {e}); "
                "SLURM memory estimate will be 0 for this rule.",
                file=sys.stderr,
            )
            include_weight = 0

        import stringzilla as sz

        magicflags = self.hunter.magicflags(filename)

        # Add PCH .gch dependency if this source uses a precompiled header.
        # Collect -I flags for the PCH CAS so GCC finds the cached .gch.
        pch_include_flags: list[str] = []
        for pch_header in magicflags.get(sz.Str("PCH"), []):
            pch_header_str = str(pch_header)
            gch_path = self._pch_gch_paths.get(pch_header_str, _gch_path(pch_header_str))
            if gch_path not in prerequisites:
                prerequisites.append(gch_path)
            include_dir = self._pch_include_dirs.get(pch_header_str)
            if include_dir:
                pch_include_flags.extend(["-I", include_dir])

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
            include_weight=include_weight,
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

    def _has_native_cas_exe(self) -> bool:
        """Whether this backend already has its own content-addressable
        cache for linker artefacts (executables, static libraries,
        shared libraries) and so should NOT be wrapped in compiletools'
        cas-exedir layer.

        False (default) — backend has no native CAS for linker
        artefacts; compiletools' cas-exedir layer applies. The producer
        rule (link / static_library / shared_library) writes to a
        content-addressable ``<cas-exedir>/<shard>/<name>_<key>.<ext>``
        with ``<ext>`` ∈ ``{.exe, .a, .so}``, paired with a downstream
        ``symlink`` rule that publishes the user-facing ``bin/<variant>/<name>``
        as a hard link (with symlink fallback) to the cached artefact.
        This is the case for Make/Ninja/Shake/Slurm.

        True — backend already maintains a CAS-equivalent layer
        (cmake's out-of-source tree, bazel's action cache). All three
        rule types write directly to their user-facing paths (legacy
        single-rule shape) so the graph IR doesn't impose a competing
        cache layout on top of the backend's own.
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
        * **Shake / Slurm** use verifying traces (content hashes).

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
            order_only_deps=[self.namer.executable_dir()],
        )

    def _create_link_rule(self, source: str, library_outputs: list[str] | None = None) -> list[BuildRule]:
        """Build the link rule(s) for an executable target.

        When ``_has_native_cas_exe()`` returns False (the default for
        Make/Ninja/Shake/Slurm), returns a two-element list:
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

        When ``_has_native_cas_exe()`` returns True (cmake/bazel),
        returns a single-element list with the legacy ``bin/<name>``
        link rule.

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
            exe_dir = self.namer.executable_dir()
            extra_link_argv.append(f"-L{exe_dir}")
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

        if self._has_native_cas_exe():
            # Backend already has its own CAS layer — emit the legacy
            # single-rule shape that writes directly to bin/<name>.
            link_cmd = (
                ld_argv
                + ["-o", exename]
                + list(object_names)
                + merged_ldflags_for_cmd
                + extra_link_argv_for_cmd
                + ld_extra_for_cmd
            )
            return [
                BuildRule(
                    output=exename,
                    inputs=link_inputs_for_graph,
                    command=link_cmd,
                    rule_type="link",
                    order_only_deps=[self.namer.executable_dir()],
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
            "extra_link_argv": extra_link_argv,
            "library_outputs": sorted(
                compiletools.apptools.canonicalize_paths_for_cache_key(library_outputs or [], anchor_root)
            ),
            "ld_extra": ld_extra,
            # canonical_bindir: full anchor-relative bindir, not just the
            # basename. The original ``bindir_basename`` defence (C5) was
            # wrong-headed in two ways: (1) ``$ORIGIN``-relative RPATH is
            # resolved at runtime by ld.so against wherever the binary
            # lives, so identical RPATH text behaves identically across
            # bindirs of the same shape — basename gives no extra
            # discrimination; (2) two SIBLING bindirs ``bin/blank`` and
            # ``out/blank`` shared basename and silently collided. The
            # canonicalised full bindir disambiguates without breaking
            # workspace-portability (gitroot-A/bin/blank and
            # gitroot-B/bin/blank canonicalise to the same string).
            "canonical_bindir": compiletools.apptools.canonicalize_path_for_cache_key(
                self.namer.executable_dir(), anchor_root
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

        When ``_has_native_cas_exe()`` returns False (the default for
        Make/Ninja/Shake/Slurm), returns a two-element list:
          [0] The ``ar`` rule whose output is the content-addressable
              ``<cas-exedir>/<shard>/lib<name>_<libkey>.a``. ``libkey``
              hashes the canonicalized object set + ar argv, so two
              ``ar`` invocations with identical content-relevant
              inputs share the cache entry across workspaces.
          [1] A ``symlink`` rule publishing the user-facing
              ``bin/<variant>/lib<name>.a`` as a hard link (with
              symlink fallback) to the cas-static-library entry.

        When ``_has_native_cas_exe()`` returns True, returns a
        single-element list with the legacy direct-output shape.

        Same lift-to-order-only treatment in make/ninja backends as
        the link rule when ``--use-mtime=False`` (default).
        """
        sourcefilename = compiletools.wrappedos.realpath(self.args.static[0])
        object_names, _ = self._get_library_object_names(self.args.static)
        lib_path = self.namer.staticlibrary_pathname(sourcefilename)

        if self._has_native_cas_exe():
            lib_cmd = ["ar", "-src", lib_path] + list(object_names)
            return [
                BuildRule(
                    output=lib_path,
                    inputs=list(object_names),
                    command=lib_cmd,
                    rule_type="static_library",
                    order_only_deps=[self.namer.executable_dir()],
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
        the ``_has_native_cas_exe`` decision and the publish-symlink
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

        if self._has_native_cas_exe():
            lib_cmd = (
                ld_argv + ["-shared", "-o", lib_path] + list(object_names) + merged_ldflags_for_cmd + ld_extra_for_cmd
            )
            return [
                BuildRule(
                    output=lib_path,
                    inputs=list(object_names),
                    command=lib_cmd,
                    rule_type="shared_library",
                    order_only_deps=[self.namer.executable_dir()],
                )
            ]

        anchor_root = self._anchor_root
        lib_key_payload = {
            "linker_identity": _compiler_identity(ld_argv[0], anchor_root=anchor_root) if ld_argv else "",
            "ld_argv": compiletools.apptools.canonicalize_paths_for_cache_key(ld_argv, anchor_root),
            "shared": True,
            "objects": sorted(compiletools.apptools.canonicalize_paths_for_cache_key(object_names, anchor_root)),
            "merged_ldflags": compiletools.apptools.canonicalize_for_cache_key(list(merged_ldflags), anchor_root),
            "ld_extra": ld_extra,
            "canonical_bindir": compiletools.apptools.canonicalize_path_for_cache_key(
                self.namer.executable_dir(), anchor_root
            ),
            "link_environment": _link_environment_snapshot(),
        }
        lib_key_hash = self._compute_artefact_key_hash(lib_key_payload)
        cas_lib_path = self.namer.cas_dynamiclibrary_pathname(sourcefilename, lib_key_hash)
        cas_lib_bucket = os.path.dirname(cas_lib_path)

        lib_cmd = (
            ld_argv + ["-shared", "-o", cas_lib_path] + list(object_names) + merged_ldflags_for_cmd + ld_extra_for_cmd
        )
        lib_rule = BuildRule(
            output=cas_lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="shared_library",
            order_only_deps=[cas_lib_bucket],
        )
        return [lib_rule, self._build_publish_rule(cas_lib_path, lib_path, source_realpath=sourcefilename)]


@functools.lru_cache(maxsize=512)
def _resolve_system_header_abs_paths(cxx: str, token: str, std_flag: str = "-std=c++20") -> list[str]:
    """Resolve a header-unit token to every path the compiler may key it by.

    Used by the gcc cas-pcmdir mapper. gcc keys header-unit lookups by
    the *string form* of the resolved include path -- and that string
    depends on the compiler's flag context. Two cases that produce
    different strings for the same physical header:

    * Default: ``-fcanonical-system-headers`` is on, so gcc reports the
      include path with ``..`` segments collapsed and symlinks resolved.
    * ``-fno-canonical-system-headers``: gcc reports whatever raw search
      path produced the hit -- typically containing ``..`` segments
      (e.g. ``.../gcc/16/bin/../lib/gcc/.../include/vector``) and
      preserving symlinks.

    Bazel's gcc autoconfig appends ``-fno-canonical-system-headers``
    AFTER user ``copts`` / ``--cxxopt``, so the importer compile sees
    the non-canonical form even though our explicit
    ``--cxxopt=-fcanonical-system-headers`` is present. If the mapper
    only carries the canonical key, the importer's lookup misses with
    "unknown compiled module interface". The fix is to emit BOTH
    spellings as mapper keys (both pointing to the same ``.gcm``) so
    the lookup hits regardless of how the consumer's flag set ended up
    canonicalizing.

    Returns a list with the canonical path first (for stability) and
    any additional non-canonical spelling. Duplicates collapsed.
    Empty list when the compiler probe fails -- callers must handle
    this (for the mapper case, omit those entries and gcc will fall
    back to its default ``gcm.cache`` placement, still correct just
    not cached).
    """
    bare = _header_unit_arg(token)
    delim_open, delim_close = ("<", ">") if (token.startswith("<") and token.endswith(">")) else ('"', '"')
    snippet = f"#include {delim_open}{bare}{delim_close}\n"

    def _probe(extra_flags: list[str]) -> str | None:
        try:
            r = subprocess.run(
                [cxx, std_flag, *extra_flags, "-M", "-x", "c++", "-"],
                input=snippet,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        # gcc emits a make-style dep listing: `<obj>: <dep1> <dep2> \\\n  <dep3> ...`
        # Flatten line continuations, split on whitespace, then find a dep
        # whose tail matches the bare header path.
        deps = r.stdout.replace("\\\n", " ").split()
        if deps and deps[0].endswith(":"):
            deps = deps[1:]
        for dep in deps:
            if dep == "-" or dep.endswith("/stdc-predef.h"):
                continue
            if dep.endswith("/" + bare) or os.path.basename(dep) == bare:
                return dep
        return None

    paths: list[str] = []
    seen: set[str] = set()
    # Probe canonical first -- preserves the existing cache key shape and
    # gives the bazel-free build path the same string as before.
    for extra in ([], ["-fno-canonical-system-headers"]):
        p = _probe(extra)
        if p and p not in seen:
            paths.append(p)
            seen.add(p)
    return paths


def _resolve_system_header_abs_path(cxx: str, token: str, std_flag: str = "-std=c++20") -> str | None:
    """Backward-compatible single-path wrapper around
    ``_resolve_system_header_abs_paths``. Returns the canonical
    spelling when both probes succeed, the only spelling when only one
    does, ``None`` when both fail.
    """
    paths = _resolve_system_header_abs_paths(cxx, token, std_flag=std_flag)
    return paths[0] if paths else None


def _cas_pcm_path(filename_stem: str, pcmdir: str, command_hash: str) -> str:
    """Return the cache path for a clang ``.pcm`` or gcc ``.gcm``.

    Layout: ``<pcmdir>/<command_hash>/<filename_stem>``.

    Mirrors ``cas-pchdir``'s shape: one ``command_hash`` directory per
    unique compile configuration (compiler + flags + source content +
    transitive headers), with the artefact and a sidecar
    ``manifest.json`` inside.

    .. note:: An earlier revision of this file split the ``command_hash``
       into three independent path components (file_hash + dep_hash +
       cmd_hash), mirroring the object cache's
       ``<basename>_<file_hash_12>_<dep_hash_14>_<macro_state_hash_16>.o``
       filename. That refactor was reverted: the object cache predates
       sidecar manifests and uses path-axis separation because
       ``trim_objdir`` reads ``file_hash[:12]`` directly out of the
       filename to do its "is this source still tracked?" check.
       PCM (a) ships with a manifest from day one that already carries
       ``bucket_key`` and ``transitive_hashes``, (b) holds two orders
       of magnitude fewer entries than the object cache so sharding is
       overkill, and (c) has header-unit entries that can't fit the
       (source, deps, env) triple cleanly -- their "source" is a
       compiler-shipped header with no git identity. Single hash +
       manifest is the right shape for PCM.
    """
    return os.path.join(pcmdir, command_hash, filename_stem)


def _pcm_command_hash(
    args,
    source_path: str,
    transitive_content_hash: str,
    cxxflags_tokens: list[str],
    magic_cpp_flags: list,
    magic_cxx_flags: list,
    extra_flags: list[str],
    stage: str,
    anchor_root: str | None = None,
) -> str:
    """Single content-addressable hash for a PCM cache entry.

    Folds every input that affects the BMI bytes into one 16-hex-char
    sha256 truncation: compiler identity, hash-relevant flags, magic
    flags, source identity (path), transitive header content, and a
    stage marker. Identical inputs -> identical hash -> shared cache
    entry. Any drift -> different hash -> different cache path.

    .. note:: 16 hex chars (64 bits) is the right entropy budget for
       PCM. The object cache uses 168 bits across three path
       components because a hash collision on ``.o`` files would cause
       a **silent miscompile** -- the linker doesn't verify object
       contents against the inputs that produced them. PCM and PCH
       have **in-band BMI verification at consume time**: GCC's PCH
       stamp / clang's BMI signature record the compile environment
       and reject on mismatch. A hypothetical 64-bit collision
       therefore degrades to a slow re-precompile, never a
       miscompile. Single-hash + manifest is the right shape; an
       earlier 3-axis refactor mimicking the object cache was
       reverted because it added complexity without addressing a
       safety problem PCM doesn't have.

    ``stage`` (e.g. ``"clang_module_interface"``,
    ``"clang_header_unit"``, ``"gcc_module_interface"``,
    ``"gcc_header_unit"``) prevents a same-named module and header
    unit from colliding under the same flag set.

    ``transitive_content_hash`` is the caller's responsibility to
    compose -- typically ``f"{source_hash}:{dep_hash}"`` for named
    modules and the empty string (or a token-derived value) for header
    units whose transitive deps are implicit in ``compiler_identity``.

    ``cxxflags_tokens`` is the hash-relevant structured form of
    ``args.CXXFLAGS`` -- the caller is responsible for pre-filtering
    via ``args.flags.hash_relevant("cxx")`` (which strips ``-D``/``-U``
    AND drops diagnostic-only flags). This function does NOT re-filter
    that parameter; only the per-file ``magic_cpp_flags`` /
    ``magic_cxx_flags`` (which arrive un-filtered from the magic-flag
    pipeline) are filtered here. Symmetric with ``_pch_command_hash``.
    """
    # Canonicalize path-bearing flag tokens and the source path against
    # the gitroot anchor so the cache key is decoupled from the absolute
    # workspace path -- two CI runs landing under different attempt
    # directories share the same PCM cache entries. anchor_root=None
    # falls back to the cached find_git_root() lookup; an explicit empty
    # string disables canonicalization (graceful no-op).
    if anchor_root is None:
        anchor_root = compiletools.git_utils.find_git_root()
    canonical = {
        "stage": stage,
        "compiler_identity": _compiler_identity(args.CXX, anchor_root=anchor_root),
        "cxx_command": compiletools.apptools.canonicalize_path_for_cache_key(args.CXX, anchor_root),
        "CXXFLAGS_TOKENS": compiletools.apptools.canonicalize_for_cache_key(list(cxxflags_tokens), anchor_root),
        "magic_cpp_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cpp_flags]),
            anchor_root,
        ),
        "magic_cxx_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cxx_flags]),
            anchor_root,
        ),
        "extra_flags": compiletools.apptools.canonicalize_for_cache_key(list(extra_flags), anchor_root),
        "source": compiletools.apptools.canonicalize_path_for_cache_key(source_path, anchor_root),
        "transitive_content_hash": transitive_content_hash,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def _gch_path(header: str, pchdir: str | None = None, command_hash: str | None = None) -> str:
    """Return the precompiled header output path for a header file.

    When *pchdir* and *command_hash* are provided the .gch is placed under
    ``<pchdir>/<command_hash>/<basename>.gch`` so that GCC can find it via
    ``-I <pchdir>/<command_hash>/``.  Otherwise falls back to the legacy
    ``header.gch`` path next to the header.
    """
    if pchdir and command_hash:
        return os.path.join(pchdir, command_hash, os.path.basename(header) + ".gch")
    return header + ".gch"


_PCHDIR_WARNED: set[str] = set()


def _warn_if_pchdir_not_cross_user_safe(pchdir: str, verbose: int) -> None:
    """Emit a one-time warning if pchdir's parent isn't group-writable + SGID.

    The PCH CAS is intended to be readable across users:
    user A creates ``<pchdir>/<cmd_hash>/stdafx.h.gch``, user B should be
    able to consume it. With a default ``umask 0077`` and no SGID on the
    parent, A's directory is mode ``0700`` and B silently re-builds the
    PCH every time. Warn early so the operator can fix the parent dir
    permissions (typically ``chmod 2775`` + ``chgrp <build-group>``).

    The warning is one-time per (pchdir) per process to avoid spam in
    multi-target builds.
    """
    if pchdir in _PCHDIR_WARNED:
        return
    _PCHDIR_WARNED.add(pchdir)

    # Skip the warning when pchdir is a per-user path (cwd-relative
    # or under the build's bin tree). The cross-user-safety guidance only
    # applies to genuinely shared cache locations.
    abs_pchdir = os.path.abspath(pchdir)
    cwd = os.path.abspath(os.getcwd())
    if abs_pchdir == cwd or abs_pchdir.startswith(cwd + os.sep):
        return

    parent = os.path.dirname(os.path.abspath(pchdir)) or "."
    target = pchdir if os.path.isdir(pchdir) else parent
    try:
        st = os.stat(target)
    except OSError:
        return  # No parent yet, mkdir will create it; nothing useful to warn about

    mode = st.st_mode
    issues = []
    # Group-write needed so user B can create new <cmd_hash>/ subdirs.
    if not (mode & 0o020):
        issues.append("not group-writable (need at least mode 2775)")
    # SGID needed so children inherit the parent's group, not the creator's
    # primary group.
    if not (mode & 0o2000):
        issues.append("missing SGID bit (chmod g+s)")

    if issues and verbose >= 1:
        joined = "; ".join(issues)
        print(
            f"WARNING: PCH CAS {target!r} is {joined}. "
            "Cross-user PCH cache hits will silently miss. Fix with: "
            f"chmod 2775 {target!r} && chgrp <build-group> {target!r}",
            file=sys.stderr,
        )


# Backward-compat alias: ``compiler_identity`` was promoted to
# ``compiletools.apptools`` so it can be shared with ``preprocessing_cache``
# (per-TU object cache key). Keep the original name available so the PCH
# call sites below remain unchanged.
_compiler_identity = compiletools.apptools.compiler_identity


def _pch_command_hash(
    args,
    pch_header: str,
    magic_cpp_flags: list,
    magic_cxx_flags: list,
    cxxflags_tokens: list[str],
    scope_macro_hash: str,
    anchor_root: str | None = None,
) -> str:
    """Compute a content-addressable hash for a PCH compile command.

    The hash captures compiler identity (binary realpath + size + mtime,
    not just the user-supplied command name), all flags, and the realpath
    of the header so that different compilers / flags / headers produce
    distinct cache entries while identical configurations share a single
    .gch file. Uses ``json.dumps`` rather than space-join so flag values
    containing literal spaces (``-DFOO="a b"``) cannot collide with
    space-separated flag pairs.

    .. note:: PCH (and PCM, the C++20 modules cache it inspired)
       intentionally use a **single** command_hash directory plus
       sidecar manifest, not the object cache's three path-axis
       hashes. The 3-axis structure on the object cache exists
       because ``.o`` files have no in-band verification at link
       time -- a hash collision would cause a silent miscompile, so
       the path needs the entropy of multiple independent hashes to
       make collisions statistically impossible. PCH and PCM have
       BMI / PCH-stamp verification at consume time: a hypothetical
       64-bit collision degrades to a slow re-precompile, never a
       miscompile, so the lower-entropy single-hash key is safe and
       simpler. (An earlier exploration of refactoring PCM to the
       3-axis layout was reverted for this exact reason.)

    ``cxxflags_tokens`` is the hash-relevant structured form of
    ``args.CXXFLAGS`` -- the caller is responsible for pre-filtering
    via ``args.flags.hash_relevant("cxx")`` (which strips ``-D``/``-U``
    AND drops diagnostic-only flags). This function does NOT re-filter
    that parameter; only the per-file ``magic_cpp_flags`` /
    ``magic_cxx_flags`` (which arrive un-filtered from the magic-flag
    pipeline) are filtered here. The cmdline ``-D`` macros relevant to
    this PCH header are folded in via ``scope_macro_hash`` (see
    :func:`_pch_scope_macro_hash`), so two apps that differ only in an
    irrelevant ``-DAPP_NAME=...`` value share the same PCH cache key.
    """
    # 64 bits (16 hex chars) of SHA-256 — birthday-collision risk at
    # ~4 billion entries, fine in practice. PCH cache validity is also
    # guarded by GCC's PCH stamp at consume time, so a hash collision
    # would only cause a slow rebuild, not a miscompile.
    # The cache key hashes only the immediate header's realpath, but
    # transitive-header content hashes are recorded in the sidecar
    # manifest written by ``_write_pch_manifest``. ``trim_cache.trim_pchdir``
    # reads those hashes and pre-evicts entries whose transitive headers
    # have changed, so the slow ``cc1`` PCH-stamp rebuild is avoided in
    # the cross-user-mixed-content case.
    # Diagnostic-only flags (warnings, message formatting, -pipe, -v...)
    # never affect the compiled .gch bytes. Filter them out of every
    # flag-token list so flipping -Wall <-> -Wextra (or annotating a
    # header with //#CXXFLAGS=-Wall) doesn't pollute the PCH cache key.
    #
    # Path-bearing flag tokens (-I/-isystem/etc.) and the header path
    # itself are then canonicalized against the gitroot anchor so the
    # cache key is decoupled from the absolute workspace path -- two
    # CI runs landing under different attempt directories share the
    # same PCH cache entries. anchor_root=None falls back to the
    # cached find_git_root() lookup; an explicit empty string disables
    # canonicalization (graceful no-op).
    if anchor_root is None:
        anchor_root = compiletools.git_utils.find_git_root()
    canonical = {
        # In-workspace wrapper scripts (coverage / sccache / distcc) leak
        # the per-checkout absolute path through both fields otherwise.
        "compiler_identity": _compiler_identity(args.CXX, anchor_root=anchor_root),
        "cxx_command": compiletools.apptools.canonicalize_path_for_cache_key(args.CXX, anchor_root),
        # Structured tokens with -D/-U stripped AND diagnostic-only flags
        # removed; pre-filtered by caller via args.flags.hash_relevant("cxx").
        # Cmdline -D macros are captured by ``scope_macro_hash`` after
        # per-PCH-header scoping.
        "CXXFLAGS_TOKENS": compiletools.apptools.canonicalize_for_cache_key(list(cxxflags_tokens), anchor_root),
        "magic_cpp_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cpp_flags]),
            anchor_root,
        ),
        "magic_cxx_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cxx_flags]),
            anchor_root,
        ),
        "header": compiletools.apptools.canonicalize_path_for_cache_key(
            compiletools.wrappedos.realpath(pch_header), anchor_root
        ),
        "stage": "c++-header",
        "scope_macro_hash": scope_macro_hash,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def _pch_scope_macro_hash(hunter, pch_header: str) -> str:
    """Hash the cmdline ``-D`` macros relevant to a single PCH header.

    Mirrors the per-TU scope-filter logic in
    :meth:`compiletools.hunter.Hunter.macro_state_hash`, but for PCH
    cache keys. Only cmdline-D macros that the PCH header (or any of
    its transitive headers) references as identifiers are folded in.
    Compiler builtins are not included -- they're already captured by
    ``compiler_identity`` in :func:`_pch_command_hash`.

    Returns 16 hex chars of sha256 over a sorted, deterministic
    (name, value) pair list. Returns ``"0" * 16`` when:

    * ``cmdline_origin`` is empty (no ``--append-*FLAGS=-D...`` at all), or
    * No cmdline-D macro is referenced by this PCH header.

    The all-zeros sentinel is intentional -- it makes "no scoping
    applied" visible in the canonical dict rather than masking it as a
    sha256 of an empty list.
    """
    cmdline_origin = hunter.magicparser._initial_macro_state.cmdline_origin
    if not cmdline_origin:
        return "0" * 16

    pch_content_hash = compiletools.global_hash_registry.get_file_hash(pch_header, hunter.context)
    transitive = hunter._transitive_content_hashes(pch_header)
    # Hunter has no Namer attached, so derive a stable dep_hash from
    # the sorted transitive content hashes directly. The exact value
    # doesn't matter -- it only needs to be content-addressed and
    # stable so CmdlineMacroIndex's per-TU scope cache stays coherent.
    dep_hash = hashlib.sha256("\n".join(sorted(transitive)).encode()).hexdigest()[:14]

    scope_filter = hunter._get_cmdline_macro_index().tu_referenced_macros(
        tu_filename=pch_header,
        tu_content_hash=pch_content_hash,
        dep_hash=dep_hash,
        transitive_content_hashes=transitive,
    )

    _write_pch_scope_diagnostic(hunter.args, pch_header, cmdline_origin, scope_filter)

    if not scope_filter:
        return "0" * 16

    core = hunter.magicparser._initial_macro_state.core
    pairs = sorted((str(name), str(core[name])) for name in scope_filter if name in core)
    if not pairs:
        return "0" * 16
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def _write_pch_scope_diagnostic(
    args,
    pch_header: str,
    cmdline_origin: frozenset,
    scope_filter: frozenset,
) -> None:
    """Write per-PCH scope diagnostics JSON when --scope-diagnostics is on.

    File path: ``<diagnostics_dir>/scope/pch/<basename>.json``

    Why no dep_hash in the filename: the PCH cache itself is keyed by
    cmd_hash; one PCH header in one invocation has one canonical scope
    decision. (Multiple variant builds in one invocation would share
    a process and one diagnostics dir, but get distinct cmd_hashes via
    the regular PCH cache.) If we ever observe collisions in practice
    we can extend with a discriminator.

    Mirrors :meth:`compiletools.hunter.Hunter._write_scope_diagnostic`,
    but for PCH cache keys. Silently no-ops when no diagnostics dir is
    resolvable -- callers without ``--diagnostics-dir`` or ``--bindir``
    set must not crash.
    """
    if not getattr(args, "scope_diagnostics", False):
        return

    try:
        diagnostics_dir = compiletools.diagnostics.resolve_diagnostics_dir(args)
    except RuntimeError:
        return  # No diagnostics dir resolvable -- silently skip

    scope_dir = os.path.join(diagnostics_dir, "scope", "pch")
    os.makedirs(scope_dir, exist_ok=True)

    excluded = sorted(str(n) for n in cmdline_origin if n not in scope_filter)
    included = sorted(str(n) for n in scope_filter if n in cmdline_origin)

    payload = {
        "pch_header": pch_header,
        "cmdline_d_macros_total": len(cmdline_origin),
        "cmdline_d_macros_in_hash": included,
        "cmdline_d_macros_excluded": excluded,
    }

    basename = os.path.basename(pch_header)
    out_path = os.path.join(scope_dir, f"{basename}.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _write_pcm_manifest(
    pcmdir: str,
    cmd_hash: str,
    bucket_key: str,
    transitive_headers: list[str],
    cxx_command: str,
    stage: str,
    context,
    *,
    anchor_root: str,
) -> None:
    """Write a sidecar manifest next to a cached ``.pcm`` / ``.gcm`` file.

    Layout matches ``_write_pch_manifest``: the manifest lands at
    ``<pcmdir>/<cmd_hash>/manifest.json``, alongside the artefact.

    Enables ``trim_cache.trim_pcmdir`` to (a) bucket cmd_hash dirs by
    ``bucket_key`` so cross-variant builds of the same source/header
    don't evict each other at ``keep_count=1`` (source realpath for
    named modules, verbatim token like ``<vector>`` for header units),
    and (b) pre-evict entries whose transitive header content has
    shifted since the artefact was built.

    ``stage`` is the same string handed to ``_pcm_command_hash`` so the
    trim CLI can reason about which compiler-stage produced the entry.

    Atomic via ``os.replace``.
    """
    manifest_dir = os.path.join(pcmdir, cmd_hash)
    os.makedirs(manifest_dir, exist_ok=True)

    transitive_hashes: dict[str, str] = {}
    for h in transitive_headers:
        h_real = compiletools.wrappedos.realpath(h)
        try:
            transitive_hashes[h_real] = compiletools.global_hash_registry.get_file_hash(h_real, context=context)
        except (OSError, KeyError):
            pass

    manifest = {
        "bucket_key": bucket_key,
        "stage": stage,
        "compiler": cxx_command,
        "compiler_identity": _compiler_identity(cxx_command, anchor_root=anchor_root),
        "transitive_hashes": transitive_hashes,
    }

    manifest_path = os.path.join(manifest_dir, "manifest.json")
    tmp_path = f"{manifest_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, sort_keys=True)
    os.replace(tmp_path, manifest_path)


def _write_pch_manifest(
    pchdir: str,
    cmd_hash: str,
    pch_header: str,
    transitive_headers: list[str],
    cxx_command: str,
    context,
    *,
    anchor_root: str,
) -> None:
    """Write a sidecar manifest next to a cached .gch file.

    The manifest enables ``trim_cache.trim_pchdir`` to:

    * Bucket ``<pchdir>/<cmd_hash>/`` directories by ``header_realpath``
      so ``keep_count`` is enforced per real header rather than globally —
      cross-variant builds of the same header no longer evict each other
      at the default ``keep_count=1``.
    * Pre-evict entries whose transitive header content has changed
      since the .gch was built, avoiding the slow ``cc1`` PCH-stamp
      rejection at consume time.

    Hashes are git-blob SHA1 (the algorithm used by
    ``global_hash_registry``) so that ``trim_cache``'s standalone
    re-computation produces identical values.

    Written atomically via ``os.replace`` so a concurrent reader either
    sees the prior manifest or the new one, never a partial file.
    """
    manifest_dir = os.path.join(pchdir, cmd_hash)
    os.makedirs(manifest_dir, exist_ok=True)

    transitive_hashes: dict[str, str] = {}
    for h in transitive_headers:
        h_real = compiletools.wrappedos.realpath(h)
        try:
            transitive_hashes[h_real] = compiletools.global_hash_registry.get_file_hash(h_real, context=context)
        except (OSError, KeyError):
            pass

    manifest = {
        "header_realpath": compiletools.wrappedos.realpath(pch_header),
        "compiler": cxx_command,
        "compiler_identity": _compiler_identity(cxx_command, anchor_root=anchor_root),
        "transitive_hashes": transitive_hashes,
    }

    manifest_path = os.path.join(manifest_dir, "manifest.json")
    tmp_path = f"{manifest_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, sort_keys=True)
    os.replace(tmp_path, manifest_path)


@functools.lru_cache(maxsize=1)
def _native_flock_available() -> bool:
    """Check if native flock binary (util-linux) is available."""
    return shutil.which("flock") is not None


def _build_lock_env_prefix(strategy: str, args, filesystem_type: str) -> str:
    """Build the CT_LOCK_* environment variable prefix for ct-lock-helper.

    Args:
        strategy: Lock strategy (lockdir, fcntl, cifs, flock)
        args: Namespace with sleep_interval_lockdir, sleep_interval_cifs,
              sleep_interval_flock_fallback, lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Space-terminated env var prefix string, or empty string if no vars needed.
    """
    import compiletools.filesystem_utils

    env_vars = []

    if strategy == "lockdir":
        if args.sleep_interval_lockdir is not None:
            sleep_interval = args.sleep_interval_lockdir
        else:
            sleep_interval = compiletools.filesystem_utils.get_lockdir_sleep_interval(filesystem_type)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL={sleep_interval}")
    elif strategy == "fcntl":
        pass  # fcntl.lockf() blocks in kernel, no sleep interval needed
    elif strategy == "cifs":
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_CIFS={args.sleep_interval_cifs}")
    else:  # flock (fallback when native flock unavailable)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_FLOCK={args.sleep_interval_flock_fallback}")

    env_vars.append(f"CT_LOCK_WARN_INTERVAL={args.lock_warn_interval}")
    env_vars.append(f"CT_LOCK_TIMEOUT={args.lock_cross_host_timeout}")

    return " ".join(env_vars) + " " if env_vars else ""


def wrap_compile_with_lock(compile_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a compile command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per compilation.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Shared by Make and Ninja backends. When args.file_locking is False,
    returns the command with ``-o target`` appended unchanged.

    Args:
        compile_cmd: Compile command without -o flag (e.g., "gcc -c file.c")
        target: Target file (e.g., "$@" for Make, or an actual path for Ninja)
        args: Namespace with file_locking, sleep_interval_lockdir,
              sleep_interval_cifs, sleep_interval_flock_fallback,
              lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return compile_cmd + " -o " + target

    import compiletools.filesystem_utils

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup).
    # Two invariants must hold under concurrent peer makes on an object CAS:
    #   1. Lock on a SIDECAR ``<target>.lock`` file, NOT on ``<target>``. flock
    #      opens its lock argument with O_RDWR|O_CREAT, so locking the target
    #      directly would create an empty ``<target>`` with mtime=now BEFORE
    #      the inner compile runs. A peer make's mtime check then treats the
    #      target as up-to-date and skips the compile recipe entirely, going
    #      straight to link — producing ``undefined reference to 'main'``
    #      errors. Locking a sidecar leaves ``<target>`` untouched until the
    #      mv lands, so peer makes see ``<target>`` only when it is complete.
    #   2. Compile to a temp file then atomically rename — protects link rules
    #      that read .o files WITHOUT any lock. Without temp+rename a peer
    #      linker could mmap-read a half-written .o.
    # DO NOT 'optimize' back to ``flock <target> gcc -o <target>``: that form
    # violates BOTH invariants. See locking.atomic_compile() for the rationale
    # the helper-mode path below relies on.
    if strategy == "flock" and _native_flock_available():
        target_q = shlex.quote(target)
        lock_q = shlex.quote(f"{target}.lock")
        temp_q = shlex.quote(f"{target}.compiletools.tmp")
        # $$ escapes to $ at Make-recipe expansion so the shell sees $? / $ec.
        inner = f"{compile_cmd} -o {temp_q} && mv -f {temp_q} {target_q}; ec=$$?; rm -f {temp_q}; exit $$ec"
        return f"flock {lock_q} sh -c {shlex.quote(inner)}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper compile --target={target} --strategy={strategy} -- {compile_cmd}"


def wrap_link_with_lock(link_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a link/ar command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per link.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Unlike wrap_compile_with_lock, the command is passed through unchanged
    (including any -o flag) since atomic_link does not manipulate output paths.

    Args:
        link_cmd: Complete link command string (e.g., "g++ -o bin/foo obj/foo.o")
        target: Target file for locking (e.g., "$@" for Make, or an actual path)
        args: Namespace with file_locking, sleep_interval_lockdir, etc.
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return link_cmd

    import compiletools.filesystem_utils

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup).
    # Lock on ``<target>.lock`` sidecar, NOT on ``<target>``: ``flock`` opens
    # its lock argument with O_CREAT, which would create an empty ``<target>``
    # with mtime=now and trick a peer make process into treating the target
    # as up-to-date (mtime newer than its prerequisites). See
    # wrap_compile_with_lock for the full rationale.
    if strategy == "flock" and _native_flock_available():
        lock_q = shlex.quote(f"{target}.lock")
        return f"flock {lock_q} {link_cmd}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper link --target={target} --strategy={strategy} -- {link_cmd}"


def check_lock_helper_available() -> bool:
    """Check if ct-lock-helper is on PATH. Returns True if found."""
    return shutil.which("ct-lock-helper") is not None


def report_lock_helper_missing() -> None:
    """Raise RuntimeError when ct-lock-helper is not found on PATH."""
    raise RuntimeError(
        "ct-lock-helper not found in PATH\n"
        "\n"
        "The --file-locking flag requires ct-lock-helper to be installed.\n"
        "\n"
        "Solutions:\n"
        "  1. Install compiletools: pip install compiletools\n"
        "  2. Install from source: pip install -e .\n"
        "  3. Add ct-lock-helper to your PATH\n"
        "\n"
        "Or disable file locking with: --no-file-locking"
    )


_REGISTRY: dict[str, type[BuildBackend]] = {}

_BackendT = TypeVar("_BackendT", bound="BuildBackend")


def register_backend(cls: type[_BackendT]) -> type[_BackendT]:
    """Register a backend class. Can be used as a decorator.

    Adding a new backend should be a single drop-in: implement
    BuildBackend, declare ``@staticmethod tool_command()`` if the backend
    needs an external tool (return None / ``("a", "b")`` for fallbacks),
    and register. The registry is the single source of truth for
    discovery, availability, and CLI argument registration.
    """
    _REGISTRY[cls.name()] = cls
    return cls


def get_backend_class(name: str) -> type[BuildBackend]:
    """Look up a backend class by name. Raises ValueError if not found."""
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return _REGISTRY[name]


def available_backends() -> list[str]:
    """Return sorted list of registered backend names."""
    return sorted(_REGISTRY.keys())


def ensure_backends_registered() -> None:
    """Import all built-in backend modules to trigger @register_backend.

    Called lazily by code that enumerates the registry rather than from this
    module's import time, to keep startup cost low for non-build code paths
    and to avoid the build_backend ← bazel_backend ← build_backend cycle.
    """
    import compiletools.bazel_backend  # pyright: ignore[reportUnusedImport]
    import compiletools.cmake_backend  # pyright: ignore[reportUnusedImport]
    import compiletools.makefile_backend  # pyright: ignore[reportUnusedImport]
    import compiletools.ninja_backend  # pyright: ignore[reportUnusedImport]
    import compiletools.trace_backend  # noqa: F401  # pyright: ignore[reportUnusedImport]


def backend_tool_command(name: str) -> str | None:
    """Return the external tool command for a backend, or None if
    self-executing. Reads ``cls.tool_command()`` from the registered
    backend; first element of any tuple is canonical."""
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return None
    if isinstance(tool, tuple):
        return tool[0]
    return tool


def is_backend_available(name: str) -> bool:
    """Check whether the external tool for a backend is installed.

    Backends declare their tool requirement via the optional
    ``tool_command()`` classmethod, which may return:

    * ``None``        — self-executing, always available
    * ``"name"``      — single binary; available iff on PATH
    * ``("a", "b")``  — alternates; available iff at least one on PATH
    """
    import shutil

    cls = _REGISTRY.get(name)
    if cls is None:
        return False
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return True  # self-executing backends
    candidates = (tool,) if isinstance(tool, str) else tuple(tool)
    return any(shutil.which(t) for t in candidates)


def detect_available_backends(requested: list[str]) -> list[str]:
    """Filter requested backends to those whose build tool is installed."""
    available = []
    for backend in requested:
        if is_backend_available(backend):
            available.append(backend)
        else:
            tool = backend_tool_command(backend) or backend
            print(f"  Skipping backend '{backend}': '{tool}' not found on PATH")
    return available


def register_backend_cli_arguments(cap) -> None:
    """Call ``cls.add_arguments(cap)`` on every registered backend that
    declares one. Replaces the v8.0.2 pattern of cake.py
    hardcoding which backends contributed CLI args, which silently
    dropped any add_arguments() declared on ninja/cmake/bazel/shake.
    """
    for cls in _REGISTRY.values():
        adder = getattr(cls, "add_arguments", None)
        if callable(adder):
            adder(cap)
