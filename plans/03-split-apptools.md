# Plan 03 — Split `apptools.py` (4421 LOC, 34 importers) behind a facade

Break the most-imported god-module into cohesive modules WITHOUT changing behavior or
public import paths. `apptools.py` keeps the substitution/parseargs core and becomes a
thin facade re-exporting everything its 34 importers and the tests reference.

## Prerequisite

**[Plan 00, Item B](00-prerequisites-safety-nets.md)** (coverage baseline) MUST be
recorded first. This module has the most branch-heavy code (the substitution pipeline);
after each extraction, compare the SUM of coverage across the facade + new submodules to
the baseline and assert it did not drop.

## Proposed modules

- **`apptools_canonicalize.py`** (leaf; deps `wrappedos`/`utils` only): `_GITROOT_SENTINEL`,
  `_canonicalize_one_path_to_target`, `_canonicalize_one_path`, `canonicalize_path_for_cache_key`,
  `canonicalize_path_for_command`, `canonicalize_paths_for_cache_key`,
  `_canonicalize_tokens_to_target`, `canonicalize_for_cache_key`, `canonicalize_for_command`,
  `_PATH_BEARING_FLAGS`.
- **`apptools_compiler.py`** (holds 6 of 8 cached fns): `compiler_identity`,
  `find_system_std_module_source`, `compiler_kind`, `compiler_default_cxx_std`,
  `_get_functional_cxx_compiler_cached`, `derive_c_compiler_from_cxx`,
  `get_functional_cxx_compiler`, `_test_compiler_functionality`, `tool_version`,
  `_compiler_major_version`.
- **`apptools_pkgconfig.py`**: `cached_pkg_config`, `filter_pkg_config_cflags`,
  `_pkg_config_provenance_label`, `_setup_pkg_config_overrides`,
  `_setup_pkg_config_overrides_locked`, `_add_flags_from_pkg_config`, `_batch_pkg_config`,
  `_PKG_CONFIG_OVERRIDE_LOCK`.
- **`apptools_argparse.py`**: all `add_*_arguments*`, `create_parser`, `parser_has_option`,
  `_add_xxpend_argument`, `_add_xxpend_arguments`, `_AccumulatingConfigFileParser`,
  `_ComposingArgumentParser`, `_open_conf_file_utf8`, `_expand_conf_dir`, `_expand_env_and_user`,
  conf-dir sentinels, `_fix_variable_handling_method`, `resolve_cas_directory_arguments`,
  `validate_otel_timing_pair`, `_user_passed_no_timing`.
- **`apptools_validate.py`**: `_check_resolved_compiler_available`, `_check_wild_linker_usable`,
  `_check_compiler_supports_requested_standard`, `_check_legacy_variant_config_keys`,
  `_check_legacy_cas_config_keys`, `_STD_MIN_COMPILER_VERSION`, the legacy-key regexes.
- **Keep in `apptools.py` (the core — do NOT split):** `parseargs`, `substitutions`,
  `_commonsubstitutions`, `_finalize_flag_state`, `check_flag_string_drift`, all `_*`
  substitution/flag helpers (tokenization, `_inject_ffile_prefix_map`, `_normalize_wild_linker`,
  `_materialize_wild_b_searchdir`, project version/name macros, `_unify_cpp_cxx_flags`,
  `_deduplicate_all_flags`, etc.), macro/include extraction, and the callback list
  (`_substitutioncallbacks`, `resetcallbacks`, `registercallback`) — plus the facade
  re-export block.

(Re-verify membership/line ranges before moving.)

## Facade re-export surface

Enumerate authoritatively:
```
grep -rhoE "from compiletools\.apptools import [A-Za-z0-9_,( ]+" src/compiletools/
grep -rhoE "compiletools\.apptools\.[A-Za-z_]+" src/compiletools/
```
**Private names reached by importers/tests** (must be re-exported by binding, preserving
identity): `_parser_has_option`, `_substitutioncallbacks`, `_PATH_BEARING_FLAGS`,
`_inject_ffile_prefix_map`, `_normalize_wild_linker`, `_materialize_wild_b_searchdir`,
`_check_resolved_compiler_available`, `_check_wild_linker_usable`,
`_check_compiler_supports_requested_standard`, `_compiler_major_version`,
`_setup_pkg_config_overrides`, `_pkg_config_provenance_label`, `_extend_includes_using_git_root`,
`_finalize_flag_state`, `_commonsubstitutions`, `_check_legacy_variant_config_keys`,
`_ComposingArgumentParser`, `_GITROOT_SENTINEL`.

