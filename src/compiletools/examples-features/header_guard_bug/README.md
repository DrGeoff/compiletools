# Accurate Header Guard Detection

## Overview

This sample demonstrates a bug in ct-magicflags where non-standard header guards
cause Pass 2 to skip transitive dependencies, resulting in missing magic flags.

## The Bug

### Pass 1: Initial Discovery
- Processes main.cpp → header_a.hpp → header_b.hpp
- Discovers all dependencies (2 headers)
- Macro convergence defines HEADER_A_HPP_GUARD (non-standard pattern prevents detection)

### Pass 2: Re-discovery with Converged Macros
- FileAnalyzer fails to detect HEADER_A_HPP_GUARD as a header guard
- Guard macro enters Pass 2 macro state
- Preprocessor evaluates `#ifndef HEADER_A_HPP_GUARD` as FALSE
- Skips entire header_a.hpp contents (including `#include "header_b.hpp"`)
- Only discovers 1 header instead of 2

### Result
- header_b.hpp not rediscovered in Pass 2
- Magic flags lost: `//#PKG-CONFIG=zlib`, `//#LDFLAGS=-lm`
- Game engine missing critical dependencies

## Non-Standard Guard Pattern

header_a.hpp uses a guard pattern that breaks FileAnalyzer detection:

```cpp
#ifndef HEADER_A_HPP_GUARD
#define SOME_OTHER_MACRO 1  // This breaks detection!
#define HEADER_A_HPP_GUARD
```

FileAnalyzer expects `#define HEADER_A_HPP_GUARD` immediately after `#ifndef`,
but the intermediate `#define SOME_OTHER_MACRO 1` causes it to miss the guard.

## Testing

```bash
cd /home/gericksson/compiletools
pytest src/compiletools/test_magicflags.py::TestMagicFlagsModule::test_header_guard_bug_transitive_magic_flags -v
```

**Before fix**: Test FAILS (weapon dependencies missing)
**After fix**: Test PASSES (all dependencies discovered)

## Fix

**File**: `/home/gericksson/compiletools/src/compiletools/file_analyzer.py`

1. **Line ~598**: Sort directives by line number before guard detection
2. **Line ~930-939**: Look ahead 5 positions for matching #define (not just next)

This allows FileAnalyzer to correctly detect non-standard guard patterns.
