# Non-standard header guard fixture

Fixture for `test_magicflags.py::TestMagicFlagsModule::test_header_guard_bug_transitive_magic_flags`.

## The bug this pins

`file_analyzer.analyze_file()` grouped preprocessor directives by type
(all `#ifndef`, then all `#define`, ...) instead of by line number
before matching an `#ifndef`/`#define` pair as an include guard. That
broke detection whenever another directive sat between the two, as in
`header_a.hpp`:

```cpp
#ifndef HEADER_A_HPP_GUARD
#define SOME_OTHER_MACRO 1  // sits between the guard's ifndef and define
#define HEADER_A_HPP_GUARD
```

With the guard undetected, `HEADER_A_HPP_GUARD` was treated as an
ordinary macro rather than the file's guard. On the macro-convergence
re-pass, the preprocessor evaluated `#ifndef HEADER_A_HPP_GUARD` as
false and skipped the rest of `header_a.hpp` — including its
`#include "header_b.hpp"` — so `header_b.hpp`'s magic flags
(`//#PKG-CONFIG=zlib`, `//#LDFLAGS=-lm`) never made it into the build.

## Fix

`_extract_directives()` in `file_analyzer.py` flattens and sorts
directives by byte position before `detect_include_guard()` ever sees
them, so pairing an `#ifndef` with its `#define` is no longer sensitive
to type-grouping order. `detect_include_guard()` additionally looks
ahead up to five directives for the matching `#define` instead of
requiring it to be the very next one.

## File layout

- `main.cpp` includes `header_a.hpp`
- `header_a.hpp` — the non-standard guard shown above, includes `header_b.hpp`
- `header_b.hpp` — carries the magic flags that were being lost

## What the regression test asserts

`test_header_guard_bug_transitive_magic_flags` builds `main.cpp` and
asserts `header_b.hpp`'s magic flags (`zlib` in `PKG-CONFIG`, `-lm` in
`LDFLAGS`) survive into the final flag set. Discovery of `header_a.hpp`
and `header_b.hpp` as transitive dependencies is implied by that
result, not asserted directly.