**Test-patched names that must stay attributes of `apptools`:** `add_base_arguments`,
`add_locking_arguments`, `add_output_directory_arguments`, `create_parser`, `parseargs`,
`resolve_cas_directory_arguments`, `tool_version`, `terminalcolumns`, `verbose_print_args`,
`subprocess` (module patched directly — keep `import subprocess` in `apptools.py`).

## `clear_cache()` coherence (critical)

`apptools.clear_cache()` (currently `apptools.py:2322`) resets multiple cached functions.
After the split, give each submodule its own `clear_cache()` covering only its cached fns,
and have the facade `clear_cache()` fan out to all of them. Cached fns to account for:
`compiler_identity`, `find_system_std_module_source`, `compiler_kind`,
`compiler_default_cxx_std`, `_get_functional_cxx_compiler_cached`, `tool_version`
(→ `apptools_compiler`); `cached_pkg_config` (→ `apptools_pkgconfig`); and the conf-parse
cache `_parse_conf_file_cached` (locate its home — likely `configutils`). **Preserve the
CURRENT clear set exactly** — if `clear_cache` today omits a cached fn, keep that omission;
do not silently "fix" it (raise separately if it looks wrong).

## Cycle analysis

- **`apptools ↔ git_utils` is a pre-existing top-level cycle.** `git_utils` needs only
  `parser_has_option` + `create_parser` (both move to `apptools_argparse`). Keep `git_utils`
  importing the **facade** (`compiletools.apptools`); do not point it at submodules.
- `flags.py` uses function-local apptools imports → no cycle; keep importing the facade.
  (Plan 04 cuts the deeper `flags ↔ apptools` cycle via `flag_ops.py`; land Plan 04 first.)
- New-module DAG: `canonicalize` (leaf) ← `compiler`/`pkgconfig`/`validate` ← `argparse` ←
  `apptools` core. No back-edges. Cross-submodule calls (e.g. `validate` → `compiler`) import
  the submodule directly, not the facade, to avoid load-order issues.

## Ordered steps (verify after each)

After every step:
```
python -c "import compiletools.apptools"
pytest src/compiletools/test_apptools.py src/compiletools/test_cas_dir_resolver_contract.py \
       src/compiletools/test_multiuser_cache.py src/compiletools/test_relative_cas_dir_bug.py -q
```
1. `apptools_canonicalize.py` (leaf; gated by byte-identity tests).
2. `apptools_compiler.py` (move its cached fns; wire submodule + facade `clear_cache`).
3. `apptools_pkgconfig.py`.
4. `apptools_validate.py`.
5. `apptools_argparse.py` (largest; `_ComposingArgumentParser`/`_AccumulatingConfigFileParser`
   last). Then full suite `pytest -n auto`.
6. Trim `apptools.py` to core + re-export block; re-run `--cov`, compare to baseline.

## Risks

- **Substitution ordering is behavior-affecting** — keep `substitutions`/`_commonsubstitutions`
  and all `_*` substitution helpers in one module; the `check_flag_string_drift` guard catches
  accidental reordering.
- **Cache-reset coherence** — a missed cached fn → stale cross-test results; match the current
  clear set exactly.
- **`_substitutioncallbacks` identity** — mutated by `resetcallbacks`; must remain a single
  shared list defined in the facade module, not a submodule.
- **configargparse subclasses** — move `_ComposingArgumentParser` + `_AccumulatingConfigFileParser`
  together with `_open_config_files`/`parse_known_args` intact; guarded by
  `TestAppendFlagsAccumulateAcrossConfHierarchy`.
- **Mock-patch namespace** — keep patched names as `apptools` attributes; don't rewrite callers
  to submodule-qualified imports (silent no-op trap).
