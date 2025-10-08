# Hunter Macro Propagation Bug Test

This directory contains test files that expose a bug in the Hunter dependency analysis where file-level `#define` macros are not properly propagated when analyzing header dependencies.

## The Issue

When `Hunter._get_immediate_deps()` analyzes a header file as a dependency, it passes a `macro_state_key` parameter to track which macros should be defined. However, when Hunter calls `headerdeps.process()`, the headerdeps always resets macros to core-only (empty variable macros) via `_initialize_includes_and_macros()`, completely ignoring the `macro_state_key`.

This causes headers to be preprocessed without file-level `#define` directives from parent source files, leading to incorrect conditional compilation evaluation.

## Test Files

- `app.cpp` - Source file that defines `ENABLE_RENDERING` macro before including `config.h`
- `config.h` - Header with conditional include: `#ifdef ENABLE_RENDERING #include "renderer.h" #endif`
- `renderer.h` - Header that should only be included when `ENABLE_RENDERING` is defined

## Expected Behavior

When Hunter analyzes `app.cpp`:
1. Parses `app.cpp`, discovers it defines `ENABLE_RENDERING`
2. Includes this macro in the `macro_state_key`
3. When analyzing `config.h` as a dependency, should use this macro state
4. Should find `renderer.h` as a dependency via the conditional include

## The Bug

Currently:
1. Hunter gets `macro_state_key` with `ENABLE_RENDERING` from parsing `app.cpp`
2. Hunter calls `headerdeps.process('config.h')` to get its dependencies
3. `headerdeps.process()` resets macros to empty, ignoring the `macro_state_key`
4. `config.h` is preprocessed without `ENABLE_RENDERING` defined
5. The conditional `#ifdef ENABLE_RENDERING` evaluates to False
6. `renderer.h` is NOT discovered as a dependency

## Testing

Run the pytest test to verify the bug is exposed:

```bash
pytest -xvs test_hunter_macro_propagation.py
```

Or run directly:

```bash
python test_hunter_macro_propagation.py
```
