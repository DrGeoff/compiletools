# #undef cache invariant fixture

Fixture for `test_preprocessing_cache.py::TestPreprocessingCache::test_invariant_cache_honors_undef`.

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

`MacroState` gained a `file_undefs` field recording which macros a
file's processing removed. Both the cache-hit and cache-miss
reconstruction paths now call `.without_keys(cached.file_undefs)`
after `.with_updates(...)`, so an `#undef` is applied on top of the
merge instead of being silently absorbed by it.

## What the regression test asserts

`test_invariant_cache_honors_undef` processes a file that `#undef`s a
macro and asserts the returned macro state no longer contains it —
pinning the cache invariant directly. `test_undef_bug_sample.py` and
`test_headertree_hunter_agreement.py` additionally drive this sample
end-to-end: they build `main.cpp`'s header dependency graph and assert
`should_be_included.hpp` is discovered and its `PKG-CONFIG` flag is
extracted.
