# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

compiletools is a Python package providing C/C++ build tools that require minimal configuration. The core philosophy is "magic" -- automatic dependency detection and build configuration through intelligent source code analysis. The code must run under Python 3.10+.

## Worktree-Based Development

This repository uses git worktrees for development. The `master` worktree lives at `compiletools/master/`. Feature branches are checked out as sibling worktrees under `compiletools/` (e.g., `git worktree add ../my-feature my-feature`).

## Build and Test Commands

```bash
# Ensure a Python 3.10+ venv is active, or create one with:
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

`MacroState` in `preprocessing_cache.py` separates static compiler built-ins (~388 macros, immutable) from dynamic file `#define`s (variable). Cache keys only hash variable macros, giving ~80% reduction in key computation cost. Lazy frozenset computation with incremental updates for pure additions (common case). The build-context portion of the hash also folds in `compiler_identity(args.CXX)` — a `realpath|size|mtime_ns` triple resolved via `apptools.compiler_identity` and exposed on `args.flags.compiler_identity` — so an in-place toolchain swap that leaves `args.CXX` unchanged still invalidates per-TU object cache entries (symmetric with the PCH cache key).

### Compile-Flag State (`args.flags`)

Once `apptools.parseargs` returns, the canonical view of compile flags lives on `args.flags` — a frozen `Flags` dataclass (`flags.py`) with `cpp` / `c` / `cxx` / `ld` slots as `tuple[str, ...]` plus `compiler_identity`. New consumers should read these instead of re-tokenizing `args.CPPFLAGS`. Convenience methods: `hash_relevant(slot)` returns the slot's tokens with `-D`/`-U` and diagnostic-only flags removed (used by cache-key hashing); `existing_include_paths(slot)` and `append_include(path, slots=...)` form the include-path dedup API used by `apptools._add_include_paths_to_flags`. Because `Flags` is frozen and tuple-backed, instances are hashable and consumers cannot mutate the underlying tokens — `append_include` returns a new `Flags` via `dataclasses.replace`.

**Mutation invariant.** `args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}` raw strings, the sibling `args.{*}_tokens` lists, and `args.flags` are populated once at the end of `parseargs` and must not be mutated afterwards — `args.flags` would silently drift from the raw strings. All known mutation sites (`substitutions`, `_add_include_paths_to_flags`, project version macros, pkg-config, CPP/CXX unification) run BEFORE that point. `apptools.check_flag_string_drift(args)` compares the current raw flag strings against the snapshot recorded at parseargs end (`args._flag_string_snapshot`) and raises `RuntimeError` naming the offending slot if anything has changed; call it from any consumer that wants to assert the invariant before reading `args.flags`.

### Content-Addressable Deduplication

`global_hash_registry.py` loads all file hashes in a single call at startup. `FileAnalyzer` caches results by content hash (not path), so identical files are analyzed once regardless of location.

### Locking System

`locking.py` provides four locking strategies auto-selected by filesystem type: `FcntlLock` (GPFS: `fcntl.lockf()`, cross-node, kernel-managed blocking), `LockdirLock` (NFS/Lustre: atomic `mkdir`, stale detection via `{hostname}:{pid}:{start_time}` — start_time guards against PID-reuse on busy hosts), `CIFSLock` (CIFS/SMB: exclusive file creation), and `FlockLock` (local: POSIX `flock`). Polling intervals auto-detected (Lustre: 0.01s, NFS: 0.1s). `cleanup_locks.py` removes stale lockdirs only (kernel-managed `fcntl`/`flock` locks need no manual cleanup — they release automatically on process death). `atomic_compile()` and `atomic_link()` run children in a new session and forward SIGINT/SIGTERM to the child's process group, so the lock is never released while a build child is still writing the target.

**Two invariants for concurrent peer-`make` safety on the object CAS** (violating either one produces sporadic `undefined reference to 'main'` from `crt1.o:_start` — most easily reproduced under PyPy, where slower interpreter timing widens the window):

1. **Lock a sidecar, never the build target.** All four strategies lock `<target>.lock` (or `<target>.lockdir` for `LockdirLock`). Never lock `<target>` directly: `os.open(target, O_CREAT|O_RDWR)` creates an empty target file with `mtime=now` *before* the inner compile runs. A concurrent peer `make` then statting the target sees `mtime(target) > mtime(source)` and treats it as up-to-date — skipping the compile recipe entirely and linking an empty `.o`. The sidecar is invisible to make's dependency graph, so its creation can't be misread as a build artifact.
2. **Producer-side temp+rename.** The compiler writes to `<target>.compiletools.tmp` and `mv -f`s into place. Link rules read `.o` files without acquiring any lock (read-side locks would either explode the process count or invert the lock-acquisition order and deadlock); the rename guarantees a peer linker mmap-reading the target sees either the previous good `.o` (old inode) or the new one (new inode) — never partial bytes.

