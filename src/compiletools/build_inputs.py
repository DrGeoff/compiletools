"""Frozen inputs to the pure build-state computation.

gather_inputs (Task 12) is the ONLY producer in production; tests
construct BuildInputs literals directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PkgConfigResult:
    cflags: tuple[str, ...]
    libs: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class BuildInputs:
    registered_slots: frozenset[str]
    cppflags: tuple[str, ...] = ()
    cflags: tuple[str, ...] = ()
    cxxflags: tuple[str, ...] = ()
    ldflags: tuple[str, ...] = ()
    prepend_cppflags: tuple[str, ...] = ()
    append_cppflags: tuple[str, ...] = ()
    prepend_cflags: tuple[str, ...] = ()
    append_cflags: tuple[str, ...] = ()
    prepend_cxxflags: tuple[str, ...] = ()
    append_cxxflags: tuple[str, ...] = ()
    prepend_ldflags: tuple[str, ...] = ()
    append_ldflags: tuple[str, ...] = ()
    include_paths: tuple[str, ...] = ()
    pkg_config_results: tuple[tuple[str, PkgConfigResult], ...] = ()
    separate_flags: bool = False
    gitroot: str = ""
    prefix_map_target: str = "."
    project_version: str | None = None
    project_name: str | None = None
    link_driver_is_clang: bool = False
    wild_b_selected: bool = False
    variant_raw: str = ""
    canonical_order: tuple[str, ...] = ()
    bindir_raw: str | None = None
    cas_objdir_raw: str | None = None
    cas_pchdir_raw: str | None = None
    cas_pcmdir_raw: str | None = None
    cas_exedir_raw: str | None = None
    pkg_config_path: str | None = None
    verbose: int = 0
