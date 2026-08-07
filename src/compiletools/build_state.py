"""Pure build-state computation. MUST NOT import os, sys, or subprocess
(import-linter contract). Stages are pure functions over TokenState.

posixpath is imported for its pure string functions (normpath/join) only;
stat-calling posixpath functions (exists/islink/getsize) are forbidden by
the same purity discipline even though the linter can't see them."""

from __future__ import annotations

import dataclasses
import posixpath
import re
import shlex
from dataclasses import dataclass

import compiletools.configutils
from compiletools.build_inputs import BuildInputs
from compiletools.flag_ops import dedup_include_paths_to_append, dedup_tokens, has_prefix_map_token
from compiletools.flags import Flags


@dataclass(frozen=True)
class TokenState:
    cpp: tuple[str, ...] = ()
    c: tuple[str, ...] = ()
    cxx: tuple[str, ...] = ()
    ld: tuple[str, ...] = ()


_SLOT_FIELDS = (("cpp", "cppflags"), ("c", "cflags"), ("cxx", "cxxflags"), ("ld", "ldflags"))

# Variant tokens may be separated by '.', ',', or whitespace anywhere they
# appear -- must stay identical to configutils._VARIANT_SEP_RE.
_VARIANT_SEP_RE = re.compile(r"[\s,.]+")


def stage_defaults(inputs: BuildInputs) -> TokenState:
    """Initial TokenState with the CXX-fallback rules of
    _substitute_CXX_for_missing: UNSUPPLIED (None) CPPFLAGS/LDFLAGS inherit
    CXXFLAGS (LDFLAGS only when the caller registered the slot). An
    explicitly empty slot (()) is left alone -- mirrors
    unsupplied_replacement's sentinel semantics, not emptiness."""
    cpp = inputs.cxxflags if inputs.cppflags is None else inputs.cppflags
    ld = inputs.ldflags
    if ld is None:
        ld = inputs.cxxflags if "LDFLAGS" in inputs.registered_slots else ()
    return TokenState(cpp=cpp, c=inputs.cflags, cxx=inputs.cxxflags, ld=ld)


