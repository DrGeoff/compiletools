# moonlander — the CAS-showcase sample

A terminal **Moon Lander** game (the VIC-20 classic): control the descent
thruster, manage your fuel, and touch down below the safe impact speed.

It is the project's go-to reference for *how to structure a real C++ program
built by `ct-cake`*, and it is deliberately laid out so that every
compiletools content-addressable-storage (CAS) layer is exercised by a
distinct, idiomatic translation unit.

## Design in one breath

Three well-bounded units, each understandable on its own:

- **`physics.cppm`** — the entire simulation as a C++26 *named module*
  (`lander.physics`). Pure, I/O-free, terminal-agnostic. The constants, the
  `LanderState` type and the rules `step()`/`classify()` live here and
  **only** here — the single source of truth.
- **`terminal.{h,cpp}`** — a tiny POSIX-terminal facade (raw mode, key read,
  frame write). It speaks `char`/`string_view` and knows nothing about the
  game. The heavy system headers it needs are isolated in **`pch.h`**.
- **`game.cpp`** — the only seam: it `import`s the simulation and `#include`s
  the terminal facade, then loops read → step → render → write.

A central rule keeps the example honest and portable: **a precompiled header
and a module `import` never appear in the same translation unit** (mixing them
is toolchain-fragile). That separation falls straight out of the clean
layering above.

## File map and the CAS layer each one demonstrates

| File | Role (auto-discovered by `ct-cake`) | CAS layer exercised |
|---|---|---|
| `physics.cppm` | module interface (impl) | **cas-pcmdir** (BMI) + cas-objdir |
| `terminal.cpp` | impl, declares `//#PCH=pch.h` | **cas-pchdir** + cas-objdir |
| `pch.h` | the precompiled-header payload | (the PCH itself) |
| `terminal.h` | light facade declarations | — |
| `game.cpp` | executable (`// ct-exemarker`) | cas-objdir + **cas-exedir** |
| `test_physics.cpp` | test (includes `unit_test.hpp`) | **content-keyed test-result cache** + cas-exedir |
| `unit_test.hpp` | testmarker header | — |

`ct-cake` discovers exactly one executable (`game.cpp` — has `main(`) and one
test (`test_physics.cpp` — has `main(` *and* transitively includes
`unit_test.hpp`, matching `testmarkers = unit_test.hpp`). `physics.cppm` and
`terminal.cpp` are pulled in automatically: the former via `game.cpp`'s/the
test's `import` edge, the latter via Hunter's adjacent-`.cpp` rule for
`#include "terminal.h"`.

### Free cross-user reproducibility

You don't opt into it, but it's worth knowing: compiletools injects
`-ffile-prefix-map=<gitroot>=.` so compile paths are workspace-relative, and
folds the compiler's identity (`realpath|size|mtime_ns`) into every CAS key.
The upshot is that the `.o` files and the executable are byte-identical across
checkouts and users sharing a CAS — so a teammate's first build is your cache
hit.

## Run it

```bash
cd src/compiletools/examples-end-to-end/moonlander
ct-cake
```

This builds `bin/<variant>/game` and `bin/<variant>/test_physics`, runs the
test (a non-zero exit would fail the build), and links the game.

Play it — the game opens with a splash screen of instructions; press any key
to launch, then space / `w` / `k` to thrust and `q` to quit:

```bash
bin/*/game
```

Run a second time and the build is near-instant — objects, the module BMI,
the PCH, and the linked executable are all served from the CAS, nothing
recompiles:

```bash
ct-cake          # CAS hit: no compile/link commands run
```

When stdin is not a terminal the game runs a short deterministic auto-demo and
exits (so it never hangs a pipe or CI):

```bash
printf '' | bin/*/game     # prints a single LANDED/CRASHED line
```
