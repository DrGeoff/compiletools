# Implied-source version guard

A `#if` guard whose macro is defined in an included header is resolved by the
assume-false fallback when the file is reached as an implied source, so the
reported dependency list names the wrong header. The build still links and runs
correctly, because the compiler evaluates the guard properly; only compiletools'
view of the dependency closure is wrong.

## The issue

`hunter.huntsource` walks implied sources with an unconverged macro state, and
that walk is not wrapped in `converging()`. `converging(self.context)` covers
only `MagicFlagsBase._parse` (`magicflags.py`), so the four other
`headerdeps.process` call sites (`hunter.py:111`, `hunter.py:121`,
`hunter.py:700`, `fetch.py:1369`) report a first-pass transient as a final
answer.

`SimplePreprocessor` does not follow `#include` directives. Cross-file macro
visibility comes from the magicflags convergence alone. Bypass that convergence
and a guard defined in an included header is unevaluable, so
`_warn_unevaluable_condition` fires and the false branch is taken.

Path to the failure:

```
hunter.py:754  huntsource
hunter.py:121  _get_immediate_deps      <- the implied-source branch
headerdeps.py:400  process
simple_preprocessor.py:1338  _handle_if_structured
simple_preprocessor.py:1389  _warn_unevaluable_condition
```

**Function-like-ness is the noise, not the cause.** Swap the guard for an
object-like control (`#if PUMPLIB_VERSION_MAJOR >= 2`) and the dependency list
is wrong in exactly the same way, silently: an undefined object-like macro is
legally 0, so there is nothing to warn about. The function-like form is worth
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
dependency list should contain `modern_pump.h` and not `legacy_pump.h`.

Observed instead:

```
$ ct-filelist --variant=gcc.cxx17.debug main.cpp
legacy_pump.h
main.cpp
pump.cpp
pump.h
pumpcompat.h
pumpver.h
```

At `--verbose`:

```
SimplePreprocessor warning: pump.cpp:7: cannot evaluate
'#if PUMPLIB_AT_LEAST(1, 2, 0)' (Unsafe expression: PUMPLIB_AT_LEAST(1, 2, 0))
- assuming false
```

`ct-cake --auto` builds and the binary prints `modern`, so the compiler and the
dependency walk disagree.

## Why it matters

The chosen branch decides the include list and therefore `dep_hash`. A project
whose guarded block contains `#include` directives gets a `dep_hash` computed
over the wrong headers: editing a header on the taken branch does not
invalidate the object, and the build links a stale one.

## The fix

Two separable halves:

1. Extend `converging()` to the hunter and fetch walks, so a first-pass
   transient is not reported as final. This stops the warning.
2. Give the dependency walk the converged macro state, or make it iterate the
   way magicflags does. This fixes the dependency closure. Without it the guard
   is still resolved by fallback and the warning was accurate.

## Testing

```bash
ct-filelist --variant=gcc.cxx17.debug main.cpp
ct-cake --variant=gcc.cxx17.debug --auto
```
