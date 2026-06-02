# Plan 02 — Split `build_backend.py` (4967 LOC) behind a facade

Break the god-module into cohesive lower-layer modules WITHOUT changing behavior or
public import paths. `build_backend.py` keeps the `BuildBackend` ABC and becomes a
thin facade re-exporting everything its importers and tests reference.

## Why this is safe (layering)

`BuildBackend` methods call *down* into the free functions; the free functions
reference `BuildBackend`/`BuildGraph` only in docstrings — except `build_obj_info` /
`aggregate_rule_sources`, which take a `BuildGraph` param (import from
`compiletools.build_graph`, already a dependency). So extracted modules form a lower
layer; the facade imports *up* from them. No new cycles, **provided extracted modules
never `import compiletools.build_backend`**.

## Proposed modules

- **`backend_locking.py`** (≈4430–4599): `_native_flock_available`, `_build_lock_env_prefix`,
  `wrap_compile_with_lock`, `wrap_link_with_lock`, `check_lock_helper_available`,
  `report_lock_helper_missing`. Deps: stdlib + `compiletools.locking`.
- **`backend_pch.py`** (≈3974–4429): `_stage_pch_header_alongside_gch`, `_is_under`,
  `_gch_path`, `_warn_if_pchdir_not_cross_user_safe`, `_compiler_identity`,
  `_pch_command_hash`, `_pch_scope_macro_hash`, `_write_pch_scope_diagnostic`,
  `_write_pch_manifest`, `_BMI_PCH_ARTEFACT_EXTS`.
- **`backend_cxx_modules.py`** (≈218–258, 3685–3973): `_module_pcm_filename`,
  `_header_unit_arg`, `_header_unit_safe_stem`, `_extract_system_include_path_flags`,
  `_resolve_system_header_abs_paths`, `_resolve_system_header_abs_path`, `_cas_pcm_path`,
  `_pcm_command_hash`, `_write_pcm_manifest`.
- **`backend_command_args.py`** (≈49–214, 259–551): `ObjInfo`, the `_*_EXTS` /
  `CAS_PRODUCER_TYPES` / `_LINK_ENVIRONMENT_VARS` consts, `ordering_inputs_for_compile`,
  `cas_demoted_order_only`, `_link_environment_snapshot`, `_touch`, `compute_link_signature`,
  `_read_link_sig`, `_write_link_sig`, `split_compound_args`, `extract_copts`,
  `extract_include_paths`, `extract_linkopts`, `build_obj_info`, `mangle_target_name`,
  `aggregate_rule_sources`, `_toposort_rules`.
- **`backend_registry.py`** (≈4588–4967): `_REGISTRY`, `_BUILTIN_BACKEND_MODULES`,
  `_ALWAYS_AVAILABLE_BACKENDS`, slurm consts, `register_backend`, `_import_builtin_backend`,
  `get_backend_class`, `known_backend_names`, `available_backends`,
  `ensure_backends_registered`, `backend_tool_command`, `is_backend_available`,
  `detect_available_backends`, all `_slurm_*` / `_parse_slurm_mem`,
  `_register_{make,bazel,slurm}_cli_arguments`, `register_backend_cli_arguments`.
  `register_backend` annotates `type[BuildBackend]` → use `from __future__ import
  annotations` + `if TYPE_CHECKING: from compiletools.build_backend import BuildBackend`
  (runtime registry stores the decorated subclass; no `BuildBackend` value needed).
- **`build_backend.py`** keeps `BuildBackend` (≈552–3684) + the facade re-export block.

(Re-verify line ranges before moving — they drift.)

## Facade re-export surface

