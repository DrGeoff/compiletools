# #undef Bug Sample

## Purpose
This sample demonstrates a critical bug in `preprocessing_cache.py` where `#undef` directives are not properly handled when reusing cached results or building updated macro state.

## The Bug

When processing multiple files in sequence, the preprocessing cache fails to remove macros that have been undefined via `#undef`. This causes:
1. Macros to "resurrect" after being explicitly cleaned up
2. Wrong conditional compilation paths to be taken
3. Missing or incorrect dependencies

## File Structure

### `defines_macro.hpp`
Defines `TEMP_BUFFER_SIZE` macro for internal use.

### `cleans_up.hpp`
- Includes `defines_macro.hpp` (gets TEMP_BUFFER_SIZE)
- Uses the macro
- **Cleans up with `#undef TEMP_BUFFER_SIZE`** (good C++ hygiene)

### `should_not_see_macro.hpp`
Contains alternative implementation and `PKG-CONFIG=leaked-macro-pkg`. This header should ONLY be included when `TEMP_BUFFER_SIZE` is NOT defined.

### `uses_conditional.hpp`
- Includes `cleans_up.hpp` (which undefines TEMP_BUFFER_SIZE)
- Uses `#ifndef TEMP_BUFFER_SIZE` to conditionally include `should_not_see_macro.hpp`
- **BUG**: With the preprocessing cache bug, TEMP_BUFFER_SIZE is still defined!

### `main.cpp`
Entry point that includes `uses_conditional.hpp`.

## Dependency Graph

**Expected (correct):**
```
main.cpp
  └─> uses_conditional.hpp
        ├─> cleans_up.hpp
        │     └─> defines_macro.hpp (defines TEMP_BUFFER_SIZE)
        │         [#undef TEMP_BUFFER_SIZE executed]
        └─> should_not_see_macro.hpp (included via #ifndef TEMP_BUFFER_SIZE)
              └─> PKG-CONFIG=leaked-macro-pkg extracted
```

**Buggy behavior:**
```
main.cpp
  └─> uses_conditional.hpp
        ├─> cleans_up.hpp
        │     └─> defines_macro.hpp (defines TEMP_BUFFER_SIZE)
        │         [#undef TEMP_BUFFER_SIZE executed BUT IGNORED]
        └─> should_not_see_macro.hpp NOT INCLUDED (#ifndef fails)
              └─> PKG-CONFIG=leaked-macro-pkg NOT extracted
```

## How to Reproduce

```bash
cd /home/gericksson/compiletools
source /home/gericksson/.venv312/bin/activate

# Run hunter to see header dependencies
ct-hunter main.cpp

# Expected output with fix:
# Headers: defines_macro.hpp, cleans_up.hpp, uses_conditional.hpp, should_not_see_macro.hpp

# Buggy output:
# Headers: defines_macro.hpp, cleans_up.hpp, uses_conditional.hpp
# (missing should_not_see_macro.hpp)

# Check magic flags
ct-hunter --file-list main.cpp | grep PKG-CONFIG

# Expected: PKG-CONFIG=leaked-macro-pkg
# Buggy: (empty - no PKG-CONFIG found)
```

## Technical Details

### Root Cause in preprocessing_cache.py

**Lines 442-451 (cache miss path):**
```python
new_variable_macros = {}
for k, v in preprocessor.macros.items():
    if k not in input_macros.core:
        new_variable_macros[k] = v

updated_macro_state = input_macros.with_updates(new_variable_macros)
```

The `with_updates()` method does:
```python
updated_variable = self.variable.copy()  # Copies input macros
updated_variable.update(new_macros)      # Merges preprocessor results
```

**The Problem:**
- `preprocessor.macros` correctly has TEMP_BUFFER_SIZE removed (after #undef)
- `new_variable_macros` is empty (no new defines)
- `with_updates({})` copies `input_macros.variable` which still has TEMP_BUFFER_SIZE
- Result: TEMP_BUFFER_SIZE persists even though it was undefined!

**Lines 380-386 (cache hit path - introduced by optimization):**
```python
if cached_result.active_defines:
    defines_dict = {d['name']: d['value'] for d in cached_result.active_defines}
    updated_macros = input_macros.with_updates(defines_dict)
```

Same issue - only applies `#define` operations, ignoring `#undef`.

### The Fix

Replace merging with replacement:
```python
# Instead of: updated_macro_state = input_macros.with_updates(new_variable_macros)
# Use: updated_macro_state = MacroState(input_macros.core, new_variable_macros)
```

This replaces the variable macros entirely with the preprocessor's final state, which correctly reflects both #defines and #undefs.

## Test Coverage

This bug is caught by:
- `test_preprocessing_cache.py::test_invariant_cache_honors_undef` (existing test)
- This sample provides an integration test showing real-world impact

## Impact

This is a **critical correctness bug** that causes:
1. Wrong conditional compilation paths
2. Missing dependencies in header analysis
3. Incorrect PKG-CONFIG flag extraction
4. Silent build errors (wrong sources compiled)
5. Namespace pollution (macros meant to be cleaned up persist)

Common in real C++ codebases where headers define temporary macros and clean them up with #undef to avoid polluting the global namespace.
