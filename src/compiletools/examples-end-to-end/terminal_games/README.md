# terminal_games — the CAS-showcase samples

Four terminal games — **Moon Lander**, **Snake**, **Space Invaders** and
**Breakout** — plus a controls-free **ASCII Aquarium** artwork, built by
`ct-cake`. They are the project's go-to reference for
*how to structure a real, multi-program C++ codebase* so that compiletools'
content-addressable storage (CAS) does the most work for you.

The headline: all five programs share one terminal facade in `common/`, so a
**single precompiled header plus two shared objects — `terminal.o` and
`frontend.o` — each compile once** and are served from the CAS to all five.
Change one program and only that one rebuilds.

## Architecture in one breath

Every game is a pure, I/O-free C++26 simulation **split into a module interface
unit (`.cppm`) and an implementation unit (`_impl.cpp`)**, plus an executable
that `import`s the module and `#include`s the shared terminal facade, and one or
more headless tests that assert its behaviour. Snake, invaders and breakout
**decompose into a small module graph at natural seams** — snake pulls out an
`rng` leaf; invaders splits a `formation` leaf and a `bullet` unit that imports
it, both under a `field` aggregate; breakout isolates a `bricks` leaf under an
`arena` aggregate — each
with a re-exporting aggregate module. **Moonlander stays a single cohesive
module** (a lander is one integrator with no honest seam): the rule is
*decompose at real seams, stay cohesive otherwise.*

The fifth program, the **ASCII Aquarium**, follows the same four-unit shape with
no controls except `q`: its executable loops *step → render → write* over a
pure, seeded simulation of drifting fish, rising bubbles and swaying seaweed —
a calm, colourful artwork rather than a game.

The reusable half — terminal raw mode, key reads, frame writes, screen size —
lives once in `common/` and is shared by all five programs.

```
terminal_games/
  ct.conf            -std=c++26 ; INCLUDE=${CONF_DIR}/common ; testmarkers=unit_test.hpp
  common/
    terminal.{h,cpp} the POSIX-terminal facade (raw mode, read_key, write_frame, rows, cols)
    frontend.{h,cpp} shared splash/wait scaffolding + ANSI vocabulary, built on terminal.h
    test_frontend.cpp unit test for the splash_screen builder
    pch.h            the heavy system headers, isolated behind a PCH
    unit_test.hpp    the testmarker header (UT_REQUIRE)
  moonlander/  physics.cppm physics_impl.cpp              (module lander.physics)
               moonlander.cpp  test_physics.cpp
  snake/       rng.cppm world.cppm world_impl.cpp           (modules snake.rng, snake.world)
               snake.cpp  test_rng.cpp  test_snake.cpp
  invaders/    formation.cppm formation_impl.cpp            (modules invaders.formation,
               bullet.cppm bullet_impl.cpp                   invaders.bullet, invaders.field)
               field.cppm field_impl.cpp
               invaders.cpp  test_formation.cpp  test_bullet.cpp  test_field.cpp
  breakout/    bricks.cppm bricks_impl.cpp                  (modules breakout.bricks,
               arena.cppm arena_impl.cpp                     breakout.arena)
               breakout.cpp  test_bricks.cpp  test_arena.cpp
  aquarium/    water.cppm fish.cppm bubbles.cppm seaweed.cppm tank.cppm   (5 module interfaces)
               fish_impl.cpp bubbles_impl.cpp seaweed_impl.cpp tank_impl.cpp  (4 implementation units)
               aquarium.cpp   test_fish.cpp test_bubbles.cpp test_seaweed.cpp test_tank.cpp
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
| `common/terminal.cpp` | impl, declares `//#PCH=pch.h` | **cas-pchdir** + cas-objdir — *shared by all five programs* |
| `common/terminal.h` | light facade declarations | — |
| `common/frontend.cpp` | shared splash/ANSI scaffolding (no PCH) | cas-objdir — *shared by all five programs* |
| `common/frontend.h` | splash/ANSI vocabulary declarations | — |
| `common/unit_test.hpp` | testmarker header | — |
| `*/<unit>.cppm` (×13) | module interface (impl) | **cas-pcmdir** (BMI) + cas-objdir |
| `*/*_impl.cpp` (×11) | module implementation unit (`module <name>;`) | cas-objdir — *no BMI* |
| `*/<name>.cpp` (×5) | executable (`// ct-exemarker`) | cas-objdir + **cas-exedir** |
| `*/test_*.cpp` (×13) | test (includes `unit_test.hpp`) | **content-keyed test-result cache** + cas-exedir |

`ct-cake` discovers five executables (the per-program `.cpp` files, each with
`main(`) and thirteen tests (the `test_*.cpp` files, each transitively including
`unit_test.hpp`, matching `testmarkers = unit_test.hpp`). Each program's module is
pulled in via its exe's/test's `import` edge; `common/terminal.cpp` is pulled in
via Hunter's adjacent-`.cpp` rule for `#include "terminal.h"`, resolved through
the `INCLUDE = ${CONF_DIR}/common` line in `ct.conf`.

### One PCH, two shared objects, five programs

Because the facade is shared, a clean build produces **exactly one**
`pch.h.gch` in cas-pchdir, **exactly one** `terminal.o` in cas-objdir, and
**exactly one** `frontend.o` in cas-objdir, and all five executables link both
shared objects. Edit one program's `.cppm` and rebuild: only that program's
module object and its exe are rebuilt — the shared PCH, the shared `terminal.o`,
the shared `frontend.o`, and the other four programs are all served from the CAS
untouched.

### The aquarium: one program, five modules, auto-discovered

The aquarium is split into a small module graph to show off how `ct-cake`
discovers the files that make up a program — with no file list anywhere:

    aquarium.water                      (the LCG + geometry; a leaf, no impl unit)
    aquarium.fish  .bubbles  .seaweed   (interface .cppm + implementation _impl.cpp)
    aquarium.tank                       (re-exports the four; interface + _impl.cpp)

Most modules follow the interface/implementation split: the `.cppm` interface
unit declares the types and function signatures, and a sibling
`<module>_impl.cpp` (`module aquarium.M;`) holds the definitions.
`aquarium.water` needs no implementation unit — it is all `constexpr` primitives.

`aquarium.cpp` contains a single `import aquarium.tank;`. From that one edge
`ct-cake` resolves every imported module name to its interface **and** its
implementation unit — compiling the five interface BMIs and linking the four
`_impl.cpp` objects, with no file list anywhere. Each `test_*` program imports
only the slice it needs (`test_seaweed.cpp` → `aquarium.seaweed`, which pulls
its `_impl.cpp` and `aquarium.water` behind it; `test_tank.cpp` → the whole
graph), so ct-cake compiles and links only the sub-graph each test touches.

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

This builds `bin/<variant>/{moonlander,snake,invaders,breakout,aquarium}` plus
the thirteen `test_*` programs, runs every test (a non-zero exit fails the build),
and links the executables. Run it again and the build is near-instant — every
object, BMI, PCH and executable is served from the CAS.

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

The **aquarium** is the odd one out — no controls except `q`, just a calm,
colourful artwork. Like the games it opens with a splash, animates while stdin
is a terminal, and falls back to a one-line deterministic demo otherwise:

```bash
bin/*/aquarium                 # press any key to dive in, q to quit
printf '' | bin/*/aquarium     # prints a single summary line, exits 0
```
