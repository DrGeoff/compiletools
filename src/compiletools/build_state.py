"""Pure build-state computation. MUST NOT import os, sys, or subprocess
(import-linter contract). Stages are pure functions over TokenState."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from compiletools.build_inputs import BuildInputs
from compiletools.flag_ops import dedup_include_paths_to_append, dedup_tokens, has_prefix_map_token


@dataclass(frozen=True)
class TokenState:
    cpp: tuple[str, ...] = ()
    c: tuple[str, ...] = ()
    cxx: tuple[str, ...] = ()
    ld: tuple[str, ...] = ()


_SLOT_FIELDS = (("cpp", "cppflags"), ("c", "cflags"), ("cxx", "cxxflags"), ("ld", "ldflags"))


def stage_defaults(inputs: BuildInputs) -> TokenState:
    """Initial TokenState with the CXX-fallback rules of
    _substitute_CXX_for_missing: empty CPPFLAGS/LDFLAGS inherit CXXFLAGS
    (LDFLAGS only when the caller registered the slot)."""
    cpp = inputs.cppflags or inputs.cxxflags
    ld = inputs.ldflags
    if not ld and "LDFLAGS" in inputs.registered_slots:
        ld = inputs.cxxflags
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


def stage_wild_linker(inputs: BuildInputs, ts: TokenState):
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