Enumerate authoritatively with:
```
grep -rhoE "from compiletools\.build_backend import [A-Za-z0-9_,( ]+" src/compiletools/
grep -rhoE "compiletools\.build_backend\.[A-Za-z_]+" src/compiletools/
```
Known importer surface: `BuildBackend`, `register_backend`, `CAS_PRODUCER_TYPES`,
`cas_demoted_order_only`, `aggregate_rule_sources`, `build_obj_info`, `mangle_target_name`,
`extract_linkopts`, `extract_include_paths`, `extract_copts`,
`_register_{bazel,make,slurm}_cli_arguments`, `_DEFAULT_SLURM_EXPORT`,
`_ORDER_ONLY_DEP_FORBIDDEN_EXTS`, `_compiler_identity`, `_parse_slurm_mem`,
`_BMI_PCH_ARTEFACT_EXTS`, `available_backends`, `backend_tool_command`,
`ensure_backends_registered`, `get_backend_class`, `is_backend_available`,
`known_backend_names`, `register_backend_cli_arguments`, `compute_link_signature`.

**Test-patched names that MUST stay attributes of `build_backend`** (loud failure if
moved without re-export): `_pcm_command_hash`, `_pch_command_hash`, `_pch_scope_macro_hash`,
`wrap_link_with_lock`, `wrap_compile_with_lock`, `check_lock_helper_available`,
`_native_flock_available`, `execute_compile_rule`, `execute_link_rule`, `_REGISTRY`,
`_BUILTIN_BACKEND_MODULES`.

Add an explicit `__all__` and a test asserting every name imports, to prevent silent
surface loss.

## Cycle / identity analysis

- New modules import only stdlib + `apptools`/`locking`/`build_graph`/`wrappedos`/… —
  never `build_backend`.
- `backend_registry` needs `BuildBackend` only as a hint → `TYPE_CHECKING` import.
- **Object identity is critical:** the facade must re-export the *same* `register_backend`
  function and `_REGISTRY` object (`from compiletools.backend_registry import register_backend,
  _REGISTRY, ...` — bind, not copy). Otherwise decorators populate one registry while
  `get_backend_class` reads another.
- **Mock-patch correctness:** tests patch `compiletools.build_backend._native_flock_available`
  etc., and `BuildBackend` calls these as unqualified module globals. The facade re-import
  keeps both the patch target and the call binding in `build_backend`'s namespace. **Do NOT**
  rewrite call sites to qualified `backend_locking._native_flock_available` — that would make
  the patch a silent no-op (false-confidence trap).

## Ordered steps (verify after each)

After every step run:
```
python -c "import compiletools.build_backend, compiletools.ninja_backend, compiletools.bazel_backend, compiletools.cmake_backend, compiletools.trace_backend, compiletools.makefile_backend"
pytest src/compiletools/test_build_backend.py -q
pytest src/compiletools/test_build_graph_golden.py -q
```
1. `backend_locking.py` (most self-contained) → + `test_makefile_backend.py`, `test_locking.py`.
2. `backend_command_args.py` (pure helpers) → + `test_cmake_backend.py`.
3. `backend_cxx_modules.py` → + `test_cxx_modules.py`, `test_gcc_module_cmd_hash_stable.py`.
4. `backend_pch.py` → + `test_pch_bypass_bug.py`, `test_compiler_identity_*`.
5. `backend_registry.py` (last; touches decorator timing) → + `test_cake_startup_performance.py`,
   `test_entry_point_surface.py`, then full suite `pytest -n auto`.

## Risks

- **Registry object identity** (above) — the highest-impact failure mode; mitigate with
  bind-not-copy + a test that registers a dummy backend and reads it back.
- **Mock-patch silent no-op** (above) — keep call sites on the facade namespace.
- **Startup-perf / surface lints:** before step 5, read `test_cake_startup_performance.py`
  and `test_entry_point_surface.py` to confirm they resolve names via import, not by
  AST-scanning `build_backend.py` source (the slurm CLI helpers move).
- **Interaction with Plan 01:** `build_graph()` stays in `BuildBackend`; the helpers it calls
  (`build_obj_info`, `_toposort_rules`, `aggregate_rule_sources`) move to
  `backend_command_args.py` but remain reachable via the facade — no conflict. Land Plan 01
  first so the golden is in place.
