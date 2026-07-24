# Mutation testing

Mutation testing is the empirical detector of tests that **run but don't pin
behaviour**. A tool ([mutmut](https://mutmut.readthedocs.io/)) makes small
edits to the source ("mutants" -- flip a `<` to `<=`, drop a `not`, replace a
return value), reruns the tests, and reports whether the tests noticed. A
mutant the tests **kill** is behaviour they actually assert on. A mutant that
**survives** is a line the tests execute but never check -- exactly the kind of
false confidence that coverage percentage hides.

## Why per-module scoping

Running the whole suite (~3.5 min) once per mutant is not viable: a single
module has hundreds of mutants, so full-suite-per-mutant would take days.

Instead we scope each run to **one source module** and use **only that
module's focused test file** as the per-mutant test command. A focused test
file (e.g. `test_utils.py`) runs in a fraction of a second, so a few hundred
mutants finish in a couple of minutes. mutmut copies the whole package into a
throw-away `mutants/` tree so intra-package imports still resolve, but only the
one target module is mutated and only its focused tests run.

This is a per-module contract: it measures how well `test_<module>.py` pins
`<module>.py`. It does **not** catch behaviour that is only asserted by
cross-module or e2e tests. That's a deliberate trade for speed -- the signal we
want is "does this module's own test file actually check what the module does".

## Running it locally

```bash
uv sync --extra dev --extra mutation      # pulls in mutmut>=3
scripts/mutmut_module.sh utils            # mutate src/compiletools/utils.py
scripts/mutmut_module.sh utils test_utils.py   # explicit focused test file
```

The driver writes a throw-away `setup.cfg` scoped to the module (mutmut 3.x has
no CLI knob for per-module source scope + test selection), runs the campaign,
prints the surviving mutants and a machine-readable stats block, then removes
the `setup.cfg` again. Both `setup.cfg` and the `mutants/` scratch tree are
git-ignored.

Useful environment variables:

| Variable       | Effect                                                                 |
|----------------|------------------------------------------------------------------------|
| `MUTMUT_FLOOR` | Percentage; the driver exits non-zero if the score falls below it.     |
| `MUTMUT_KEEP`  | `1` keeps the generated `setup.cfg` so you can run `mutmut show <id>`.  |

To inspect a specific survivor after a run (needs the `setup.cfg` kept):

```bash
MUTMUT_KEEP=1 scripts/mutmut_module.sh utils
uv run mutmut show compiletools.utils.x__format_cycle_error__mutmut_5
uv run mutmut browse        # interactive TUI over all mutants
```

## The mutation score

```
detected = killed + timeout          # timeout == a behaviour change caught (hang)
assessed = killed + timeout + survived
score    = 100 * detected / assessed
```

Mutants with **no covering test** (`no_tests`) are a *coverage* gap, not an
*assertion* gap, so they are excluded from the score (but still reported --
they mean the focused test file never reaches that code at all). A module that
generates zero assessed mutants scores **N/A** and always passes; that happens
for pure data-holder modules (see `flags.py` below).

## Interpreting survivors

A surviving mutant names a function and a mutation index, e.g.
`compiletools.utils.x__format_cycle_error__mutmut_5`. Run `mutmut show <name>`
to see the diff. Survivors usually fall into three buckets:

1. **Genuine test weakness** -- the mutation changes real behaviour the tests
   should assert. Add/strengthen an assertion in the focused test file. This is
   the payload of the whole exercise.
2. **Cosmetic / equivalent mutants** -- e.g. a change to an error-message
   string that no test (reasonably) asserts verbatim, or a mutation with no
   observable effect. These are acceptable survivors; don't contort tests to
   kill them.
3. **Covered-elsewhere behaviour** -- pinned by a cross-module or e2e test that
   the focused file doesn't include. Out of scope for the per-module metric.

## Pilot modules and baselines

Captured 2026-07 with mutmut 3.6:

| Module     | Total | Killed | Timeout | Survived | no_tests | Score   | Floor |
|------------|-------|--------|---------|----------|----------|---------|-------|
| `utils`    | 556   | 290    | 16      | 82       | 168      | 78.9%   | 65%   |
| `flags`    | 0     | -      | -       | -        | -        | N/A     | 0%    |
| `flag_ops` | 104   | 48     | 12      | 2        | 42       | 96.8%   | 85%   |

`flags.py` is a frozen `dataclass` that delegates all real logic to
`flag_ops.py`; it has no mutable literals/operators, so mutmut generates zero
mutants. Its focused test file still passes but there is nothing to mutate --
the real mutation target is **`flag_ops.py`**, now in the pilot.

`flag_ops.py` scores 96.8% (60 of 62 assessed mutants detected, 2 survivors)
against its property tests (`test_flag_ops_properties.py`). The 42 `no_tests`
mutants are all in `filter_hash_irrelevant_tokens`, which is exercised by the
dedicated `test_hash_irrelevant_tokens.py`. mutmut 3.6 passes its test-selection
value to pytest as a **single argument**, so a per-module run can point at only
**one** focused test file -- the two cannot be combined in one scoped run. We
select the property-test file because it assesses the most mutants (62 vs 42);
the `filter_hash_irrelevant_tokens` mutants are genuinely covered in the full
suite, they just surface as `no_tests` in this narrowed mutation scope (and are
excluded from the score, per the score definition above).

The `utils.py` survivors cluster in the LDFLAGS topo-sort internals and cycle
error formatting (`_format_cycle_error`, `_ldflags_cancel_mutual_soft_edges`,
`merge_ldflags_with_topo_sort`) -- these are the weak-test hot spots to
strengthen first.

## The scheduled CI job

`.github/workflows/mutation.yml` runs the pilot **weekly** (Monday 06:17 UTC)
and on manual `workflow_dispatch`, never on push. Each module in the matrix has
a documented floor; the job fails if that module's score drops below its floor.
A manual dispatch can also target a single module (optionally with a floor
override) via the workflow inputs.

## Raising the floor (the ratchet)

Floors start **lenient** -- comfortably below the captured baseline -- so the
job is a regression alarm, not a gate that blocks day-one. To tighten:

1. Run the module locally and confirm the current score.
2. Kill some survivors by strengthening the focused test file.
3. Re-run; note the new score.
4. Raise that module's `floor` in the matrix in
   `.github/workflows/mutation.yml` to just under the new score (leave a few
   points of headroom for timeout jitter).
5. Update the baseline table above.

Never set a floor *at* the exact baseline -- timeouts and ordering can wobble a
percent or two run to run.

## Adding a module to the pilot

Pick a module whose focused `test_<module>.py` is fast (well under a second)
and self-contained (no compiler/e2e dependency). Run
`scripts/mutmut_module.sh <module>` to capture its baseline, then add a
`{module, floor}` entry to the matrix in `.github/workflows/mutation.yml` and a
row to the baseline table here.
