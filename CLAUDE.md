# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

compiletools is a Python package providing C/C++ build tools that require minimal configuration. The core philosophy is "magic" -- automatic dependency detection and build configuration through intelligent source code analysis. The code must run under Python 3.9+.

## Worktree-Based Development

This repository uses git worktrees for development. The `master` worktree lives at `compiletools/master/`. Feature branches are checked out as sibling worktrees under `compiletools/` (e.g., `git worktree add ../my-feature my-feature`).

## Build and Test Commands

```bash
# Ensure a Python 3.9+ venv is active, or create one with:
# uv venv && source .venv/bin/activate

# Install in development mode with dev dependencies
uv pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Run all tests
pytest

# Run tests in parallel
pytest -n auto

# Run a specific test file or test
pytest src/compiletools/test_magicflags.py
pytest src/compiletools/test_namer.py::test_executable_pathname

# Lint
ruff check src/compiletools/

# Format
ruff format src/compiletools/

# Run all pre-commit hooks against the entire codebase
pre-commit run --all-files
```

pytest configuration is in `pyproject.toml` (`[tool.pytest.ini_options]`): tests live in `src/compiletools/`, matching `test_*.py`, verbose by default.

## Architecture

### Build Flow (ct-cake --auto)

The build process in `cake.py` follows this sequence:

1. **Config Resolution** -- `configutils.py` merges config from 7+ priority levels (bundled < system < venv < user < project < cwd < env < CLI). Variant system selects compiler/optimization profiles (e.g., `gcc.debug`, `clang.release`). Variant aliases map `debug` -> `blank`, `release` -> `blank.release`.

2. **Target Discovery** -- `findtargets.py` scans for executables (files containing `main(` and similar markers from `ct.conf`) and tests (files including `unit_test.hpp`). This triggers a two-stage argument reparse -- first parse extracts the variant, discovery modifies the target list, then a second parse produces the final configuration.

3. **Dependency Analysis** -- `hunter.py` recursively walks the dependency graph. For each source file:
   - `headerdeps.py` finds `#include` dependencies (factory: `DirectHeaderDeps` uses `FileAnalyzer`, `CppHeaderDeps` uses `cpp -MM`)
   - `magicflags.py` extracts build flags from `//#` comment annotations (`CPPFLAGS=`, `PKG-CONFIG=`, `SOURCE=`, `LDFLAGS=`, etc.)
   - Implied sources are discovered (e.g., `foo.h` implies `foo.cpp`)

4. **Build Generation** -- `makefile.py` generates a Makefile with compilation rules; `compilation_database.py` generates `compile_commands.json`.

5. **Build Execution** -- Runs make with parallel jobs, then runs tests if configured.

### Magic Detection Pipeline

The performance-critical path for analyzing source files:

```
FileAnalyzer.analyze_file()          # SIMD-optimized single-pass scan (stringzilla)
    -> memory-mapped file read
    -> vectorized search for #include, //# flags, preprocessor directives
    -> pre-computed line_byte_offsets for O(log n) comment detection
    -> returns FileAnalysisResult

SimplePreprocessor.process_structured()  # Conditional compilation
    -> evaluates #if/#ifdef/#ifndef/#elif chains
    -> tracks #define/#undef for MacroState
    -> returns active lines, includes, flags, defines

PreprocessingCache                    # Two-tier caching
    -> invariant cache: content_hash only (files without conditionals, ~80% of files)
    -> variant cache: content_hash + macro_state_key (files with #if/#ifdef)
```

`MacroState` in `preprocessing_cache.py` separates static compiler built-ins (~388 macros, immutable) from dynamic file `#define`s (variable). Cache keys only hash variable macros, giving ~80% reduction in key computation cost. Lazy frozenset computation with incremental updates for pure additions (common case).

### Content-Addressable Deduplication

`global_hash_registry.py` loads all file hashes in a single call at startup. `FileAnalyzer` caches results by content hash (not path), so identical files are analyzed once regardless of location.

### Locking System

`locking.py` uses atomic `mkdir` for cross-platform/cross-NFS locks. Lock info stored as `{hostname}:{pid}`. Auto-detects filesystem type to set polling intervals (Lustre: 0.01s, NFS: 0.1s, GPFS: 0.05s, local: blocking `flock`). `cleanup_locks.py` removes stale locks with process liveness checks (local via psutil, remote via SSH).

### Key Modules

| Module | Role |
|--------|------|
| `cake.py` | Build orchestration (`Cake.process()`) |
| `configutils.py` | Hierarchical config with variant system |
| `magicflags.py` | Extract compiler flags from `//#` annotations (factory pattern) |
| `file_analyzer.py` | SIMD-optimized source scanning via StringZilla |
| `preprocessing_cache.py` | Unified cache with `MacroState` tracking |
| `simple_preprocessor.py` | C preprocessor for conditional compilation |
| `headerdeps.py` | Header dependency analysis (factory: Direct or Cpp) |
| `hunter.py` | Recursive dependency graph walking |
| `findtargets.py` | Executable/test target detection |
| `namer.py` | File naming, object paths (includes macro state hash) |
| `makefile.py` | Makefile generation |
| `compilation_database.py` | `compile_commands.json` generation |
| `locking.py` | Cross-platform atomic file locking |
| `stringzilla_utils.py` | SIMD text operation helpers |
| `global_hash_registry.py` | Content-addressable file hashing |

### Configuration Files

- `ct.conf.d/ct.conf` -- default variant, variant aliases, exe/test markers, shared-objects settings
- `ct.conf.d/{variant}.conf` -- compiler-specific flags (e.g., `gcc.debug.conf`, `clang.release.conf`)
- Config priority: bundled < system (`/etc/xdg/ct`) < venv < user (`~/.config/ct`) < project (`{gitroot}/ct.conf.d/`) < cwd < env < CLI

### Command-Line Tools

Python entry points defined in `pyproject.toml [project.scripts]`. Shell wrappers in `scripts/` (ct-build, ct-release, ct-watch-build, ct-lock-helper, profile-ct).

All tools support `--variant=<config>` for build configuration selection.

## Test Conventions

- Prefer function-based tests (`def test_something():`) for simple cases
- Use class-based tests with `BaseCompileToolsTestCase` from `test_base.py` for tests needing cache isolation (it clears all module-level caches in setup/teardown)
- `testhelper.py` provides `TempDirContext`, `create_temp_config()`, `samplesdir()`, `@requires_functional_compiler`
- `conftest.py` has session-wide `ensure_lock_helpers_in_path` fixture and function-scoped `pkgconfig_env` fixture
- Sample projects in `src/compiletools/samples/` cover specific test scenarios (conditional_includes, macro_deps, cross_platform, etc.)

## Caches and Performance Testing

Two caches to be aware of when performance testing:
1. **ccache** -- clear with `ccache -C`
2. **cake object cache** -- clear with `rm -rf bin`; with `--shared-objects`, objdir may be a shared location

## Profiling

```bash
# Profile with cProfile
python -m cProfile -s cumulative $(which ct-cake) --auto

# Use the project profiling script
scripts/profile-ct ct-compilation-database -d /path/to/project -- --include "dir1 dir2"

# System-level with py-spy
py-spy record -o profile.svg -- python ct-cake --auto
```

## Dependencies

- **stringzilla** -- SIMD-accelerated string operations (core to FileAnalyzer performance)
- **configargparse** -- unified CLI/env/config file option parsing
- **appdirs** -- XDG-compliant directory locations
- **psutil** -- process management for stale lock detection
- **rich** -- terminal formatting
