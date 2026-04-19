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
   - `magicflags.py` extracts build flags from `//#` comment annotations (`CPPFLAGS=`, `PKG-CONFIG=`, `SOURCE=`, `LDFLAGS=`, `PCH=`, etc.)
   - Implied sources are discovered (e.g., `foo.h` implies `foo.cpp`)

4. **LDFLAGS Merging** -- `utils.merge_ldflags_with_topo_sort()` merges per-file `-l` flags via topological sort. Edges are classified as **soft** (from single-package `PKG-CONFIG` or plain `LDFLAGS` -- cancelled when two files disagree) or **hard** (from multi-package `PKG-CONFIG=a b` annotations -- always kept). Hard orderings are computed inside `magicflags._handle_pkg_config()` from the same macro-expanded `libs_list` that produces the soft-constraint LDFLAGS (stored under the `_HARD_ORDERINGS_KEY` sentinel in the flags dict, consumed by `build_backend._merge_ldflags_for_sources()`). Genuine cycles after cancellation raise `ValueError` with a diagnostic showing the cycle path and contributing source files.

5. **Backend Dispatch** -- `cake.py:_call_backend()` uses the `--backend` flag (default: `make`) to select a build backend via the registry in `build_backend.py`. The backend calls `build_graph()` to populate a `BuildGraph` (backend-agnostic IR of `BuildRule` objects) from Hunter/Namer data, then `generate()` to write the native build file. Available backends: make (default), ninja, cmake, bazel, shake, tup. `compilation_database.py` generates `compile_commands.json` independently.

6. **Build Execution** -- `backend.execute("build")` invokes the native build tool (make, ninja, cmake --build, etc.). If tests exist, `backend.execute("runtests")` runs them (most backends delegate to the shared `BuildBackend._run_tests()` which runs test executables directly). Finally `_copyexes()` copies executables to the output directory.

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

`locking.py` provides four locking strategies auto-selected by filesystem type: `FcntlLock` (GPFS: `fcntl.lockf()`, cross-node, kernel-managed blocking), `LockdirLock` (NFS/Lustre: atomic `mkdir`, stale detection via `{hostname}:{pid}`), `CIFSLock` (CIFS/SMB: exclusive file creation), and `FlockLock` (local: POSIX `flock`). Polling intervals auto-detected (Lustre: 0.01s, NFS: 0.1s). `cleanup_locks.py` removes stale lockdirs (process liveness via psutil/SSH) and unheld fcntl lock files (non-blocking `lockf()` probe).

### Precompiled Header (PCH) Caching

`compiletools` supports content-addressable PCH caching via `--pchdir`. Headers marked with the `//#PCH=` magic flag are compiled into `.gch` files and cached in `{git_root}/shared-pchdir/{variant}` (or custom path via `--pchdir`). The cache key includes compiler, flags, and header path, preventing collisions across builds. PCH files are atomically created and cross-user safe. Enable `--pchdir` in `ct.conf.d/ct.conf` for automatic per-variant caching, or pass `--pchdir=<path>` at the CLI. Use `ct-trim-cache --pchdir-only` to selectively clean aged PCH entries while preserving active builds. Falls back to legacy `.gch` placement in the object directory if `--pchdir` is unset.

### Key Modules

| Module | Role |
|--------|------|
| `cake.py` | Build orchestration (`Cake.process()`) |
| `configutils.py` | Hierarchical config with variant system |
| `magicflags.py` | Extract compiler flags from `//#` annotations (factory pattern); handles macro expansion in flag values |
| `file_analyzer.py` | SIMD-optimized source scanning via StringZilla |
| `preprocessing_cache.py` | Unified cache with `MacroState` tracking |
| `simple_preprocessor.py` | C preprocessor for conditional compilation |
| `headerdeps.py` | Header dependency analysis (factory: Direct or Cpp) |
| `hunter.py` | Recursive dependency graph walking |
| `findtargets.py` | Executable/test target detection |
| `namer.py` | File naming, object paths (includes macro state hash) |
| `makefile.py` | Makefile generation (`MakefileCreator`) |
| `build_graph.py` | Backend-agnostic IR (`BuildRule`, `BuildGraph`) |
| `build_backend.py` | `BuildBackend` ABC, registry, `build_graph()`, `_run_tests()`; PCH handling and hard orderings consolidation |
| `build_context.py` | Per-build state and caches; tracks pkg-config overrides and build variants |
| `build_timer.py` | Build timing instrumentation and reporting |
| `makefile_backend.py` | Make backend (wraps `MakefileCreator`) |
| `ninja_backend.py` | Ninja backend (with file-locking support) |
| `bazel_backend.py` | Bazel backend |
| `cmake_backend.py` | CMake backend |
| `trace_backend.py` | Shake + Slurm backends (self-executing, verifying traces; both classes here) |
| `tup_backend.py` | Tup backend |
| `compilation_database.py` | `compile_commands.json` generation |
| `locking.py` | Cross-platform atomic file locking |
| `stringzilla_utils.py` | SIMD text operation helpers |
| `global_hash_registry.py` | Content-addressable file hashing |
| `trim_cache.py` | Cache trimming utility for object and PCH directories with configurable retention |

### Configuration Files

- `ct.conf.d/ct.conf` -- default variant, variant aliases, exe/test markers, file-locking settings
- `ct.conf.d/{variant}.conf` -- compiler-specific flags (e.g., `gcc.debug.conf`, `clang.release.conf`)
- Config priority: bundled < system (`/etc/xdg/ct`) < venv < user (`~/.config/ct`) < project (`{gitroot}/ct.conf.d/`) < cwd < env < CLI

### Command-Line Tools

Python entry points defined in `pyproject.toml [project.scripts]`. Shell wrappers in `scripts/` (ct-build, ct-release, ct-watch-build, profile-ct).

All tools support `--variant=<config>` for build configuration selection and `--backend=<name>` for build system backend selection (make, ninja, cmake, bazel, shake, tup).

## Test Conventions

- Prefer function-based tests (`def test_something():`) for simple cases
- Use class-based tests with `BaseCompileToolsTestCase` from `test_base.py` for tests needing cache isolation (it clears all module-level caches in setup/teardown)
- `testhelper.py` provides `TempDirContext`, `create_temp_config()`, `samplesdir()`, `@requires_functional_compiler`
- `conftest.py` has session-wide `ensure_lock_helper_in_path` fixture and function-scoped `pkgconfig_env` fixture
- Sample projects in `src/compiletools/samples/` cover specific test scenarios (conditional_includes, macro_deps, cross_platform, etc.)
- Never hardcode compiler names (`gcc`, `g++`) in tests that invoke compilation — use `@requires_functional_compiler` decorator and `apptools.get_functional_cxx_compiler()` to detect the system compiler
- Use `monkeypatch.chdir()` instead of `os.chdir()` with try/finally in tests — pytest auto-restores the working directory
- When removing/renaming methods, search tests for `patch.object(...)` mocks referencing the old name
- `BuildRule.rule_type` is validated against `VALID_RULE_TYPES` in `build_graph.py` — adding a new rule type requires updating the frozenset

