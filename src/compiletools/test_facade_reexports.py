"""Guard: facade modules must re-export extracted symbols by *binding*.

The architecture-cleanup refactor split two god-modules (`build_backend.py`,
`apptools.py`) into focused lower-layer modules, keeping the originals as thin
facades that re-export the moved names so existing importers and
``unittest.mock.patch`` targets keep working.

Several of those re-exports have NO remaining caller inside the facade module
(their only users are other modules or tests that reach them via the facade).
Such re-exports look like dead imports to ``ruff``: a future ``ruff --fix`` can
silently delete them, which would break consumers only at runtime -- and for
CAS-key helpers like ``canonicalize_path_for_cache_key`` the breakage is a
silent wrong-cache-key, not an import error.

This test pins the contract: each load-bearing name must be present on the
facade AND be the *same object* as in its source module. If a re-export is
dropped (or accidentally copied instead of bound), this fails loudly.
"""

from __future__ import annotations

import importlib

import pytest

# (facade_module, attr_name, source_module) triples.
# A name here must resolve to getattr(source) by identity on the facade.
_REEXPORTS: list[tuple[str, str, str]] = []


def _add(facade: str, source: str, names: list[str]) -> None:
    for name in names:
        _REEXPORTS.append((facade, name, source))


# --- apptools facade --------------------------------------------------------
_add(
    "compiletools.apptools",
    "compiletools.apptools_canonicalize",
    [
        "canonicalize_path_for_cache_key",  # NO internal caller -> ruff-fragile
        "canonicalize_for_cache_key",
        "canonicalize_path_for_command",
        "canonicalize_for_command",
        "canonicalize_paths_for_cache_key",
        "_GITROOT_SENTINEL",
        "_PATH_BEARING_FLAGS",
        "_PREFIX_MAP_FLAG_PREFIXES",
    ],
)
_add(
    "compiletools.apptools",
    "compiletools.apptools_compiler",
    [
        "compiler_identity",
        "compiler_kind",
        "compiler_default_cxx_std",
        "get_functional_cxx_compiler",
        "find_system_std_module_source",
        "tool_version",
        "derive_c_compiler_from_cxx",
        "_compiler_major_version",
    ],
)
_add(
    "compiletools.apptools",
    "compiletools.apptools_pkgconfig",
    [
        "cached_pkg_config",
        "filter_pkg_config_cflags",
        "_setup_pkg_config_overrides",
        "_setup_pkg_config_overrides_locked",
        "_add_flags_from_pkg_config",
        "_batch_pkg_config",
        "_pkg_config_provenance_label",
        "_PKG_CONFIG_OVERRIDE_LOCK",  # threading.Lock -- identity is load-bearing
        "_PkgConfigOrigin",
    ],
)
_add(
    "compiletools.apptools",
    "compiletools.apptools_validate",
    [
        "_check_resolved_compiler_available",
        "_check_wild_linker_usable",
        "_check_compiler_supports_requested_standard",
        "_check_legacy_variant_config_keys",
        "_check_legacy_cas_config_keys",
        "_STD_MIN_COMPILER_VERSION",
        "_LEGACY_CAS_KEY_RE",  # compiled regex -- identity is load-bearing
        "_LEGACY_VARIANT_KEY_RE",  # compiled regex -- identity is load-bearing
    ],
)
_add(
    "compiletools.apptools",
    "compiletools.flag_ops",
    [
        "strip_d_u_tokens",
        "filter_hash_irrelevant_tokens",
        "dedup_include_paths_to_append",
        "extract_include_paths_from_tokens",
    ],
)

# --- build_backend facade ---------------------------------------------------
_add(
    "compiletools.build_backend",
    "compiletools.backend_locking",
    [
        "wrap_compile_with_lock",
        "wrap_link_with_lock",
        "_native_flock_available",
        "check_lock_helper_available",
        "report_lock_helper_missing",
    ],
)
_add(
    "compiletools.build_backend",
    "compiletools.backend_command_args",
    [
        "build_obj_info",
        "compute_link_signature",
        "CAS_PRODUCER_TYPES",
        "extract_copts",
        "extract_include_paths",
        "extract_linkopts",
        "mangle_target_name",
        "aggregate_rule_sources",
        "cas_demoted_order_only",
        "ObjInfo",
    ],
)
_add(
    "compiletools.build_backend",
    "compiletools.backend_cxx_modules",
    [
        "_pcm_command_hash",
        "_cas_pcm_path",
        "_write_pcm_manifest",
        "_resolve_system_header_abs_path",
        "_resolve_system_header_abs_paths",
        "_module_pcm_filename",
    ],
)
_add(
    "compiletools.build_backend",
    "compiletools.backend_pch",
    [
        "_pch_command_hash",
        "_gch_path",
        "_write_pch_manifest",
        "_PCHDIR_WARNED",  # mutable set -- identity is load-bearing
        "_is_under",
        "_stage_pch_header_alongside_gch",
    ],
)
_add(
    "compiletools.build_backend",
    "compiletools.backend_registry",
    [
        "_REGISTRY",  # shared mutable dict -- identity is load-bearing
        "register_backend",
        "get_backend_class",
        "ensure_backends_registered",
        "available_backends",
        "known_backend_names",
        "register_backend_cli_arguments",
    ],
)


@pytest.mark.parametrize(
    "facade_mod, attr, source_mod",
    _REEXPORTS,
    ids=[f"{f.split('.')[-1]}.{a}" for f, a, _ in _REEXPORTS],
)
def test_facade_reexports_by_binding(facade_mod: str, attr: str, source_mod: str) -> None:
    facade = importlib.import_module(facade_mod)
    source = importlib.import_module(source_mod)
    assert hasattr(source, attr), f"{source_mod}.{attr} missing (moved/renamed?)"
    assert hasattr(facade, attr), (
        f"{facade_mod}.{attr} was dropped -- a re-export was lost "
        f"(likely a ruff --fix removed an 'unused' import). "
        f"Restore the binding from {source_mod}."
    )
    assert getattr(facade, attr) is getattr(source, attr), (
        f"{facade_mod}.{attr} is NOT the same object as {source_mod}.{attr} (re-exported by copy instead of binding?)."
    )
