# Plan 04 — Break the import cycle, promote deferred imports

Reduce the ~95 runtime (in-function) deferred imports. The honest finding: **only ONE
true structural cycle exists** — `flags ↔ apptools`. Everything else is either
intentional lazy-startup or a redundant re-import. This is a much smaller, safer change
than "95 imports" implies.

## Findings

**The one real cycle:** `apptools.py:31` does `from compiletools.flags import Flags`;
`flags.py` therefore defers all 5 of its apptools calls (≈lines 55, 74, 82, 92). Root cause.

**Not cycles — leave alone:** `otel.*` (cake ~550/698/720), `ccache_stats`, `build_timer`,
`textual`/`timing_tui`, `compiler_macros` (`apptools.py:1219`), and the
`preprocessing_cache.py:402` apptools deferral (explicitly commented "imported very early
at startup" — a deliberate CLI-speed optimization). These are lazy-loading of heavy/optional
deps, not cycle-avoidance.

**Redundant re-imports — just delete:** `magicflags.py:1036–1037` (re-imports modules already
top-level, for a clear-cache call) and the duplicate `build_backend` import at `cake.py:88`
(already top-level).

## Fix: extract the leaf `flag_ops.py`

`Flags` (frozen dataclass) is already dependency-free; the problem is *direction*. The 5
apptools functions `flags.py` calls are pure token helpers: `compiler_identity`,
`filter_hash_irrelevant_tokens`, `strip_d_u_tokens`, `extract_include_paths_from_tokens`,
`dedup_include_paths_to_append`.

1. Create **`compiletools/flag_ops.py`** (leaf; deps `utils`/`git_utils` only) holding those
   5 helpers.
2. `apptools` re-exports them (`from compiletools.flag_ops import ...`) for back-compat —
   preserves every existing `apptools.<fn>` call site and patch target.
3. `flags.py` imports `flag_ops` at **top-level** and drops its 4–5 deferred apptools imports.
   `apptools` keeps `from compiletools.flags import Flags`. Cycle cut.

> Note: `compiler_identity` currently lives in `apptools` and is also a Plan 03
> `apptools_compiler` candidate. If both plans run, `flag_ops` is the lower leaf — let
> `apptools_compiler` (or a thin wrapper) re-export from `flag_ops`, or keep
> `compiler_identity` in `apptools_compiler` and move only the 4 pure-token helpers to
> `flag_ops` if `compiler_identity` pulls in heavier deps. Decide at execution time by
> checking `compiler_identity`'s actual imports; keep `flag_ops` truly leaf.

## Then promote the safe leaf imports

- **flags (5):** all → top-level via `flag_ops`. Cycle gone.
- **magicflags (12):** delete redundant `1036–1037`; promote `headerdeps`,
  `preprocessing_cache`, `global_hash_registry`, `simple_preprocessor` to top-level (all
  leaf-ish, no cycle). Move `build_context` to a `TYPE_CHECKING` block (only a type at ~1380).
- **hunter (6):** promote `global_hash_registry`; the `cmdline_macro_index` runtime use (~654)
  → top-level (already `TYPE_CHECKING` for the type).
- **build_backend (6):** promote `filesystem_utils` (3×), `file_analyzer`,
  `global_hash_registry` (peers already top-level). `build_timer` stays deferred (optional path).
- **cake (8):** delete duplicate `build_backend` (~88); keep `otel`/`ccache_stats`/`build_timer`
  deferred.

## Ordered steps (verify after each)

After every step run an import smoke-check + targeted tests:
```
.venv/bin/python -c "import compiletools.apptools, compiletools.flags"
pytest src/compiletools/test_flags.py src/compiletools/test_apptools.py -q
```
1. Create `flag_ops.py`; re-export from `apptools`. Verify above.
2. Promote `flags.py`'s imports to top-level. Verify above.
3. magicflags: delete `1036–1037`, promote leaf imports, `build_context`→TYPE_CHECKING.
   `python -c "import compiletools.magicflags"` + `pytest -k "magicflags or hunter"`.
4. hunter / build_backend / cake leaf promotions + delete `cake.py:88` dup.
   `python -c "import compiletools.<m>"` per module.
5. Full suite `pytest -n auto` (covers registration / import-time side effects).

## Risks

- **Import-time side effects:** `version.py` `@functools.cache`, `magicflags` module-level
  `sz.Str`, and `@register_backend` decorators must not change first-import order of
  registration-bearing modules. Mitigated by promoting only *leaf* modules.
- **Startup cost:** do NOT promote the `preprocessing_cache.py:402` apptools deferral — it is
  an intentional CLI-speed optimization. Same for otel/textual/ccache_stats.
- **Redundant re-imports:** confirm `magicflags.py:1036–1037` are genuine dups (used only for a
  local clear-cache) before deleting.
- Land this BEFORE Plan 03 so the `flags ↔ apptools` edge is already cut when apptools splits.
