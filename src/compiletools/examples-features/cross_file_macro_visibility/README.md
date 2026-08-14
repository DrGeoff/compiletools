# Cross-file macro visibility

`SimplePreprocessor` does not follow `#include`. A `#if` whose controlling
macro is `#define`d in another file is therefore unevaluable on its own, and
would fall back to assume-false and take the wrong branch. What makes those
conditions resolve correctly is the `DirectMagicFlags` convergence, which
accumulates macro state across files and re-processes them until it settles.

This fixture pins that behaviour. The object-like gates select a different
`-l` library on their false branch, so a regression there is a mislink rather
than a warning; the function-like gate emits no branch flags when unresolved,
which its `static_assert` turns into a compile error.

## Three properties, isolated

`simple_main.cpp` covers **cross-file visibility**. `platform_level.h` is
included before `simple_gate.h`, so one accumulating pass is enough to resolve
the gate. This isolates visibility from the iteration loop.

`chain_main.cpp` covers the **convergence iteration loop**. Three headers form
a dependency chain (`chain_gate.h` reads `CHAIN_MID`, `chain_mid.h` computes it
from `CHAIN_BASE`, `chain_base.h` defines `CHAIN_BASE`) and are included in
reverse order, top of chain first. Each is processed before the header that
supplies the macro it needs, so no single pass resolves the chain.

`funclike_main.cpp` covers **function-like macros across both mechanisms**.
`funclike_gate.h` calls `FUNCLIKE_AT_LEAST(2)`; its `#define` in
`funclike_def.h` is guarded by `FUNCLIKE_LEVEL` from `funclike_level.h`, and
the three are included top-of-chain first — so a single accumulating pass
never records the function-like macro at all, and its body AND parameter list
must survive into a later convergence round before the gate can expand it.
The other two trees are object-like only, so this is the coverage that catches
a regression dropping parameter lists between rounds. The gate sits under an
`#ifdef FUNCLIKE_AT_LEAST` wrapper because a real compiler hard-errors on an
undefined function-like macro inside `#if`; the observable ablation effect is
therefore "no branch flags" rather than the legacy branch. An unconditional
sentinel flag in `funclike_gate.h` lets the control distinguish "gate
unresolved" from "gate header never scanned".

**The reverse include orders in `chain_main.cpp` and `funclike_main.cpp` are
load-bearing.** Sorting either into dependency order leaves the file compiling
and the flags correct while removing coverage of the loop.

## Why these are tested rather than assumed

Ablation over 172 bundled examples and a 500-source sample of a large external
C++ codebase measured what each property is worth:

- Removing cross-file visibility changed the computed flags on 4 of 500 real
  sources, including one LDFLAGS change.
- Capping the convergence loop at a single iteration changed **nothing** across
  all 672 sources. The loop looks dead on real code. It is not: the reverse
  chain in this fixture flips to the wrong library when the loop is capped.

The second result is why this fixture exists. Corpus silence is not absence,
and a loop that appears unused is exactly the kind of thing a future cleanup
deletes.

## Self-verification

Each `main.cpp` carries a `static_assert` selecting on the macro its gate
emits, so handing the computed flags to a real compiler proves both that the
right branch was taken and that the flags are deliverable. Compiling any of
the three files **without** the computed flags is expected to fail; that is
the check working, not a broken fixture.

## Tests

`test_cross_file_macro_visibility.py`. Each property has a paired control that
ablates the mechanism and asserts the answer changes, so the fixture cannot
degrade into passing for free.
