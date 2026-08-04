"""Pure build-state computation. MUST NOT import os, sys, or subprocess
(import-linter contract). Stages are pure functions over TokenState."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from compiletools.build_inputs import BuildInputs
from compiletools.flag_ops import dedup_include_paths_to_append


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
