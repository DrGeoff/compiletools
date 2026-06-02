# Architecture cleanup plans

Behavior-preserving refactors identified in the 2026-05 architecture review.
Every plan is independently shippable. None changes runtime behavior.

## Execute in this order

1. **[00-prerequisites-safety-nets.md](00-prerequisites-safety-nets.md)** — establish the regression
   nets (whole-`BuildGraph` golden + coverage baseline) BEFORE touching anything.
2. **[04-break-import-cycle.md](04-break-import-cycle.md)** — smallest change; unblocks cleaner imports.
3. **[01-decompose-build-graph.md](01-decompose-build-graph.md)** — highest comprehension payoff.
4. **[05-wrappedos-contract-lint.md](05-wrappedos-contract-lint.md)** — pure CI win, no prod change.
5. **[02-split-build-backend.md](02-split-build-backend.md)** — facade split of the 4967-LOC module.
6. **[03-split-apptools.md](03-split-apptools.md)** — facade split of the 4421-LOC module.

## Ground rules for every plan

- **Establish a green baseline first.** Run the FULL suite (`pytest -n auto`), not targeted
  subsets — load-bearing guards (drift, byte-identity, contract lints) live in non-obvious files.
- **One logical change per commit.** Run the per-plan verification after each step.
- **Facade splits preserve import paths and object identity.** Re-export by binding
  (`from sub import name`), never by copy, so `unittest.mock.patch` targets and singletons
  (`_REGISTRY`, `_substitutioncallbacks`, module locks) keep working.
- **No silent behavior "fixes."** If a cache-clear set or substitution order looks wrong,
  preserve it exactly and raise it separately — these refactors are structure-only.
