# Implied-source version guard

A `#if` guard whose macro is defined in an included header was resolved by the
assume-false fallback when the file was reached as an implied source, so the
reported dependency list named the wrong header. The build still linked and ran
correctly, because the compiler evaluates the guard properly; only compiletools'
view of the dependency closure was wrong. Fixed by walking each source under its
own converged macro state (`hunter._walk_macro_state_key`); this example is now a
regression guard.

## The issue

`hunter._expand_deps_recursive` threaded one `macro_state_key` — the root
translation unit's — through the whole closure, and `hunter.py:121` handed that
key to `headerdeps.process(implied, ...)`. `pump.cpp` is its own translation
unit, so evaluating its guard against `main.cpp`'s macro state asks a question
about the wrong TU: `main.cpp` never includes `pumpcompat.h`, so
`PUMPLIB_AT_LEAST` is undefined there.

`SimplePreprocessor` does not follow `#include` directives. Cross-file macro
visibility comes from the magicflags convergence alone
(`DirectMagicFlags.get_structured_data`: pass 1, converge, pass 2 with the
converged key). Bypass that convergence and a guard defined in an included
header is unevaluable, so `_warn_unevaluable_condition` fires and the false
branch is taken.

Path to the failure:

```
hunter.py:754  huntsource
hunter.py:121  _get_immediate_deps      <- the implied-source branch
headerdeps.py:400  process
simple_preprocessor.py:1338  _handle_if_structured
simple_preprocessor.py:1389  _warn_unevaluable_condition
```

Of the four `headerdeps.process` call sites outside `converging()`, only two
carried the defect. `hunter.py:700` (`header_dependencies`) already calls
`self.magicflags(source_filename)` first, so its key is post-convergence and its
answer was always right — which is why `dep_hash` was never affected (see *Why
it matters*). `fetch.py:1369` passes `frozenset()` deliberately: it is a
tolerant pre-fetch scan for `//#GIT=` declarations that runs before the externals
it would need in order to converge exist. It has its own separate defect,
written up in `bugreport-fetch-guarded-git-declaration.md`.

**Function-like-ness is the noise, not the cause.** Swap the guard for an
object-like control (`#if PUMPLIB_VERSION_MAJOR >= 2`) and the dependency list
was wrong in exactly the same way, silently: an undefined object-like macro is
legally 0, so there was nothing to warn about. The function-like form is worth
shipping because it makes the same defect visible.

## Test files

- `pumpver.h` — version numbers, standing in for a third-party version header
- `pumpcompat.h` — includes `pumpver.h`, defines the function-like
  `PUMPLIB_AT_LEAST` guard
- `pump.h` — the interface `main.cpp` includes; its sibling `pump.cpp` is
  reached as an implied source, which is what triggers the defect
- `pump.cpp` — guards `#include "modern_pump.h"` on `PUMPLIB_AT_LEAST(1, 2, 0)`
- `modern_pump.h` — the header the true branch includes
- `legacy_pump.h` — the header the false branch includes
- `main.cpp` — prints the flavour

## Expected behaviour

`PUMPLIB_VERSION` is 1.2.7, so `PUMPLIB_AT_LEAST(1, 2, 0)` is true and the
dependency list contains `modern_pump.h` and not `legacy_pump.h`:

```
$ ct-filelist --variant=gcc.cxx17.debug main.cpp
main.cpp
modern_pump.h
pump.cpp
pump.h
pumpcompat.h
pumpver.h
```

Before the fix it reported `legacy_pump.h` instead, and `--verbose` carried:

```
SimplePreprocessor warning: pump.cpp:7: cannot evaluate
'#if PUMPLIB_AT_LEAST(1, 2, 0)' (Unsafe expression: PUMPLIB_AT_LEAST(1, 2, 0))
- assuming false
```

`ct-cake --auto` built and the binary printed `modern` either way, so the
compiler and the dependency walk disagreed.

## Why it matters

Two consumers read `hunter.required_files`, and both got the wrong branch:
`filelist.py` (what `ct-filelist` prints) and, through
`required_source_files`, the **source set that `build_backend` and `cake`
compile and link**. A guarded branch whose header has an implied source, or
carries a `//#SOURCE=` magic flag, therefore contributes the wrong objects to
the link.

This example is the mild instance of that: both `modern_pump.h` and
`legacy_pump.h` define `pump_flavour` inline, so the wrong closure still links
and the binary is still correct. Only the reported dependency list is wrong.

`dep_hash` is **not** affected, and an earlier revision of this README claimed
otherwise. Every `namer.compute_dep_hash` caller in `build_backend.py` is fed
`hunter.header_dependencies()`, not `required_files()`, and
`header_dependencies` already resolved the guard correctly (see *The issue*).
Measured over all 44 `examples-end-to-end` dirs at `gcc.cxx17.debug`, 111 of 111
object-cache keys are byte-identical before and after the fix — including
`pump.cpp`'s.

## The fix

`hunter._walk_macro_state_key` splits the two cases the walk had conflated: a
source file is its own translation unit and gets its own converged key, while a
header inherits the key of the TU that reached it. It is applied at both the
implied-source lookup and the recursive descent — applying it at only one unions
the two branches instead of choosing between them.

The example's original write-up proposed a second half, widening `converging()`
over the hunter and fetch walks so a first-pass transient is not reported as
final. That was dropped on measurement: with the converged key in place the
guard is evaluated correctly, so `ct-filelist -v` and `ct-cake --auto -v` emit
no `cannot evaluate` line at all, and there is no remaining transient to defer.

## Testing

```bash
ct-filelist --variant=gcc.cxx17.debug main.cpp
ct-cake --variant=gcc.cxx17.debug --auto
```
