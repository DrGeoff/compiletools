# Early Test Execution

## Goal

Today `ct-cake --auto` builds in two strictly-sequential phases: `backend.execute("build")` completes
first, then `backend.execute("runtests")` runs every test. Profiling a sudoku build with the Chrome
trace exporter (`ct-cake --timing` → `ct-timing-report --chrome-trace …`) showed
`test_combinator` was linked at t=360 ms but didn't actually execute until t=932 ms — it sat idle
for ~572 ms waiting for `sudoku.cpp` to finish compiling and `sudoku` to link:

```
 36.8ms  +290.7ms  end= 327.5ms  [compile]  test_combinator.cpp
329.2ms  + 31.2ms  end= 360.4ms  [link]     test_combinator_…
 36.5ms  +824.5ms  end= 861.0ms  [compile]  sudoku.cpp
862.8ms  + 36.5ms  end= 899.4ms  [link]     sudoku_…
931.4ms  +  2.8ms  end= 934.2ms  [phase]    test_execution
932.6ms  +  1.4ms  end= 934.0ms  [test]     test_combinator
```

Make each test executable run as soon as its link rule finishes, in parallel with continued
compilation of unrelated translation units. This is the **new default** — no flag, no opt-out.

## Constraints (from user)

- **Default behavior, no flag.** Don't introduce `--early-tests` or similar.
- **`--serialise-tests`** still exists. It means: only one test runs at a time, but tests still
  run early (during the build, not after).
- **Failure semantics.** Make/ninja's natural "stop on first test failure" is fine.
  Users who want aggregate failure reporting can pass `make -k` / `ninja -k 0` themselves; we
  don't auto-add a keep-going flag.
- **Copy the make/ninja style for the other backends.** Tests are real build-graph nodes that
  the backend's native scheduler runs during the build.
- **Single PR.**

## Architectural change

Stop calling `_run_tests` post-build. Route test execution through each backend's scheduler via the
`RuleType.TEST` rules that `_build_graph` already emits at `build_backend.py:1271-1304`. The test
rule already has `success_marker=result_path` and `order_only_deps=[exe_path]` set correctly — the
design is there; the wiring is missing in 4 of 6 backends.

`cake.py` collapses to:
```python
with timer.phase("build_execution"):
    backend.execute("build")
```
The `test_execution` phase and the second `backend.execute("runtests")` call are deleted.
Test events nest inside `build_execution` with `category="test"`; `aggregate_by_category` already
breaks them out as a sub-row.

## Tasks

Each task is implemented + reviewed + committed before the next one starts.

### Task 1 — Foundation: shared helpers + per-backend capability gate

**Files:** `src/compiletools/build_backend.py`, `src/compiletools/cake.py`,
`src/compiletools/test_framework.py` (read-only)

**Why first:** lets us migrate backends one by one without breaking master. Once all backends
flip their capability to True, Task 8 deletes the capability and the legacy `_run_tests` path.

**What to do:**

1. Add `BuildBackend._runs_tests_in_build_phase() -> bool` (default `False`). Concrete backends
   that have been migrated will override to `True`.

2. In `cake.py` (~line 329-334), gate the post-build runtests call:
   ```python
   with timer.phase("build_execution"):
       backend.execute("build")
   if (self.args.tests
       and "runtests" in graph.outputs
       and not backend._runs_tests_in_build_phase()):
       with timer.phase("test_execution"):
           backend.execute("runtests")
   ```
   This keeps the legacy path alive for un-migrated backends.

