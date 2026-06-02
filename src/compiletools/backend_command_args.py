"""Command/flag/graph free-function helpers for the build backends.

These free functions translate BuildGraph rules and raw compile/link
command vectors into the per-backend data the concrete BuildBackend
subclasses (Make, Ninja, CMake, Bazel, trace) need: extracted copts /
include paths / linkopts, per-object ``ObjInfo`` maps, CAS order-only
demotion, link signatures, and the named-module rule toposort.

This module is a deliberately thin lower layer: it imports only stdlib
plus genuinely-leaf compiletools modules (``wrappedos`` and the
``build_graph`` data types, both already below ``build_backend``) so that
``build_backend`` can re-export these names without creating an import
cycle. ``build_backend`` binds them back into its own namespace,
preserving object identity for both call sites inside ``BuildBackend``
and ``unittest.mock.patch`` targets.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections import deque
from typing import NamedTuple

import compiletools.wrappedos
from compiletools.build_graph import BuildGraph, BuildRule, RuleType


class ObjInfo(NamedTuple):
    """Per-object compile metadata extracted from a BuildGraph compile rule."""

    source: str
    headers: list[str]
    copts: list[str]


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


_DETACHED_ARG_FLAGS: frozenset[str] = frozenset({"-include"})
"""Compiler flags whose argument is a separate token (the ``-flag value``
form) and must be preserved in copts as a 2-token pair. Without this
list, the value would be dropped by the ``not arg.startswith("-")``
guard in :func:`extract_copts`, producing an orphan flag that the
receiving backend translates into a malformed compile command (e.g.
gcc consuming cmake's downstream ``-MD`` as the missing file argument
to ``-include``)."""


def extract_copts(command: list[str], *, strip_includes: bool = False) -> list[str]:
    """Extract compiler flags from a compile command.

    Strips the compiler binary, -c, source file, -o, and output file.
    When strip_includes is True, drops all -I/-isystem/-iquote flags
    (needed by Bazel which manages include paths itself).
    When False, recombines space-separated ``-I <dir>`` into ``-I<dir>``.
    Detached-argument flags listed in :data:`_DETACHED_ARG_FLAGS` (e.g.
    ``-include <header>``) are preserved as the original 2-token pair —
    gcc has no joined form for these, so we must not collapse them.
    """
    if not command:
        return []
    args = split_compound_args(command[1:])
    copts = []
    skip_next = False
    include_next = False
    keep_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if include_next:
            if not strip_includes:
                copts.append(f"-I{arg}")
            include_next = False
            continue
        if keep_next:
            copts.append(arg)
            keep_next = False
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
        if arg in _DETACHED_ARG_FLAGS:
            copts.append(arg)
            keep_next = True
            continue
        if not arg.startswith("-"):
            continue
        copts.append(arg)
    return copts


def extract_include_paths(command: list[str]) -> list[str]:
    """Extract include-path arguments from a compile command.

    Recognized forms (path is returned, flag is dropped):
      ``-Ipath``, ``-I path``, ``-I=path``, ``-isystempath``,
      ``-isystem path``, ``-isystem=path``, ``-iquotepath``,
      ``-iquote path``, ``-iquote=path``.

    The ``=path`` form is a non-standard variant some build systems emit;
    all three triad members strip a single leading ``=`` so their
    behaviour is uniform.

    **Scope is intentionally narrow.** This function exists solely to feed
    the Bazel backend's ``cc_*(includes=[...])`` attribute, which Bazel
    expands into ``-isystem`` flags propagated to dependents. The three
    families above are the ones that ``includes=[...]`` semantically maps
    to; the following are deliberately NOT recognized:

    * ``-idirafter`` / ``-iframework`` — valid gcc/clang flags but Bazel
      has no propagation channel for them through dep edges.

    For the broader path-bearing flag set used by the cache-key
    canonicalizer (which DOES include ``-idirafter`` because it's
    correctness-relevant for hashing), see
    ``apptools._PATH_BEARING_FLAGS``.

    Used by the Bazel backend to re-emit include paths via
    ``cc_binary(includes=[...])`` since Bazel manages include paths
    itself and ``extract_copts(strip_includes=True)`` drops them.
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
            paths.append(arg[2:].lstrip("="))
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
    Object-file matching is normalised via ``wrappedos.normpath`` on
    both sides so that ``./obj/foo.o`` and ``obj/foo.o`` are treated as
    the same file — without this, the divergent form would leak into
    linkopts and break Bazel/CMake link rules.
    """
    if not command:
        return []
    normalised_objects = {compiletools.wrappedos.normpath(o) for o in object_files}
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
        if compiletools.wrappedos.normpath(arg) in normalised_objects:
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
    compile rules (interface AND implementation units) so partitions and
    interface units compile before the interfaces / implementation units that
    depend on them.
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
        raise ValueError(f"_toposort_rules: cycle detected among named-module compile rules: {descriptors}")

    return result
