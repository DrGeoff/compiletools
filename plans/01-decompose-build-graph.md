# Plan 01 — Decompose `BuildBackend.build_graph()`

Split the ~674-line `build_graph()` (`build_backend.py:1021–1695`) into a thin
orchestrator plus focused private helpers. 100% behavior-preserving — identical
`BuildGraph` output, identical rule emission order.

## Prerequisite

**[Plan 00, Item A](00-prerequisites-safety-nets.md)** (whole-`BuildGraph` golden)
MUST be green first. It is the primary net for this refactor: rule *order* into the
graph must stay byte-identical, and the golden + determinism test makes a reordering
an instant diff instead of a subtle e2e failure.

## Phase map (current)

| Phase | Lines | What it does |
|---|---|---|
| A. Setup/guard | 1027–1051 | huntsource, `all_sources`, early-return guard, `all_compile_sources`, `library_compile_sources` |
| B. Base mkdirs + dynamic-source set | 1053–1081 | objdir/exe-dir mkdir rules, sets `self._dynamic_sources` |
| C. PCH pre-pass | 1083–1209 | discovers PCH headers; sets `_pch_gch_paths`/`_pch_include_dirs`; emits gch rules |
| D. PCH mkdirs | 1201–1209 | mkdir per `pch_mkdir_dirs` |
| E. Module pre-pass setup | 1211–1270 | sets `_module_compiler_kind`, `_module_pcm_cache_root`, `_gcc_module_mapper_path`, `_module_pcm_dir` |
| F. Module iface/impl scan | 1272–1318 | populates `_module_iface_obj/_pcm/_gcm/_impl_obj`; pcm mkdirs |
| G. Header-unit pre-pass | 1320–1464 | sets `_header_unit_artefact`, `_gcc_header_unit_resolved`, `_header_unit_extra_system_includes`; HU mkdir+precompile rules |
| H. Mapper write | 1466–1469 | `self._write_gcc_module_mapper()` |
| I. Compile rules | 1471–1525 | per-source compile/clang-iface rules + compile-bucket mkdirs |
| J. Link/library/publish rules | 1527–1584 | `_add_artefact_rules` closure; static/dynamic/link rules; cas-exe mkdirs |
| K. Phony + test rules | 1586–1691 | `build`/`runtests`/`all` phonies; per-test rules; serialise-tests |

(Re-verify these line numbers before editing — they drift.)

## Target structure

`build_graph()` becomes: inline guard (A) → compute `all_compile_sources` → call helpers
in phase order, with H kept as an explicit one-liner between G and I.

| Helper | Phases | Signature (returns mutate `graph` + `self._*`) |
|---|---|---|
| `_plan_directories(graph, exe_dir)` | B | → None |
| `_plan_pch_rules(graph, all_compile_sources)` | C+D | → None |
| `_init_module_state()` | E | → `compiler_kind` |
| `_plan_module_prepass(graph, all_compile_sources)` | F | → `gcc_cache_active` |
| `_plan_header_unit_prepass(graph, all_compile_sources, gcc_cache_active)` | G | → None |
| `_plan_compile_rules(graph, all_compile_sources)` | I | → None |
| `_plan_link_and_publish_rules(graph)` | J | → `library_outputs`, `cas_exe_bucket_dirs` |
| `_plan_test_rules(graph, library_outputs)` | K | → None |

## Cross-phase locals (the hard part)

- `all_compile_sources` — consumed by C, F, G, I → pass as a **parameter** (clean set).
- `compiler_kind` / `gcc_cache_active` — **return** from `_init_module_state` /
  `_plan_module_prepass` rather than recompute, to guarantee identical truthiness.
- `library_outputs` — produced by J, consumed by K → return from J, pass to K.
- `_add_artefact_rules` (in J) and `_add_magic_si_tokens` (in G) — keep as **nested
  closures inside their owning helper**. They capture `graph`/`cas_exe_bucket_dirs` and
  local dedup sets; promoting them to methods would change capture semantics.
- All `self._*` attrs already cross via `self` — no change needed (this is why the
  refactor is low-risk).
- `pchdir`, `header_unit_flat_dir`, `compile_bucket_dirs`, `pcm_mkdir_dirs` — phase-
  internal → become helper locals.

## Ordered steps (extract bottom-up; one helper per commit)

1. `_plan_test_rules` (K) — leaf, needs only `library_outputs`.
2. `_plan_link_and_publish_rules` (J).
3. `_plan_compile_rules` (I).
4. `_plan_header_unit_prepass` (G).
5. `_plan_module_prepass` + `_init_module_state` (F+E).
6. `_plan_pch_rules` (C+D) and `_plan_directories` (B).

## Verification (after EACH step)

```
pytest src/compiletools/test_build_graph_golden.py -q          # whole-graph identity
pytest src/compiletools/test_build_backend.py -q
pytest src/compiletools/test_pch_bypass_bug.py src/compiletools/test_cxx_modules.py -q
pytest src/compiletools/test_makefile_backend.py src/compiletools/test_ninja_backend.py \
       src/compiletools/test_cmake_backend.py src/compiletools/test_bazel_backend.py \
       src/compiletools/test_shake_backend.py src/compiletools/test_slurm_backend.py -q
```

## Risks

- **No subclass overrides `build_graph`** (confirmed: only `build_backend.py:1021`), so
  extracting private helpers is MRO-safe. BUT `makefile_backend`, `bazel_backend`, and
  `trace_backend` read the `self._module_*`/`_pch_*`/`_header_unit_*` attrs in
  `generate()`/`_prebuild_aux_artefacts`. **Every attr set in `build_graph` must still be
  set on every path**, including the early-return at ~1037 (which sets none — preserve
  that). Do not lazy-init an attr only inside a helper that an early-return could skip.
- **Phase ordering is load-bearing:** H (`_write_gcc_module_mapper`) must run AFTER F and G
  populate their dicts and BEFORE I references the mapper path. Keep H an explicit
  orchestrator call. The bare-objdir dedup check (`bucket_dir == self.args.cas_objdir`,
  ~line 1516) depends on B emitting the objdir rule first — preserve B-before-I.
- **mkdir-rule ordering:** bucket-dir mkdirs are emitted *after* their consuming rules
  within each phase — keep that intra-helper ordering.