3. Extract test-execution helpers in `build_backend.py` from `_run_tests` / `_run_single_test`:
   - `_test_command_for(exe_path: str) -> list[str]`: returns the full argv including
     `TESTPREFIX` parts and framework XML argv when `--test-xml-dir` is set. Caches framework
     detection on `self._test_frameworks` exactly as today (today's lookup is at lines 1404-1413).
   - `_touch_result_marker(result_path: str) -> None`: helper used by backends that need to touch
     the success marker themselves (shake, slurm, bazel post-hoc, …). Uses `pathlib.Path.touch`
     and is a no-op (without error) if `result_path` is empty.

4. **Bake XML argv into `rule.command` at graph-build time.** In `_build_graph` at the per-test
   loop (~line 1291), replace `test_cmd = testprefix_parts + [exe_path]` with
   `test_cmd = self._test_command_for(exe_path)`. Detection requires
   `Hunter.header_dependencies(source)`, which is already populated at graph-build time. Side
   effect (strict improvement): multi-framework `ValueError` from `detect_framework` now fires at
   graph-build time, not test-run time.

5. **Important: the `_test_command_for` helper must produce identical commands to today's
   `_run_single_test` for the same inputs**, so the legacy `_run_tests` path continues working
   unchanged for un-migrated backends. After this task, all 6 backends still go through the
   legacy path (capability still False), and behavior is bit-identical to master.

**Tests:**
- Existing test suite passes unchanged (`pytest -n auto`).
- Add `test_test_command_for_xml_argv.py`: contract test that `_test_command_for(exe_path)`
  returns gtest's `--gtest_output=xml:<path>`, doctest's `--reporters=junit,...`, etc.,
  matching what `test_framework.KNOWN_FRAMEWORKS` declares.

**Commit message:** `refactor(build_backend): extract _test_command_for and add capability gate
for in-build test execution`

---

### Task 2 — Migrate make backend

**Files:** `src/compiletools/makefile_backend.py`, `src/compiletools/test_makefile_backend.py`

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True`.
2. Change the `target == "build"` branch in `_execute_build` to invoke whatever phony target
   aggregates compile/link/test rules. Inspect the current `_write_makefile` output: today
   there's almost certainly an `all: build runtests` phony (or `all: <test results> <bin exes>`).
   Use that. If the aggregate doesn't exist yet, emit it.
3. The `&& touch <success_marker>` tail rendered by `render_shell_recipe` (`build_graph.py:248`)
   already handles the success-marker touch.
4. `--output-sync=target` already set for `parallel > 1` — preserves per-target output blocks.

**Tests:**
- `test_make_runs_tests_in_build`: with a project having one slow source (use a sleep shim) +
  one fast test, assert the test's `.result` marker mtime is earlier than the slow exe's mtime.
- `test_make_test_failure_halts_build`: a deliberately-failing test → ct-cake exits non-zero;
  the failed test's `.result` is NOT touched; XML still emitted by the framework.

**Commit message:** `feat(makefile_backend): run tests as part of build phase`

---

### Task 3 — Migrate ninja backend

**Files:** `src/compiletools/ninja_backend.py`, `src/compiletools/test_ninja_backend.py`

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True`.
2. Change the `target == "build"` branch in `_execute_build` to call the aggregate phony
   (likely `ninja all`). The existing `rule test_cmd` block + `restat = 1` already correct.
3. Extend `record_rules_from_ninja_log` to classify outputs whose graph rule_type is `test` so
   the Chrome trace tags them `category="test"` instead of falling through to `_classify_output`
   (which would guess from extension and miss bare-name test execs).

**Tests:**
- `test_ninja_runs_tests_in_build`: same shape as the make version.
- `test_ninja_log_classifies_test_rules`: feed a synthetic `.ninja_log` plus a graph with a
  test rule; assert the resulting event has `category="test"`.

**Commit message:** `feat(ninja_backend): run tests as part of build phase`

---

### Task 4 — Migrate shake backend

**Files:** `src/compiletools/trace_backend.py`, `src/compiletools/test_trace_backend.py` (or
wherever shake tests live)

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True` on `ShakeBackend`.
2. Remove (or guard with `target != "build"`) the `RuleType.TEST` early-return in `_do_build`
   at `trace_backend.py:344-349`.
3. Extend `_execute_rule` (~line 430) to handle `rule.rule_type == "test"`:
   - `subprocess.run(rule.command, …)`.
   - On rc==0: `_touch_result_marker(rule.success_marker)`.
   - On rc!=0: append to `self._test_failures` (new list, init in `__init__`). Continue other
     rules — let already-in-flight work finish.
   - Record per-rule timing via the existing path.
4. After `_do_build` returns from the top-level call, raise `RuntimeError` if
   `self._test_failures` is non-empty.

**Tests:**
- `test_shake_runs_tests_in_build`: same shape.
- `test_shake_aggregates_test_failures`: two failing tests → both reported in the raised error.

**Commit message:** `feat(trace_backend): ShakeBackend runs tests as part of build phase`

---

### Task 5 — Migrate slurm backend

**Files:** `src/compiletools/trace_backend.py`, slurm tests (gated by existing skipif)

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True` on `SlurmBackend`.
2. Add `"test"` to the local-rule type set at `trace_backend.py:954` (the
   `local_rules = [r for r in graph.rules if r.rule_type not in (...)]` line).
3. In `_run_local` (or wherever local rules dispatch), add a `RuleType.TEST` branch:
   `subprocess.run(rule.command, …)` + `_touch_result_marker(rule.success_marker)` on rc==0;
   buffer failures and raise after all locals complete.
   **Do NOT route tests through `atomic_link`** — tests are pure-argv, not link actions.

**Tests:**
- `test_slurm_runs_tests_in_build`: behind the existing slurm skipif gate.

**Commit message:** `feat(trace_backend): SlurmBackend runs tests as part of build phase`

---

### Task 6 — Migrate cmake backend

**Files:** `src/compiletools/cmake_backend.py`, `src/compiletools/test_cmake_backend.py`

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True`.
2. Replace `enable_testing()` + `add_test(NAME … COMMAND …)` (lines ~262-269) with, for each
   test:
   ```cmake
   add_custom_command(
     OUTPUT <result_path>
     COMMAND <test argv>
     COMMAND ${CMAKE_COMMAND} -E touch <result_path>
     DEPENDS <exe_path>
   )
   ```
   Plus an aggregate `add_custom_target(runtests ALL DEPENDS <all-result-paths>)`. Now tests
   are real build-graph nodes; `cmake --build --parallel` runs them concurrently with the build.
   `ctest` is no longer involved.

**Tests:**
- `test_cmake_runs_tests_in_build`: same shape.
- `test_cmake_emits_custom_command_per_test`: assert the generated CMakeLists.txt contains the
  expected `add_custom_command` blocks.

**Commit message:** `feat(cmake_backend): run tests as build-graph nodes via add_custom_command`

---

### Task 7 — Migrate bazel backend

**Files:** `src/compiletools/bazel_backend.py`, `src/compiletools/test_bazel_backend.py`

**What to do:**

1. Override `_runs_tests_in_build_phase` → `True`.
2. In `_execute_build` (~line 689), when there are test targets, switch the invocation from
   `bazel build //:all` to `bazel test //:all`. Plumb `TESTPREFIX` via `--run_under=<prefix>`.
3. After bazel returns: for each `cc_test`, read `bazel-testlogs/<mangled>/test.xml` to check
   pass/fail (failures count > 0 → fail). On success, touch `<cas-path>.result` (or the
   legacy `<exe>.result`, whichever `_result_marker_path` returns) and, if `--test-xml-dir` is
   set, copy `test.xml` to `_xml_path_for(exe_path)`.
4. Bazel's own test cache may skip rerunning an unchanged test; in that case the XML is still
   present at `bazel-testlogs/.../test.xml` (stale but valid). Copy it anyway.

**Tests:**
- `test_bazel_runs_tests_in_build`: same shape.
- `test_bazel_post_publishes_xml`: assert XML lands at `_xml_path_for(exe_path)`.
- `test_bazel_touches_cas_result_marker`: assert `<cas-path>.result` touched on success.

**Commit message:** `feat(bazel_backend): use 'bazel test' and post-publish XML/result markers`

---

### Task 8 — `--serialise-tests` as a chain + cleanup + timing collapse

**Files:** `src/compiletools/build_backend.py`, `src/compiletools/cake.py`,
`src/compiletools/build_timer.py`, several existing tests

**What to do:**

1. **`--serialise-tests` reimplementation.** In `_build_graph`, after all test rules are
   emitted but before they're returned: if `args.serialise_tests`, sort test rules
   deterministically (by `source` path), then inject each rule's `success_marker` into the
   *next* rule's `inputs` (or `order_only_deps` if `--no-use-mtime`). All 6 backends see the
   strict ordering natively. No `.NOTPARALLEL` needed.
2. **Delete `_run_tests`, `_run_tests_sequential`, `_run_tests_parallel`, `_run_single_test`,
   `_test_frameworks` cache** (now dead — all backends override `_runs_tests_in_build_phase`).
3. **Remove `_runs_tests_in_build_phase` capability** itself — also dead.
4. **Collapse cake.py phases.** Delete the `if … "runtests" in graph.outputs: …
   with timer.phase("test_execution"): backend.execute("runtests")` block. Test events nest
   inside `build_execution`.
5. **Drop `test_execution` from `BuildTimer.AGGREGATING_PHASES`.** Aggregation by category
   still works under `build_execution`.
6. **Update existing tests** that assert on the two-phase shape. Search:
   `grep -rn "test_execution\b" src/compiletools/test_*.py`.

**Tests:**
- `test_serialise_tests_chains_results`: with two tests A and B, assert B's success marker
  mtime > A's success marker mtime even with `-j 4`.
- `test_timing_collapsed_phase_shape`: assert `timing.json` has `build_execution` containing
  `category="test"` events; assert `test_execution` is absent.
- All existing tests pass.

**Commit message:** `feat: collapse build_execution/test_execution into one phase; delete
legacy _run_tests path`

---

### Task 9 — Smoke verification on /home/geoff/code/sudoku

**Files:** none (verification only)

**What to do:**

1. From the early-tests worktree, with its venv active, run on sudoku:
   ```bash
   cd /home/geoff/code/sudoku && rm bin/gcc.cxx26.debug/diagnostics/*/timing.json
   ct-cake --timing
   ```
2. Convert the resulting timing.json to a Chrome trace and inspect via the chrome-devtools-mcp:
   ```bash
   ct-timing-report --chrome-trace /tmp/sudoku-trace-after.json <new timing.json>
   ```
3. Compare with the pre-change trace events. Expected: `test_combinator` `[test]` starts well
   before `sudoku.cpp` `[compile]` ends; the ~572 ms idle bubble shrinks to ≤50 ms.

**Commit message:** none (verification task; no file changes)

---

## Risks / open issues

1. **Bazel test cache + stale XML** — bazel may skip rerunning an unchanged test, leaving the
   prior `test.xml` in place. Task 7 must copy whatever is there; that's still the correct
   "last known result." Document this in a CLAUDE.md note.
2. **CMake `add_custom_command` doesn't carry per-rule timing.** cmake builds won't get
   per-test timing events in `timing.json`. Acceptable; cmake's timing fidelity is already
   coarse.
3. **Test stdout/stderr interleaving with compile output.** Per-target atomicity preserved by
   `--output-sync=target` for make; ninja is per-edge by default. Accepted.
4. **`make -k` / `ninja -k 0` users.** Existing users who pass keep-going flags continue to
   work; we don't auto-add them.
