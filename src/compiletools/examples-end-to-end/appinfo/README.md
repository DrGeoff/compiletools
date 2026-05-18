# appinfo — preferred name/version pattern

This is the **preferred** way to bake project name, version, build
timestamp, or any other small per-build constants into a C/C++
binary while preserving the build cache.

## What it does

* `appinfo.hpp` declares stable `extern const char* const` symbols.
  It is checked-in and **never changes across builds**.
* `gen_appinfo.sh` runs as `--prebuild-script` and writes
  `appinfo.cpp` containing the definitions, taking values from env
  (`APP_NAME`, `APP_VERSION`) with `git describe` fallback.
* `main.cpp` `#include`s `appinfo.hpp` and reads the symbols.
  ct-cake's implied-source mechanism finds the generated
  `appinfo.cpp` adjacent to the header and links it into the exe.

## Why a generated `.cpp`, not a generated `.h`?

This is the whole point of the example. A generated **header**
poisons the build cache; a generated **implementation file** does
not. With the impl-file pattern:

| State                                | After `APP_VERSION` bump                                            |
|--------------------------------------|---------------------------------------------------------------------|
| `cas-pchdir` (PCH)                   | Cache HIT — `appinfo.hpp` is stable, PCH command hash unchanged.    |
| Every consumer `.o` in `cas-objdir`  | Cache HIT — `dep_hash` unchanged because no included file changed.  |
| `appinfo.cpp`'s `.o` in `cas-objdir` | MISS — its `file_hash` changed. This is the one TU that recompiles. |
| Final link                           | Picks up the new symbol value.                                      |

With a generated header the version change flows transitively into
every TU that `#include`s it (directly or via PCH), invalidating
every consumer's `dep_hash` and triggering a project-wide rebuild.
For a one-line version string this is needless waste.

## Why not `--project-version` / `--project-name`?

Those flags inject `-DCT_PROJECT_VERSION="..."` into CXXFLAGS, which
is even worse: ct-cake's macro scope filter (see
`README.ct-cake.rst`) pulls the macro into every TU whose
transitive headers *textually mention* the macro name — including a
single line of header documentation. The flags are deprecated; this
example replaces them.

## Run

```bash
./build.sh                                    # uses git describe
APP_NAME=myapp APP_VERSION=2.0.0 ./build.sh   # explicit override
```

Expected output (with the defaults):

```
name=demo_app
version=<git-describe-output>
```
