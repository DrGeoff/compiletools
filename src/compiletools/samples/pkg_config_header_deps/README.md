# PKG-CONFIG Header Dependencies Sample

## Purpose
This sample demonstrates and tests that PKG-CONFIG directives are properly extracted from headers included after macro definitions, and that caches are properly invalidated when header content changes.

## Test Scenarios

### Scenario 1: PKG-CONFIG Extraction from Nested Headers
Tests that when a source file includes a header that:
1. Includes another header defining macros
2. Then includes a header with PKG-CONFIG directives

The PKG-CONFIG directives are properly extracted and processed.

### Scenario 2: Cache Invalidation
Tests that when a header file's PKG-CONFIG directive is modified:
1. The cache is properly invalidated
2. The new PKG-CONFIG package is detected
3. The old PKG-CONFIG package is no longer present

## File Structure

### `libs/header_with_macros.hpp`
Defines macros similar to `hash_map.hpp` in production code.
- **Purpose**: Provides macro definitions that might affect subsequent includes
- **Macros defined**: `MY_MACRO_1`, `MY_MACRO_2`
- **Key characteristic**: Represents headers that define types/macros used by other headers

### `libs/header_with_pkgconfig.hpp`
Contains a PKG-CONFIG magic flag directive.
- **Contains**: `//#PKG-CONFIG=testpkg1` magic flag
- **Purpose**: Declares `testpkg1_function()` and requires testpkg1 package
- **Key characteristic**: This header is included AFTER `header_with_macros.hpp`
- **Cache test**: Modified to use `testpkg2` in cache invalidation test

### `libs/main_header.hpp`
Aggregator header that includes both macro and PKG-CONFIG headers.
- **Includes**:
  1. `header_with_macros.hpp` (macro definitions first)
  2. `header_with_pkgconfig.hpp` (PKG-CONFIG directive second)
- **Purpose**: Mimics pattern in production code where headers include dependencies in specific order
- **Key characteristic**: Order matters - macros before PKG-CONFIG

### `src/test.cpp`
Source file that includes the main header using a relative path.
- **Includes**: `../libs/main_header.hpp` (relative path from src/ to libs/)
- **Purpose**: Entry point for testing; demonstrates cross-directory includes
- **Key characteristic**: Uses relative path to test include path handling

### `pkgconfig/testpkg1.pc`
First fake pkg-config package.
- **Cflags**: `-I/usr/local/include/testpkg1 -DTEST_PKG1_ENABLED`
- **Purpose**: Tests initial PKG-CONFIG extraction

### `pkgconfig/testpkg2.pc`
Second fake pkg-config package.
- **Cflags**: `-I/usr/local/include/testpkg2 -DTEST_PKG2_ENABLED`
- **Purpose**: Tests cache invalidation when PKG-CONFIG directive changes

## Dependency Graph

```
src/test.cpp
  └─> libs/main_header.hpp
        ├─> libs/header_with_macros.hpp (defines MY_MACRO_1, MY_MACRO_2)
        └─> libs/header_with_pkgconfig.hpp (requires testpkg1)
              └─> testpkg1 (PKG-CONFIG)
```

**Processing order**:
1. Parse `test.cpp`
2. Find and process `main_header.hpp`
3. Process `header_with_macros.hpp` (extract macro definitions)
4. Process `header_with_pkgconfig.hpp` (extract PKG-CONFIG directive)
5. Resolve testpkg1 via pkg-config
6. Add testpkg1 Cflags to compile flags

## Expected Behavior

### Test 1: PKG-CONFIG Extraction
**Validates**: PKG-CONFIG directives in nested headers are extracted

**Steps**:
1. Create compiletools objects with `--magic=direct` and `--headerdeps=direct`
2. Parse `test.cpp`
3. Extract magic flags

**Assertions**:
- `PKG-CONFIG` key exists in flags
- `'testpkg1'` in PKG-CONFIG list
- testpkg1 Cflags present in CPPFLAGS (either `-I/usr/local/include/testpkg1` or `-DTEST_PKG1_ENABLED`)

