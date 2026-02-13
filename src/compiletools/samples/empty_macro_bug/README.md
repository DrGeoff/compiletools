# Empty Macro Bug Sample

## Purpose
This sample demonstrates and tests the empty macro state bug in initial header discovery. The bug occurs when `headerdeps.process()` is called with an empty macro state (`frozenset()`), causing conditionally-included headers to be missed.

## Bug Description
When the initial header discovery phase uses an empty macro state (in direct mode with SimplePreprocessor):
1. Header dependencies are processed without macros defined in previously processed files
2. Conditional includes that depend on those macros fail (e.g., `#ifdef USE_HASH`)
3. Headers are missing from the initial dependency list
4. Convergence only processes files in the initial list
5. Missing headers are **never discovered**, even after macros converge

## File Structure

### `libs/base.hpp`
Defines the `USE_HASH` macro via C++ `#define` (not a magic flag).
- **Purpose**: Provides a macro that affects conditional compilation in other headers
- **Key characteristic**: Macro is defined in C++ code, not via command-line flags

### `libs/conditional.hpp`
Conditionally includes `dependency.hpp` based on whether `USE_HASH` is defined.
- **Includes**: `base.hpp` (to get `USE_HASH` macro), then conditionally `dependency.hpp`
- **Key characteristic**: The `#ifdef USE_HASH` directive mimics how `string.hpp` conditionally includes `hash_utility.hpp` in production code

### `libs/dependency.hpp`
The header that gets missed by the bug when initial discovery uses empty macro state.
- **Contains**: `//#PKG-CONFIG=conditional` magic flag
- **Purpose**: Declares `dependency_function()` and requires conditional package
- **Key characteristic**: This header is **only** discovered when `USE_HASH` is defined during header processing

### `libs/main.cpp`
Entry point for the test.
- **Includes**: `conditional.hpp`
- **Purpose**: Minimal main function to create a compilable source file

### Shared `samples/pkgs/conditional.pc`
Fake pkg-config file (shared across samples) for testing PKG-CONFIG magic flag extraction.
- **Cflags**: `-I/usr/local/include/testpkg -DTEST_PKG_ENABLED`
- **Purpose**: Tests that PKG-CONFIG directives from conditionally-included headers are extracted
- **Note**: Uses shared pkgs directory via `pkgconfig_env` pytest fixture

## Dependency Graph

```
main.cpp
  └─> conditional.hpp
        ├─> base.hpp (defines USE_HASH)
        └─> dependency.hpp (conditional on USE_HASH)
              └─> requires conditional (PKG-CONFIG)
```

**Critical path**:
- If `USE_HASH` is not defined when processing `conditional.hpp`, then `dependency.hpp` is **not included**
- This means `//#PKG-CONFIG=conditional` is never discovered
- Build will fail with missing `conditional` package flags

## Expected Behavior

### With Bug (Before Fix):
1. Initial header discovery uses empty macro state: `headerdeps.process(main.cpp, frozenset())`
2. Processing `conditional.hpp` without `USE_HASH` defined
3. `#ifdef USE_HASH` fails → `dependency.hpp` NOT included
4. Initial headers list: `[conditional.hpp, base.hpp]` (missing `dependency.hpp`)
5. Convergence only processes `[conditional.hpp, base.hpp]`
6. `dependency.hpp` is **NEVER discovered**
7. PKG-CONFIG=conditional is **NEVER extracted**
8. **Test fails**: `dependency.hpp` not found, `conditional` flags missing

### With Fix (After Two-Pass Discovery):
1. **Pass 1**: Initial discovery with core macros (compiler built-ins)
2. Converge macros with Pass 1 file set
3. **Pass 2**: Re-discover with converged macros (including `USE_HASH`)
4. Processing `conditional.hpp` with `USE_HASH=1` defined
5. `#ifdef USE_HASH` succeeds → `dependency.hpp` IS included
6. Final headers list: `[conditional.hpp, base.hpp, dependency.hpp]`
7. PKG-CONFIG=conditional IS extracted
8. **Test passes**: All headers found, all flags extracted

## Related Tests

- **Test File**: `src/compiletools/test_empty_macro_bug.py`
  - **Test Method**: `test_empty_macro_state_causes_missing_headers()`
  - **Validates**: Two-pass discovery finds conditionally-included headers with file-defined macros
  - **Assertions**:
    - `dependency.hpp` is found in header dependencies
    - `PKG-CONFIG=conditional` is extracted
    - conditional package Cflags are present in CPPFLAGS

## Usage in Tests

Tests copy the libs directory to a temporary location and use the shared `pkgs/` directory via the `pkgconfig_env` pytest fixture:
```python
from compiletools.testhelper import samplesdir
sample_src = Path(samplesdir()) / "empty_macro_bug"
shutil.copytree(sample_src / "libs", self.libs_dir)
# PKG_CONFIG_PATH is set by pkgconfig_env fixture to samples/pkgs/
```

This maintains test isolation while using version-controlled sample code.

## Key Characteristics

1. **Simple linear dependency chain**: Easy to understand and debug
2. **Single conditional include**: Focused on one specific failure mode
3. **C++ macro definition**: Tests macros defined in headers (not command-line)
4. **PKG-CONFIG in conditional header**: Tests that magic flags are extracted from conditionally-included dependencies
5. **Minimal code**: Only the essential components needed to reproduce the bug

## Related Documentation

- **Fixed Code**: `src/compiletools/magicflags.py` (two-pass discovery implementation)
