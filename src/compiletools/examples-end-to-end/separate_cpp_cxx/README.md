# separate_cpp_cxx Sample

Demonstrates the `--separate-flags-CPP-CXX` toggle. By default ct-cake
**unifies** `//#CPPFLAGS` and `//#CXXFLAGS` (and the corresponding
`--CPPFLAGS` / `--CXXFLAGS` CLI options) into a single deduplicated set.
With `--separate-flags-CPP-CXX` the two slots stay disjoint.

## Inspect, don't build

The user-visible test is via `ct-magicflags`:

```bash
# Unified (default): CPPFLAGS and CXXFLAGS contain the same merged set.
ct-magicflags main.cpp

# Separated: each slot only contains what its own annotation declared.
ct-magicflags --separate-flags-CPP-CXX main.cpp
```

In unified mode you'll see `-DFROM_CPP=1 -DFROM_CXX=1
-DPREPROCESSOR_ONLY -DUNIFIED_FLAGS_SEEN` echoed under both `CPPFLAGS`
and `CXXFLAGS`. In separated mode each line shows only its own
annotation's tokens.

## Build

Both modes compile and run; the program is just a sanity check that
both `FROM_CPP` and `FROM_CXX` reach the C++ frontend. The difference
matters when one slot must carry tokens the other shouldn't see — for
example, a preprocessor-only directive that would confuse the codegen
stage.
