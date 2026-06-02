# Plan 00 — Prerequisite safety nets

Close the two regression-coverage gaps found in the review BEFORE starting any
cleanup. Both are additive (new tests / measurement), neither changes behavior.

## Why

The suite is strong (1.5:1 test:source, cross-backend e2e + byte-identical CAS
checks, contract lints, an "import every entry point" test). Two gaps remain:

- **No whole-`BuildGraph` equality golden.** Tests assert *specific* rules exist
  (`test_build_backend.py:502+`) and link-signature stability
  (`test_build_backend.py:2504`, `sig1 == sig2`), but nothing asserts the entire
  graph is unchanged across a refactor. This is the cheap insurance Plan 01 needs.
- **No measured coverage baseline.** Coverage is configured
  (`pyproject.toml [tool.coverage.run]`) but unmeasured. Plan 03 splits the
  branch-heavy `apptools` substitution pipeline; a before/after coverage diff
  catches any branch that silently stops being exercised.

## Item A — whole-`BuildGraph` golden test

**Goal:** a fast, deterministic snapshot that fails on ANY change to the set/order
of rules `build_graph()` emits, for a representative multi-feature fixture.

**Approach:**
1. Add a stable serializer for a `BuildGraph` (sort-stable, path-canonical). Prefer
   reusing existing rule fields; serialize each `BuildRule` to a tuple of
   `(rule_type, output, sorted(inputs), command-with-<GITROOT>-sentinel, cwd)` so the
   golden is workspace-independent (reuse `apptools.canonicalize_path_for_cache_key`).
   If a serializer already exists for compile-DB/debug output, reuse it.
2. Pick a fixture exercising the high-churn phases: a target with PCH + a C++20 named
   module + a header-unit import + a static/shared lib + a test exe. Check
   `examples-features/` for an existing one before authoring a new fixture.
3. New test `test_build_graph_golden.py`:
   - `test_build_graph_is_deterministic`: build the graph twice, assert
     `serialize(g1) == serialize(g2)` (the missing whole-graph equality).
   - `test_build_graph_matches_golden`: assert `serialize(graph)` equals a committed
     golden string/JSON. Provide a `CT_UPDATE_GOLDEN=1` regen path.
4. Generate the golden, eyeball it for sanity, commit.

**Scope guard:** keep it to the default backend's graph (backend-agnostic phase
output). Backend-specific emission is already covered by the per-backend suites +
cross-backend e2e.

**Verification:**
```
pytest src/compiletools/test_build_graph_golden.py -q
```
Run it twice to confirm determinism is real (not accidentally seeded).

## Item B — coverage baseline

**Goal:** a recorded, reproducible coverage number for the modules about to be split,
so post-refactor coverage can be diffed.

**Approach:**
1. Capture a baseline:
   ```
   pytest -n auto --cov=compiletools --cov-report=term-missing --cov-report=xml:coverage-baseline.xml
   ```
   (`pytest-cov` is already a dev dep.)
2. Record the per-file numbers for the refactor targets — `apptools.py`,
   `build_backend.py`, `flags.py`, `magicflags.py`, `hunter.py`, `cake.py` — in this
   plan's "Baseline" section below (paste the summary rows).
3. Do NOT commit `coverage-baseline.xml` (add to `.gitignore` if needed); commit only
   the recorded numbers here so the target is reviewable.
4. After each facade split, re-run `--cov` for that module and assert the line/branch
   count did not drop. Coverage may legitimately *move* between the facade and its
   submodules — compare the SUM across the facade + new submodules, not the facade alone.

**Baseline (measured 2026-06-01 at `96c873cd`, `pytest -n auto --cov`; TOTAL 88%):**
```
apptools.py        : 89%   (1611 stmts, 170 miss)
build_backend.py   : 91%   (1665 stmts, 154 miss)
magicflags.py      : 93%   (609 stmts, 45 miss)
hunter.py          : 90%   (373 stmts, 38 miss)
cake.py            : 78%   (439 stmts, 98 miss)
flags.py           : (high; not in skip-covered tail — re-measure per Plan 03/04 diff)
```
Note: the clean `pytest -n auto` (no coverage) run is fully green (3391 passed, 0
failed). The coverage run shows 3 load-induced flakes (lock-contention, slurm,
bazel) that pass in isolation — not regressions. Post-split, compare the SUM of
coverage across the facade + its new submodules against the row above.

## Exit criteria

- `test_build_graph_golden.py` green and deterministic across two runs.
- Baseline coverage numbers recorded above.
- Full suite (`pytest -n auto`) green — this is the green baseline every later plan starts from.