Both invariants must be maintained in every code path that emits compile/link commands: the native-`flock` fastpaths in `wrap_compile_with_lock`/`wrap_link_with_lock` (used by Make and Ninja makefiles), and the in-Python `atomic_compile`/`atomic_link` callers (used by the Shake/Slurm backends via `trace_backend.py`). `trim_cache._safe_locked_rmtree` filters `*.lock`/`*.lock.excl` from both the lock list and its TOCTOU re-scan — sidecars created by its own lock acquisitions would otherwise be misread as peer build activity.

### Variant-Aware Compilation Database

`ct-cake` and `ct-compilation-database` write per-variant databases as `<gitroot>/compile_commands.<variant>.json` (e.g. `compile_commands.gcc.debug.json`) and atomically retarget a sibling `compile_commands.json` symlink at whichever variant most recently completed. The bare name is what clangd / clang-tidy / VSCode actually open — the [JSON CDB spec](https://clang.llvm.org/docs/JSONCompilationDatabase.html) allows multiple entries per source file, but consumers (clangd in particular) pick one and ignore the rest, so multi-variant must live in separate files plus a switcher. Symlink targets are written as a relative basename so the tree is portable across renames/copies. If the user passes `--compilation-database-output=<path>` the literal path is honored verbatim and no symlink is touched (preserves backward-compat for scripts that pin a specific filename). To switch the active variant for tooling, run a build with `--variant=<other>` (or `VARIANT=<other>`); the symlink follows the most recent build.

### Precompiled Header (PCH) Caching

`compiletools` supports content-addressable PCH caching via `--cas-pchdir`. Headers marked with the `//#PCH=` magic flag are compiled into `.gch` files and cached in `{git_root}/cas-pchdir/{variant}` (or custom path via `--cas-pchdir`). The cache key includes compiler, flags, and header path, preventing collisions across builds. PCH files are atomically created and cross-user safe. Enable `--cas-pchdir` in `ct.conf.d/ct.conf` for automatic per-variant caching, or pass `--cas-pchdir=<path>` at the CLI. Each cache entry writes a sidecar `manifest.json` (header realpath + transitive-header content hashes); `ct-trim-cache --cas-pchdir-only` reads those manifests to bucket entries by real header (so cross-variant builds don't evict each other) and to pre-evict entries whose transitive headers have changed (avoiding the slow `cc1` PCH-stamp rejection at consume time). Falls back to legacy `.gch` placement in the object directory if `--cas-pchdir` is unset.

**Naming history.** Prior to this rename, the object CAS was called `shared-objdir/` (default `{git_root}/shared-objdir/{variant}/`) and the PCH CAS was `shared-pchdir/`, with CLI flags `--objdir` / `--pchdir`. The "shared object" overload conflicted with Linux `.so`. There is no backward-compat alias: existing `ct.conf` files setting `objdir` / `pchdir` must rename those keys to `cas-objdir` / `cas-pchdir`, and on-disk `shared-objdir/` directories from earlier builds are no longer consulted (safe to delete).

### C++20 Modules Caching (cas-pcmdir)

`compiletools` caches BMI artefacts (clang `.pcm`, gcc `.gcm`) at `{git_root}/cas-pcmdir/{variant}/{command_hash}/<name>.{pcm,gcm}` with a sidecar `manifest.json` (`bucket_key` + `stage` + `transitive_hashes`). Mirrors `cas-pchdir`'s shape and trim semantics; `ct-trim-cache --cas-pcmdir-only` works the same way. For gcc, BMI placement is steered via a per-makefile mapper file at `{dirname(makefilename)}/.module-mapper.txt` (falling back to `{cas-objdir}/.module-mapper.txt` when the makefile path has no dirname component, e.g., test fixtures using a bare `Makefile` filename). Per-makefile placement isolates concurrent `ct-cake` invocations targeting different makefiles from each other.

**Discovery flow.** `FileAnalyzer` scans every source for module declarations (`export module M(:P);`, `module M(:P);`, `import M(:P);`, `import :P;`, `import <h>;`, `import "h";`). Hunter builds a `module_name → first-exporter-path` registry from the global hash registry (with an `os.walk` fallback for non-git tmp dirs and any explicit `--include` directories). Multiple files exporting the same name are tolerated at registry-build time (warning at `verbose ≥ 1`); the hard error fires at lookup time with the importer's path in the diagnostic. System modules (`std`) fall back to compiler-shipped sources: `<gcc-include>/c++/<ver>/bits/std.cc` for gcc, `<clang-install>/share/libc++/v1/std.cppm` for clang.

**Why single command_hash + manifest, not the object cache's 3-axis path?** The object cache uses `{basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o` (~168 bits in the path) because there is **no in-band verification** of `.o` content at link time — the linker happily links the bytes it's handed. A hash collision would therefore cause a silent miscompile, so the path needs the entropy of multiple independent hashes to make collisions statistically impossible. PCH and PCM artefacts are different: **the compiler verifies BMI compatibility at consume time** (GCC's PCH stamp, clang's BMI signature). The compile environment (compiler version, language standard, ABI flags, target triple, etc.) is recorded inside the BMI itself; consume time loads it and compares against the consumer's environment, rejecting on mismatch. A hypothetical 64-bit collision on the single command_hash therefore degrades to a slow re-precompile, never a miscompile. PCH had this single-hash + manifest design from day one; PCM matches it. An earlier exploration of refactoring PCM to the object cache's 3-axis layout was reverted because it added complexity without addressing a safety problem PCM doesn't have. See the `_pcm_command_hash` and `_pch_command_hash` docstrings for the full reasoning, and `README.ct-cake.rst`'s "C++20 Modules Caching" section for the user-facing version of this argument.

**Per-compiler details.** clang writes `.pcm` files directly to the cache path via `--precompile -o <pcm_path>`; importers reference each cached `.pcm` via `-fmodule-file=<name>=<pcm_path>`. The token form (e.g. `<vector>`) is shell-quoted at flag emission to keep `<`/`>` from being interpreted as redirection. gcc reads/writes `.gcm` files at the cache path via `-fmodule-mapper=<mapper-path>`; the mapper file maps module names (or, for header units, the resolved absolute system-header path obtained from `g++ -M -x c++ -`) to cache paths. The cache key (`command_hash`) folds in: `compiler_identity` (realpath + size + mtime_ns of the resolved binary), hash-relevant `cxxflags_tokens`, per-file magic flags, compiler-injected extras (`-stdlib=libc++`, `-Wno-reserved-module-identifier`), source path, transitive content hash (source content + `compute_dep_hash` of headers), and a `stage` marker (`clang_module_interface` / `clang_header_unit` / `gcc_module_interface` / `gcc_header_unit`).

**Filename escapes.** Module partition separators (`:`) and header-unit path separators (`/`) are escaped to `^^` (`_NAME_ESCAPE` in `build_backend.py`) for the on-disk filename — `_` and `-` would collide with characters that legitimately appear in identifiers and header names. The compiler-facing mapper key keeps the original `:` so `-fmodule-mapper` lookups for partitions resolve correctly.

### Key Modules

| Module | Role |
|--------|------|
| `cake.py` | Build orchestration (`Cake.process()`) |
| `configutils.py` | Hierarchical config with variant system |
| `magicflags.py` | Extract compiler flags from `//#` annotations (factory pattern); handles macro expansion in flag values |
| `flags.py` | Frozen `Flags` dataclass: structured view of `args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}` as immutable tuples + `compiler_identity`. Centralizes hash-relevance filtering (`hash_relevant`), -I dedup (`existing_include_paths` / `append_include`). Built once per parseargs call as `args.flags`. |
| `file_analyzer.py` | SIMD-optimized source scanning via StringZilla |
| `preprocessing_cache.py` | Unified cache with `MacroState` tracking |
| `simple_preprocessor.py` | C preprocessor for conditional compilation |
| `headerdeps.py` | Header dependency analysis (factory: Direct or Cpp) |
| `hunter.py` | Recursive dependency graph walking |
| `findtargets.py` | Executable/test target detection |
| `namer.py` | File naming, object paths (includes macro state hash) |
| `build_graph.py` | Backend-agnostic IR (`BuildRule`, `BuildGraph`) |
| `build_backend.py` | `BuildBackend` ABC, registry, `build_graph()`, `_run_tests()`; PCH handling and hard orderings consolidation |
| `build_context.py` | Per-build state and caches; tracks pkg-config overrides and build variants |
| `build_timer.py` | Build timing instrumentation and reporting |
| `makefile_backend.py` | Make backend; `MakefileBackend` class plus the `ct-create-makefile` CLI entry point |
| `ninja_backend.py` | Ninja backend (with file-locking support) |
| `bazel_backend.py` | Bazel backend |
| `cmake_backend.py` | CMake backend |
| `trace_backend.py` | Shake + Slurm backends (self-executing, verifying traces; both classes here) |
| `tup_backend.py` | Tup backend |
| `compilation_database.py` | `compile_commands.<variant>.json` generation + `compile_commands.json` symlink |
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