def stage_xxpend(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """--prepend-*/--append-* composition: new tokens only, prepend
    leftmost, append rightmost (port of _do_xxpend)."""
    updates = {}
    for ts_field, in_field in _SLOT_FIELDS:
        current = getattr(ts, ts_field)
        pre = tuple(t for t in getattr(inputs, f"prepend_{in_field}") if t not in current)
        post = tuple(t for t in getattr(inputs, f"append_{in_field}") if t not in current)
        if pre or post:
            updates[ts_field] = pre + current + post
    return dataclasses.replace(ts, **updates) if updates else ts


def stage_include_paths(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """Fold inputs.include_paths into the compile slots as detached
    -I pairs, skipping paths already present as -I entries."""
    if not inputs.include_paths:
        return ts
    updates = {}
    for slot in ("cpp", "c", "cxx"):
        current = getattr(ts, slot)
        added = dedup_include_paths_to_append(current, inputs.include_paths)
        if added:
            updates[slot] = current + tuple(added)
    return dataclasses.replace(ts, **updates) if updates else ts


def stage_project_macros(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """Append CT_PROJECT_VERSION / CT_PROJECT_NAME define tokens to the
    compile slots. Values arrive pre-escaped from gather; the embedded
    double quotes are the C string literal delimiters."""
    tokens = []
    if inputs.project_version is not None:
        tokens.append(f'-DCT_PROJECT_VERSION="{inputs.project_version}"')
    if inputs.project_name is not None:
        tokens.append(f'-DCT_PROJECT_NAME="{inputs.project_name}"')
    if not tokens:
        return ts
    updates = {}
    for slot in ("cpp", "c", "cxx"):
        current = getattr(ts, slot)
        added = tuple(t for t in tokens if t not in current)
        if added:
            updates[slot] = current + added
    return dataclasses.replace(ts, **updates) if updates else ts


def stage_pkg_config_flags(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """Fold gathered pkg-config query results into the slots. --libs
    lands in ld only when the caller's CAP registered LDFLAGS -- the
    decision is input schema (the want_libs hasattr bug class is
    unrepresentable here)."""
    if not inputs.pkg_config_results:
        return ts
    want_libs = "LDFLAGS" in inputs.registered_slots
    cpp, c, cxx, ld = ts.cpp, ts.c, ts.cxx, ts.ld
    for _pkg, result in inputs.pkg_config_results:
        if result.cflags:
            cpp += result.cflags
            c += result.cflags
            cxx += result.cflags
        if want_libs and result.libs:
            ld += result.libs
    return TokenState(cpp=cpp, c=c, cxx=cxx, ld=ld)


def stage_unify(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """CPPFLAGS/CXXFLAGS unification (skipped under --separate-flags-CPP-CXX)."""
    if inputs.separate_flags:
        return ts
    unified = dedup_tokens(ts.cpp + ts.cxx)
    if unified == ts.cpp == ts.cxx:
        return ts
    return dataclasses.replace(ts, cpp=unified, cxx=unified)


def stage_prefix_map(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """Inject -ffile-prefix-map=<gitroot>=<target> into cxx/c, per-slot
    skipped when the user already set any prefix-map family flag."""
    if not inputs.gitroot:
        return ts
    flag = f"-ffile-prefix-map={inputs.gitroot}={inputs.prefix_map_target}"
    updates = {}
    for slot in ("cxx", "c"):
        current = getattr(ts, slot)
        if not has_prefix_map_token(current):
            updates[slot] = current + (flag,)
    return dataclasses.replace(ts, **updates) if updates else ts


@dataclass(frozen=True)
class SetEnv:
    name: str
    value: str


@dataclass(frozen=True)
class EnsureLinkerSymlinkDir:
    directory: str
    link_name: str
    target: str


Effect = SetEnv | EnsureLinkerSymlinkDir


def stage_wild_linker(inputs: BuildInputs, ts: TokenState) -> tuple[TokenState, tuple[Effect, ...]]:
    """Wild-linker selection decisions as pure data: the clang
    -fuse-ld=wild -> --ld-path=wild rewrite, and the wild-B -B token plus
    a symlink-dir effect for the apply layer to materialize."""
    ld = ts.ld
    effects: tuple[Effect, ...] = ()
    if inputs.link_driver_is_clang and "-fuse-ld=wild" in ld:
        ld = tuple("--ld-path=wild" if t == "-fuse-ld=wild" else t for t in ld)
    if inputs.wild_b_selected and inputs.gitroot:
        directory = f"{inputs.gitroot}/.ct-wild-ld"
        ld = ld + (f"-B{directory}",)
        effects = (EnsureLinkerSymlinkDir(directory=directory, link_name="ld", target="wild"),)
    if ld == ts.ld and not effects:
        return ts, ()
    return dataclasses.replace(ts, ld=ld), effects


def stage_dedup(inputs: BuildInputs, ts: TokenState) -> TokenState:
    """Uniform pair-aware dedup over all four slots -- runs AFTER the
    pkg-config merge, closing the CFLAGS/LDFLAGS re-append class by
    position rather than by guard."""
    return TokenState(
        cpp=dedup_tokens(ts.cpp),
        c=dedup_tokens(ts.c),
        cxx=dedup_tokens(ts.cxx),
        ld=dedup_tokens(ts.ld),
    )


@dataclass(frozen=True)
class NameState:
    variant: str
    bindir: str
    cas_objdir: str
    cas_pchdir: str
    cas_pcmdir: str
    cas_exedir: str


def _with_variant_suffix(raw: str | None, variant: str) -> str:
    if not raw:
        return ""
    normalized = posixpath.normpath(raw)
    if normalized == variant or normalized.endswith("/" + variant):
        return normalized
    # posixpath.join, not ``+ "/" +``: for the root path the latter builds
    # ``//<variant>``, and a POSIX path beginning with exactly two slashes is
    # implementation-defined rather than a plain absolute path.
    return posixpath.join(normalized, variant)


def canonical_variant_name(variant_raw: str, canonical_order) -> str:
    """THE variant naming rule. Pure, and the only implementation.

    Shared for the same reason ``cas_dir_name`` is: the variant IS the last
    path component of every cas dir, so a build that canonicalizes
    ``debug.gcc`` to ``gcc.debug`` while a diagnostic tool keeps the literal
    spelling has the two naming different pools. ``stage_resolve_names`` and
    ``resolve_cas_directory_arguments`` both call this.
    """
    tokens = [t for t in _VARIANT_SEP_RE.split(variant_raw.strip()) if t]
    return ".".join(compiletools.configutils.canonicalize_variant_tokens(tokens, list(canonical_order)))


def cas_dir_name(raw: str | None, kind: str, variant: str, gitroot: str) -> str:
    """THE cas-dir naming rule. Pure, and the only implementation.

    Both resolution paths route their naming through here so they cannot
    drift: ``stage_resolve_names`` for a ct-cake build, and
    ``apptools_argparse.resolve_cas_directory_arguments`` for the diagnostic
    tools (ct-cache-report / ct-trim-cache / ct-cleanup-locks) that parse
    with ``cap.parse_args`` and never reach the gather->compute pipeline.
    Those two must agree exactly: they name the same on-disk pool, one
    writing it and the other scanning and trimming it.

    ``raw is None`` means unsupplied and derives the gitroot-anchored
    default ``<gitroot>/cas-<kind>dir/<variant>``. Empty means explicitly
    disabled and stays empty -- it must NOT become the gitroot, which is
    what an anchoring step applied to ``""`` would produce, pointing a trim
    scan at the whole repository.

    Idempotent, so re-running on its own output is a fixed point and the
    NON_CANONICAL trim-cache class cannot be minted here.
    """
    if raw is None:
        raw = posixpath.join(gitroot, f"cas-{kind}dir")
    return _with_variant_suffix(raw, variant)


def stage_resolve_names(inputs: BuildInputs) -> NameState:
    """Variant canonicalization + bindir/cas-dir naming. All naming is a
    canonicalization fixed point: re-running on its own output changes
    nothing (the NON_CANONICAL trim-cache class cannot be minted here).

    Cas-dir naming is delegated to ``cas_dir_name``, which
    ``resolve_cas_directory_arguments`` also calls -- one implementation,
    so the paths a build writes and the paths the diagnostic tools scan
    cannot drift apart. Gather's gitroot is ``find_git_root()``, which
    falls back to the cwd outside a repo, so the default stays absolute in
    production."""
    variant = canonical_variant_name(inputs.variant_raw, inputs.canonical_order)
    if inputs.bindir_raw is None:
        bindir = "bin/" + variant
    elif inputs.bindir_raw == "":
        bindir = ""
    else:
        bindir = posixpath.normpath(inputs.bindir_raw)

    def _cas_dir(raw: str | None, kind: str) -> str:
        return cas_dir_name(raw, kind, variant, inputs.gitroot)

    return NameState(
        variant=variant,
        bindir=bindir,
        cas_objdir=_cas_dir(inputs.cas_objdir_raw, "obj"),
        cas_pchdir=_cas_dir(inputs.cas_pchdir_raw, "pch"),
        cas_pcmdir=_cas_dir(inputs.cas_pcmdir_raw, "pcm"),
        cas_exedir=_cas_dir(inputs.cas_exedir_raw, "exe"),
    )


@dataclass(frozen=True)
class BuildState:
    names: NameState
    flags: Flags
    cppflags: str
    cflags: str
    cxxflags: str
    ldflags: str
    effects: tuple[Effect, ...]
    # No production consumer reads this: it is the schema fact ("did this
    # CAP register the slot at all?") that a post-parseargs consumer cannot
    # otherwise reach, because BuildInputs is internal to gather. The live
    # reader is test_resubstitute_convergence's three-slot-cap precondition.
    registered_slots: frozenset[str]


def compute_build_state(inputs: BuildInputs) -> BuildState:
    """The pure pipeline. Stage order is load-bearing: unify -> inject ->
    unify keeps the injected prefix-map in CPPFLAGS within one pass."""
    ts = stage_defaults(inputs)
    ts = stage_xxpend(inputs, ts)
    ts = stage_include_paths(inputs, ts)
    ts = stage_project_macros(inputs, ts)
    ts = stage_pkg_config_flags(inputs, ts)
    ts = stage_unify(inputs, ts)
    ts = stage_prefix_map(inputs, ts)
    ts = stage_unify(inputs, ts)
    ts, wild_effects = stage_wild_linker(inputs, ts)
    ts = stage_dedup(inputs, ts)
    names = stage_resolve_names(inputs)
    effects: tuple[Effect, ...] = ()
    if inputs.pkg_config_path is not None:
        effects += (SetEnv(name="PKG_CONFIG_PATH", value=inputs.pkg_config_path),)
    effects += wild_effects
    return BuildState(
        names=names,
        flags=Flags(cpp=ts.cpp, c=ts.c, cxx=ts.cxx, ld=ts.ld, compiler_identity=inputs.compiler_identity),
        cppflags=shlex.join(ts.cpp),
        cflags=shlex.join(ts.c),
        cxxflags=shlex.join(ts.cxx),
        ldflags=shlex.join(ts.ld),
        effects=effects,
        registered_slots=inputs.registered_slots,
    )


def cache_naming_view(state: BuildState):
    """Everything that names CAS cells, for re-run diagnostics."""
    return (state.flags, state.names)