**Why it matters**: Ensures that PKG-CONFIG directives work correctly even when:
- Header is not directly included by source file
- Header comes after macro-defining headers
- Headers are in different directories

### Test 2: Cache Invalidation
**Validates**: Cache is invalidated when header content changes

**Steps**:
1. First pass: Parse `test.cpp`, extract flags, verify testpkg1
2. Clear global hash registry and magicflags cache
3. Modify `header_with_pkgconfig.hpp` to use testpkg2 instead of testpkg1
4. Second pass: Parse `test.cpp` again, extract flags

**Assertions**:
- First pass: `'testpkg1'` in PKG-CONFIG list
- Second pass: `'testpkg2'` in PKG-CONFIG list
- Second pass: `'testpkg1'` NOT in PKG-CONFIG list

**Why it matters**: Tests the bugfix where:
- `global_hash_registry.clear_global_registry()` clears `_HASHES` and `_REVERSE_HASHES`
- But forgot to clear the LRU cache on `get_file_hash()`
- Caused `get_file_hash()` to return cached hashes that weren't in `_REVERSE_HASHES`
- Led to `FileNotFoundError` in `get_filepath_by_hash()`

## Directory Structure

This sample uses a two-tier directory structure:
```
pkg_config_header_deps/
├── src/           # Source files (.cpp)
│   └── test.cpp
├── libs/          # Header files (.hpp)
│   ├── header_with_macros.hpp
│   ├── header_with_pkgconfig.hpp
│   └── main_header.hpp
└── pkgconfig/     # Fake pkg-config files
    ├── testpkg1.pc
    └── testpkg2.pc
```

This structure tests:
- Cross-directory includes (`#include "../libs/..."`)
- Include path management (`--INCLUDE=` flags)
- File path resolution in different directories

## Related Tests

- **Test File**: `src/compiletools/test_pkg_config_header_deps.py`
  - **Test Method 1**: `test_pkgconfig_extracted_from_nested_header()`
    - Validates PKG-CONFIG extraction from nested headers
  - **Test Method 2**: `test_cache_invalidation_after_header_change()`
    - Validates cache invalidation when header changes

## Usage in Tests

Tests copy this sample directory to a temporary location before running:
```python
from compiletools.testhelper import samplesdir
sample_src = Path(samplesdir()) / "pkg_config_header_deps"

# Copy entire directory structure
shutil.copytree(sample_src / "src", self.src_dir)
shutil.copytree(sample_src / "libs", self.libs_dir)
shutil.copytree(sample_src / "pkgconfig", self.pkgconfig_dir)
```

**Why copy instead of direct reference?**
- Cache invalidation test modifies `header_with_pkgconfig.hpp` during execution
- Copying to temp ensures test isolation
- Each test run gets a clean copy of the files
- Modifications don't affect the version-controlled samples

## Key Characteristics

1. **Two-tier directory structure**: Tests cross-directory includes and path resolution
2. **Multiple pkg-config packages**: Tests both extraction and cache invalidation
3. **Order-dependent includes**: Macros first, PKG-CONFIG second
4. **File modification during test**: Cache invalidation test changes header content
5. **Relative path includes**: `test.cpp` uses `../libs/` to include headers

## Regression Prevention

This sample prevents regression of:

1. **PKG-CONFIG extraction failure**: Ensures PKG-CONFIG directives in nested headers are always found
2. **Cache staleness bug**: Ensures cache is properly cleared when headers change
3. **Hash registry bug**: Prevents `get_file_hash()` from returning unregistered hashes
4. **Multi-directory handling**: Ensures compiletools correctly handles src/ and libs/ structure

## Related Documentation

- **Test implementation**: `src/compiletools/test_pkg_config_header_deps.py`
- **Global hash registry**: `src/compiletools/global_hash_registry.py`
- **Magic flags processing**: `src/compiletools/magicflags.py`
