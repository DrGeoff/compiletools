# #undef cache invariant fixture

Fixture for `test_undef_bug_sample.py` and
`test_headertree_hunter_agreement.py` (the cache-invariant unit test
`test_preprocessing_cache.py::TestPreprocessingCache::test_invariant_cache_honors_undef`
pins the same bug but builds its directives synthetically and does not
read this directory).

## The bug this pins

`preprocessing_cache.py`'s macro-state cache reconstructed a file's
outgoing macro state by merging the preprocessor's newly-defined
macros onto the incoming state (`MacroState.with_updates`). A merge
only ever adds or overwrites keys, so a macro removed via `#undef`
during that file's processing "resurrected": the incoming state still
had it, the merge never subtracted it, and the outgoing state carried
it forward as if the `#undef` had never run. Anything downstream that
branched on `#ifndef`/`#ifdef` for that macro then took the wrong path.

## File structure

- `defines_macro.hpp` — defines `TEMP_BUFFER_SIZE`
- `cleans_up.hpp` — includes `defines_macro.hpp`, then `#undef
  TEMP_BUFFER_SIZE`
- `should_be_included.hpp` — carries `//#PKG-CONFIG=leaked-macro-pkg`;
  meant to be reachable only when `TEMP_BUFFER_SIZE` is undefined
- `uses_conditional.hpp` — includes `cleans_up.hpp`, then `#ifndef
  TEMP_BUFFER_SIZE` guards the include of `should_be_included.hpp`
- `main.cpp` — includes `uses_conditional.hpp`

With the bug, `TEMP_BUFFER_SIZE` incorrectly looked defined by the time
`uses_conditional.hpp` checked it, so `should_be_included.hpp` (and
its `PKG-CONFIG` flag) was never discovered.

## Fix

`ProcessingResult` gained a `file_undefs` field recording which macros
a file's processing removed, and `MacroState` gained a
`without_keys()` method to subtract them. Both cache-hit
reconstruction paths (macro-invariant and macro-variant) now call
`.without_keys(cached.file_undefs)` after `.with_updates(...)`, so an
`#undef` is applied on top of the merge instead of being silently
absorbed by it. The cache-miss path needs no subtraction — it builds
the outgoing state from the preprocessor's post-`#undef` macros
directly and merely records `file_undefs` for later reconstruction.

## What the regression test asserts

`test_invariant_cache_honors_undef` processes a synthetic `#undef`
directive and asserts the returned macro state no longer contains the
macro — pinning the cache invariant directly. `test_undef_bug_sample.py`
drives this sample end-to-end: it builds `main.cpp`'s header dependency
graph and asserts `should_be_included.hpp` is discovered and its
`PKG-CONFIG` flag is extracted. `test_headertree_hunter_agreement.py`
uses the sample differently: it asserts only that `ct-headertree` and
the hunter walk agree on the header set, without pinning what that set
contains.
