# terminal_games — the CAS-showcase samples

Four terminal games — **Moon Lander**, **Snake**, **Space Invaders** and
**Breakout** — built by `ct-cake`. They are the project's go-to reference for
*how to structure a real, multi-program C++ codebase* so that compiletools'
content-addressable storage (CAS) does the most work for you.

The headline: all four games share one terminal facade in `common/`, so a
**single precompiled header and a single `terminal.o` compile once** and are
served from the CAS to every game. Change one game and only that game rebuilds.

## Architecture in one breath

Every game is the same three well-bounded units, each understandable on its own:

- a **pure, I/O-free C++26 named module** holding the entire simulation (the
  constants, the state type, the `step()`/`classify()` rules) — the single
  source of truth for that game;
- an **executable** that `import`s the module and `#include`s the shared
  terminal facade, then loops *read key → step → render → write*;
- a **headless test** that `import`s the module and asserts its behaviour.

The reusable half — terminal raw mode, key reads, frame writes, screen size —
lives once in `common/` and is shared by all four games.

```
terminal_games/
  ct.conf            -std=c++26 ; INCLUDE=${CONF_DIR}/common ; testmarkers=unit_test.hpp
  common/
    terminal.{h,cpp} the POSIX-terminal facade (raw mode, read_key, write_frame, rows, cols)
    pch.h            the heavy system headers, isolated behind a PCH
    unit_test.hpp    the testmarker header (UT_REQUIRE)
  moonlander/  physics.cppm   moonlander.cpp  test_physics.cpp   (module lander.physics)
  snake/       world.cppm     snake.cpp       test_snake.cpp     (module snake.world)
  invaders/    field.cppm     invaders.cpp    test_field.cpp     (module invaders.field)
  breakout/    arena.cppm     breakout.cpp    test_arena.cpp     (module breakout.arena)
```

Two rules keep the example honest and portable:

1. **A precompiled header and a module `import` never appear in the same
   translation unit** (mixing them is toolchain-fragile). Only `common/terminal.cpp`
   uses the PCH; every game's exe and test use `import`. In a TU that both
   imports a module and includes textual headers, the `#include`s come *first*
   (gcc's `-fmodules-ts` otherwise pulls the module's global-module-fragment
   headers into the global module and clashes with the textual re-includes).
2. **The simulation never learns what a terminal is.** Each `*.cppm` is pure and
   deterministic — any randomness is an explicit seed carried in the state — so
   the tests are exhaustive and run identically on every backend.

## The CAS layer each file exercises

| File | Role (auto-discovered by `ct-cake`) | CAS layer |
|---|---|---|
| `common/pch.h` | the precompiled-header payload | (the PCH itself) |
| `common/terminal.cpp` | impl, declares `//#PCH=pch.h` | **cas-pchdir** + cas-objdir — *shared by all four games* |
| `common/terminal.h` | light facade declarations | — |
| `common/unit_test.hpp` | testmarker header | — |
| `*/<game>.cppm` (×4) | module interface (impl) | **cas-pcmdir** (BMI) + cas-objdir |
| `*/<game>.cpp` (×4) | executable (`// ct-exemarker`) | cas-objdir + **cas-exedir** |
| `*/test_*.cpp` (×4) | test (includes `unit_test.hpp`) | **content-keyed test-result cache** + cas-exedir |

`ct-cake` discovers four executables (the `<game>.cpp` files, each with `main(`)
and four tests (the `test_*.cpp` files, each transitively including
`unit_test.hpp`, matching `testmarkers = unit_test.hpp`). Each game's module is
pulled in via its exe's/test's `import` edge; `common/terminal.cpp` is pulled in
via Hunter's adjacent-`.cpp` rule for `#include "terminal.h"`, resolved through
the `INCLUDE = ${CONF_DIR}/common` line in `ct.conf`.

### One PCH, one terminal.o, four games

Because the facade is shared, a clean build produces **exactly one**
`pch.h.gch` in cas-pchdir and **exactly one** `terminal.o` in cas-objdir, and
all four game executables link that same object. Edit one game's `.cppm` and
rebuild: only that game's module object and its exe are rebuilt — the shared
PCH, the shared `terminal.o`, and the other three games are all served from the
CAS untouched.

### Free cross-user reproducibility

You don't opt into it: compiletools injects `-ffile-prefix-map=<gitroot>=.` so
compile paths are workspace-relative, and folds the compiler's identity
(`realpath|size|mtime_ns`) into every CAS key. So the objects and executables
are byte-identical across checkouts and users sharing a CAS — a teammate's
first build is your cache hit, and that includes the shared `terminal.o`.

## Run it

```bash
cd src/compiletools/examples-end-to-end/terminal_games
ct-cake
```

This builds `bin/<variant>/{moonlander,snake,invaders,breakout}` plus the four
`test_*` programs, runs every test (a non-zero exit fails the build), and links
the games. Run it again and the build is near-instant — every object, BMI, PCH
and executable is served from the CAS.

Play any of them:

```bash
bin/*/snake        # space/W/A/S/D etc.; each game opens with an instructions splash
```

Each game opens with a splash screen of instructions; press any key to start
and `q` to quit. When stdin is not a terminal the game runs a short,
deterministic auto-demo and exits — so it never hangs a pipe or CI:

```bash
printf '' | bin/*/breakout     # prints a single outcome line, exits 0
```
