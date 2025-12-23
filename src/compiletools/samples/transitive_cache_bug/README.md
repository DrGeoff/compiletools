# Transitive Cache Bug Sample

This sample demonstrates a cache bug in compiletools' header dependency discovery
that causes transitive dependencies to be missing from the Makefile.

## The Bug

When processing multiple source files that share headers with traditional
`#ifndef`/`#define` include guards, the `_include_list_cache` can return
stale results, causing transitive dependencies to be lost.

## How It Manifests

1. `a-game.cpp` is processed first (alphabetically)
2. It includes headers that go through `event_handler.hpp` (which has `#ifndef` guard)
3. `event_handler.hpp` is cached with its guard macro defined
4. `b-game.cpp` is processed second
5. It needs `memory_buffer.hpp` via: `task_scheduler.hpp → event_handler.hpp → memory_buffer.hpp`
6. Due to cache pollution, `memory_buffer.hpp` is missing from the dependency list
7. `memory_buffer.hpp` has `//#PKG-CONFIG=zlib`, so `-lz` is not linked
8. Build fails: `undefined reference to 'compressBound'`

## To Reproduce

```bash
cd samples/transitive_cache_bug
rm -rf bin
ct-cake -j1
```

Expected error:
```
undefined reference to `compressBound'
clang: error: linker command failed with exit code 1
```

## Root Cause

The cache key `(content_hash, macro_key)` doesn't account for the fact that
processing a header with `#ifndef` guard changes the macro state for subsequent
lookups of that same header from different include chains.

## Unit Tests

See `test_transitive_cache_bug.py` for unit tests that reproduce this bug
in isolation.
