# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

compiletools is a Python package providing C/C++ build tools that require minimal configuration. The core philosophy is "magic" -- automatic dependency detection and build configuration through intelligent source code analysis. The code must run under Python 3.10+.

## Worktree-Based Development

This repository uses git worktrees for development. The `master` worktree lives at `compiletools/master/`. Feature branches are checked out as sibling worktrees under `compiletools/` (e.g., `git worktree add ../my-feature my-feature`).

**Per-worktree venvs are mandatory.** Each worktree needs its own venv with `uv pip install -e .` (or `pip install -e .`) run from inside that worktree. An editable install records the *path it was installed from* — so a venv created in `master/` will keep importing `compiletools` from `master/src/` even when you `cd` into `my-feature/` and run `pytest`. Subprocess-driven tools like `ct-cake` and the e2e test suites would silently exercise the wrong code. Run `ct-check-venv` from the worktree to verify (`compiletools/check_venv.py` — exits 0 when the `ct-cake` on PATH and the local `compiletools` resolve to the same install root, 1 with an actionable diagnostic when they don't). The pytest helper `compiletools.testhelper.skipif_e2e_unavailable` enforces this for every e2e marker so tests skip with a clear message instead of failing mysteriously.

## Build and Test Commands

```bash
# Ensure a Python 3.10+ venv is active, or create one with:
# uv venv && source .venv/bin/activate

# Install in development mode with dev dependencies (pulls in ruff,
# pyright, prek, pytest-xdist, …).
uv pip install -e ".[dev]"

# Install the git hooks (one-time, per checkout). prek consumes the
# same .pre-commit-config.yaml as `pre-commit` and is what the project
# standardises on; the reference pre-commit binary is not in [dev] but
# works against the same config if installed ad-hoc.
prek install

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

# Type-check (also runs via the pre-commit hook; pyproject.toml pins
# `[tool.pyright].venvPath = ".", venv = ".venv"` so the hook's isolated
# env still resolves worktree deps).
pyright src/compiletools/

# Run all hooks against the entire codebase (handy after a big rebase or
# before pushing).
prek run --all-files
```

pytest configuration is in `pyproject.toml` (`[tool.pytest.ini_options]`): tests live in `src/compiletools/`, matching `test_*.py`, verbose by default.

## Architecture

### Build Flow (ct-cake --auto)

The build process in `cake.py` follows this sequence:

1. **Config Resolution** -- `configutils.py` merges config from 7+ priority levels (bundled < system < venv < user < project < cwd < env < CLI). Variants are *composed* from axis conf files (one per orthogonal concern: toolchain / optimization / instrumentation). `--variant=gcc,debug,asan` (comma, dot, or whitespace separators — all equivalent) splits into atomic tokens, canonicalizes their order via `variant-canonical-order` in `ct.conf` (sorted to the dotted form `gcc.debug.asan` used in all CAS paths), and synthesizes the conf file list from `gcc.conf` + `debug.conf` + `asan.conf`. A literal `<canonical_name>.conf` anywhere in the hierarchy is an authoritative override (its `extends = ...` directive can pull in parents). Axis confs use `append-CFLAGS = ...` form so multiple axes accumulate flags additively across the configargparse layering. The `variantaliases = {...}` dict is gone; `_check_legacy_variant_config_keys` raises if any resolved `ct.conf` still defines it. Two startup-time guards (`apptools._check_resolved_compiler_available` and `_check_compiler_supports_requested_standard`) catch common misconfigurations early: (a) toolchain axis pinned a compiler that isn't on PATH; (b) `-std=c++NN` exceeds the resolved compiler's major version per `_STD_MIN_COMPILER_VERSION`. Both name the variant chain in the diagnostic so the user knows which axis to swap.

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

`MacroState` in `preprocessing_cache.py` separates static compiler built-ins (~388 macros, immutable) from dynamic file `#define`s (variable). Cache keys only hash variable macros, giving ~80% reduction in key computation cost. Lazy frozenset computation with incremental updates for pure additions (common case). The build-context portion of the hash also folds in `compiler_identity(args.CXX, anchor_root=...)` — a `realpath|size|mtime_ns` triple resolved via `apptools.compiler_identity` and exposed on `args.flags.compiler_identity` — so an in-place toolchain swap that leaves `args.CXX` unchanged still invalidates per-TU object cache entries (symmetric with the PCH cache key). The realpath segment is routed through `canonicalize_path_for_cache_key` so an in-workspace wrapper script (coverage shim, sccache/distcc wrapper) at e.g. `<gitroot>/tools/cxx-wrapper.sh` doesn't leak its per-checkout prefix into the cache key.

### Compile-Flag State (`args.flags`)

Once `apptools.parseargs` returns, the canonical view of compile flags lives on `args.flags` — a frozen `Flags` dataclass (`flags.py`) with `cpp` / `c` / `cxx` / `ld` slots as `tuple[str, ...]` plus `compiler_identity`. New consumers should read these instead of re-tokenizing `args.CPPFLAGS`. Convenience methods: `hash_relevant(slot)` returns the slot's tokens with `-D`/`-U` and diagnostic-only flags removed (used by cache-key hashing); `existing_include_paths(slot)` and `append_include(path, slots=...)` form the include-path dedup API used by `apptools._add_include_paths_to_flags`. Because `Flags` is frozen and tuple-backed, instances are hashable and consumers cannot mutate the underlying tokens — `append_include` returns a new `Flags` via `dataclasses.replace`.

**Mutation invariant.** `args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}` raw strings, the sibling `args.{*}_tokens` lists, and `args.flags` are populated once at the end of `parseargs` and must not be mutated afterwards — `args.flags` would silently drift from the raw strings. All known mutation sites (`substitutions`, `_add_include_paths_to_flags`, project version macros, pkg-config, CPP/CXX unification) run BEFORE that point. `apptools.check_flag_string_drift(args)` compares the current raw flag strings against the snapshot recorded at parseargs end (`args._flag_string_snapshot`) and raises `RuntimeError` naming the offending slot if anything has changed; call it from any consumer that wants to assert the invariant before reading `args.flags`.

### Content-Addressable Deduplication

`global_hash_registry.py` loads all file hashes in a single call at startup. `FileAnalyzer` caches results by content hash (not path), so identical files are analyzed once regardless of location.

### Path-Canonical CAS Keys

All four CAS keys (per-TU object, PCH, PCM, linker-artefact) hash *gitroot-relative* paths instead of absolute paths so caches survive moving or cloning the workspace. `apptools.canonicalize_path_for_cache_key(path, anchor_root)` rewrites `<gitroot>/foo/bar.cpp` to `<GITROOT>/foo/bar.cpp` (literal sentinel string) and `apptools.canonicalize_for_cache_key(tokens, anchor_root)` does the same for path-bearing flag tokens (`-I` `-isystem` `-iquote` `-idirafter` `-F` `-B` `-include` `-include-pch`, both attached `-I/path` and detached `-I /path` forms — plus `-Wl,opt[=val][,/abs/path,...]` and the `-Xlinker /abs/path` two-token form for linker flags). Anything outside the anchor (system headers, sibling repos), already-relative tokens, and tokens that already contain the sentinel pass through unchanged — the rewrite is idempotent. Empty `anchor_root` is the identity function (graceful no-op when gitroot can't be resolved). The compiler still receives the real absolute paths; canonicalization only touches hash inputs. `MacroState` accepts `anchor_root` at construction (threaded from `headerdeps.py` / `magicflags.py`); the PCH, PCM, and linker `_pch_command_hash` / `_pcm_command_hash` / `_link_key_hash` / `_lib_key_hash` builders call the same canonicalizer on `cxxflags_tokens` / `ldflags_tokens` and the source path before hashing. The same anchor is also applied to every binary path that participates in a cache-key field — `compiler_identity`'s realpath, the PCH/PCM `cxx_command`, the link-key `ld_argv` (including `ld_argv[0]`), the static-lib `ar_argv_prefix` (including the `ar` binary path), the shared-lib `ld_argv`, `MacroState.compiler_path`, and `trace_backend.py`'s per-action `command_hash` inputs — so an in-workspace wrapper script doesn't leak its per-checkout prefix through any of those sites. Manifest writers (`_write_pch_manifest` / `_write_pcm_manifest`) require `anchor_root` as a keyword argument to make accidental re-leaks a `TypeError` rather than silent regression; a grep-scan lint test (`test_every_production_caller_passes_anchor_root`) catches future production callers that drop the kwarg.

User-visible upshot: an identical TU built in `/scratch/run-1/repo/` and `/scratch/run-2/repo/` produces the same cache key, so the second build hits the cache instead of recompiling. Cloning a repo to a new path or renaming the worktree no longer invalidates the object/PCH/PCM/exe caches.

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

### Linker-artefact Caching (cas-exedir)

`compiletools` caches the *output* of the link step at `{git_root}/cas-exedir/{variant}/<linkkey[:2]>/<basename>_<linkkey>.<ext>` with `<ext>` ∈ `{.exe, .a, .so}`. Producer rules (`link`, `ar`, `link-shared`) write directly to the CAS path; a downstream `symlink` rule then publishes the user-facing `bin/<variant>/<name>` (or `bin/<variant>/lib<name>.{a,so}`) via `ct-cas-publish` (`cas_publish.py`) — `link()` + `rename()` for atomic publish, with a symlink fallback only on `EXDEV`. Other `OSError`s (`ENOSPC`, `EPERM`, `EROFS`) surface visibly instead of silently degrading to a symlink (which would break trim's hard-link protection by leaving `nlink == 1` on the cas entry). Two sidecars accompany each entry: `<cas-path>.manifest` (`{"source_realpath": ...}`, written best-effort after the publish so `trim_cache.trim_exedir` can bucket by source identity rather than basename) and — for test executables only — `<cas-path>.result`, an empty file touched by `_run_tests` after a successful test invocation (CAS-only mode). `trim_exedir` cleans both sidecars when it evicts the parent entry.

**Link-key hash inputs.** Beyond the linker identity, canonicalized LDFLAGS, and gitroot-canonical sorted object paths, the executable link key folds in `SOURCE_DATE_EPOCH`, `LIBRARY_PATH`, `LD_LIBRARY_PATH`, `LD_PRELOAD`, the `ar` binary identity (binutils version determines archive format), and the **canonical bindir** (anchor-relative full bindir path, not just basename — `bin/blank` and `out/blank` collide on basename alone, so the full anchored path is the disambiguator that prevents RPATH/`$ORIGIN` linker scripts from baking the wrong sibling-dir resolution into a cache hit). The static-lib (`ar`) key is simpler: just the `ar` argv prefix and the canonical object set.

**Test-rerun skip is content-addressed in CAS-only mode.** `make runtests` (and the Python-side `_run_tests` filter) decide whether to re-execute a test by consulting the `<cas-path>.result` sidecar, NOT `mtime(<bin>/<test>.result) >= mtime(<bin>/<test>)`. The mtime check is wrong here because the published `bin/<variant>/<test>` is a hard-link of the cas-exedir entry — it inherits the cached entry's original creation time, so any cas-exedir hit produces a stale-looking exe relative to a newer marker file. With the content-keyed sidecar, the rule becomes `<cas-path>.result: | bin/<variant>/<test>` (order-only on the published exe so make builds it first; no normal prereqs so existence of the marker is sufficient). User-visible upshot: two workspaces sharing a `--cas-exedir` also share test-success markers — workspace B's first `runtests` correctly skips a test workspace A already ran for the same exe content (mirrors the cas-objdir/cas-pchdir/cas-pcmdir sharing model). Tests with `$PWD`-dependent observable side effects (writing to `$PWD/test-output/...`) should set `--use-mtime=True` to opt out of the cross-workspace skip, since the marker assertion is "this exe's bytes once exited 0," not "this exe ran in this workspace." Legacy pre-fix `<bin>/<test>.result` files left over from earlier installs are harmless dead bytes — the new code never reads them.

### Mtime-vs-CAS Rebuild Mode (`--use-mtime`)

The `--use-mtime` boolean (registered in `apptools.add_cas_arguments`, called from `add_output_directory_arguments` so every backend's argparse picks it up) controls whether classical mtime semantics apply on top of the CAS layer. Default `--use-mtime=False` (CAS-only): compile, link, ar, and link-shared rules drop their sources/objects from prerequisites entirely — only PCH/BMI artefacts remain, as order-only deps for build ordering. The CAS artefact's existence on disk is the sole rebuild signal, so a fresh `git checkout` (every source has `mtime=now`) hits the cache instead of re-running the producer. `--use-mtime=True` restores legacy mtime-based behavior for interactive workflows where re-touching a source should force a rebuild.

**Backend scope.** Only the make and ninja rule emitters branch on `args.use_mtime` — they are the two backends that consume the prereq list as a literal mtime comparison, so they're the ones that can honor either mode. The cmake/bazel/tup/shake/slurm backends use their own change detection (cmake's out-of-source incremental tracking, bazel's content-addressable action cache, tup's FUSE content tracking, trace_backend's verifying traces) and a touched-but-otherwise-unchanged source is invisible to all of them — `--use-mtime=True` cannot deliver "touch to force rebuild" semantics there. `BuildBackend.__init__` checks `self._honors_use_mtime()` (overridden to `True` in MakefileBackend / NinjaBackend, default `False` everywhere else) and emits a stderr warning when the user explicitly sets `--use-mtime=True` on a backend that can't honor it, so the silent-no-op is gone.

**Caveats of CAS-only mode.** Generated headers must exist before headerdeps runs (a header produced by an earlier build step but absent at headerdep analysis time is treated as unresolved and excluded from `dep_hash`; first appearance does change `dep_list` so the second build picks up the include). `make -t` and `ninja -t restat` are inappropriate — they create empty files at target paths whose recipes have no real prerequisites in CAS-only mode, corrupting the cache. System headers (`/usr/include`) are not in the cache key, matching ccache/sccache contract; a glibc upgrade between CI runs reuses cached objects (in-place compiler swaps usually change `compiler_identity` and invalidate implicitly).

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
| `build_backend.py` | `BuildBackend` ABC, registry, `build_graph()`, `_run_tests()`; PCH handling, hard orderings consolidation, link-key hashing, `_has_native_cas_exe` dispatch |
| `cas_publish.py` | `ct-cas-publish` entry point: atomic `link()`+`rename()` publish of cas-exedir entries to user-facing bindir paths, with `EXDEV`-only symlink fallback and best-effort `<cas-path>.manifest` sidecar |
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
| `trim_cache.py` | Cache trimming utility for object, PCH, PCM, and linker-artefact (cas-exedir) directories with configurable retention; `trim_exedir` honours hard-link protection and re-stats `nlink` under the lock to close the scan-to-unlink TOCTOU |
| `cache_report.py` | `ct-cache-report` entry point: read-only diagnostic that walks the four CAS layers (`cas-objdir` / `cas-pchdir` / `cas-pcmdir` / `cas-exedir`) and reports occupancy plus duplication caused by cache-key pollution; shares the on-disk format helpers (`_load_*_manifest`, `_OBJ_BUCKET_RE`, `_PCH/PCM_COMMAND_HASH_RE`, `_CAS_EXE_SUFFIXES`) with `trim_cache.py` so format drift is impossible |
| `test_framework.py` | Single source of truth for the `--test-xml-dir` framework table: `KNOWN_FRAMEWORKS` (gtest / doctest / Catch2) and `detect_framework(transitive_headers, test_source)`. Detection trips on header-substring match against the test's transitive header set, computed by `Hunter.header_dependencies`. Multi-match raises `ValueError` (must disambiguate); no match returns `None` (test runs without XML, warning at verbose ≥ 1). Consumed by `BuildBackend._run_tests` to append the per-framework XML-emit argv after `exe_path`, with the rerun-skip predicate extended to "skip iff `.result` is current AND (XML file exists OR no framework detected)". |

### Configuration Files

- `ct.conf.d/ct.conf` -- default variant, `variant-canonical-order` axis ordering, exe/test markers, file-locking settings
- `ct.conf.d/{axis}.conf` -- per-axis bundled defaults grouped by concern: **toolchain** (`gcc.conf`, `clang.conf`), **linker** (`ld.conf`, `gold.conf`, `mold.conf`, `wild.conf` — each adds `-fuse-ld=<name>` to LDFLAGS), **optimization** (`debug.conf`, `release.conf`), **instrumentation** (`asan.conf`, `ubsan.conf`, `tsan.conf`, `coverage.conf`, `lto.conf`). Composition collapses what was previously an N×M×K explosion of `<compiler>.<opt>.<instrument>.conf` files into N + M + K axis files plus on-the-fly synthesis. Canonical token order in `variant-canonical-order` is toolchain → linker → optimization → instrumentation, so `--variant=asan,release,mold,gcc` canonicalizes to `gcc.mold.release.asan`.
- `ct.conf.d/{variant}.conf` -- compiler-specific flags (e.g., `gcc.debug.conf`, `clang.release.conf`)
- Config priority: bundled < system (`/etc/xdg/ct`) < venv < user (`~/.config/ct`) < project (`{gitroot}/ct.conf.d/`) < cwd < env < CLI

### Variant Resolution (`configutils.py`)

The variant system collapses what was previously an O(N·M·K) per-combination conf-file explosion (`gcc.debug.asan.conf`, `clang.release.coverage.conf`, …) into O(N+M+K) orthogonal axis confs (`gcc.conf` + `debug.conf` + `asan.conf`) composed on-the-fly. `--variant=gcc,debug,asan` is split, canonicalized, and resolved against axis confs plus an optional literal `<canonical>.conf` composite override; the resolver builds a flat priority-ordered list of conf paths and hands them to configargparse as `default_config_files`.

Load-bearing pieces:

- **`_DEFAULT_VARIANT_CANONICAL_ORDER`** (tuple in `configutils.py`) is the SINGLE source of truth for canonical token ordering. Bundled `ct.conf` does NOT redeclare it as data; it shows the full list as a commented-out copy-paste starting point. `test_bundled_ct_conf_comment_example_matches_builtin` is the drift guard.
- **Override hierarchy:** `--variant-canonical-order=<tokens>` CLI > `CT_VARIANT_CANONICAL_ORDER` env > `variant-canonical-order = ...` in any ct.conf level > builtin. Implemented in `get_canonical_order(argv=...)`, which scans argv early (mirrors `extract_variant`) since the order is needed at create_parser time, before configargparse parses.
- **`extends = ...`** in any axis or composite conf names parents. Resolution is DFS with cycle detection (`VariantResolutionError`), first-visit diamond dedup, and a "highest-priority conf wins" rule for which `extends` value is taken when multiple files share a name. The traversal order of `extends` is behavior-affecting because `_resolve_axis` walks it in declared order and configargparse layers in load order — so `extends = werror, gcc` produces different flag layering than `extends = gcc, werror`. Both `test_bundled_composite_extends_obeys_canonical_order` (CI-time, for bundled bundles) and `_check_extends_canonical_order` (runtime, for user confs) enforce this.
- **Composite override** (`<canonical_name>.conf`) tunes a composition. Default semantics: layers on top of the canonical-token atoms (equivalent to `extends = <each canonical token>`). Explicit `extends = ...` in the composite replaces that default. Multiple composites in the hierarchy ALL contribute to `flat_paths` (so a project's `gcc.debug.conf` overlays the bundled one); the highest-priority composite's `extends` directive steers chain-seed selection.
- **Parse cache** (`_parse_conf_file_cached`, `@cache`'d by absolute path) collapses what was previously ~13 file opens per parseargs flow (`extract_variant` + `resolve_variant` + `canonicalize_variant_input` + a second `resolve_variant` from `_commonsubstitutions`, each re-reading every touched conf) down to one open per file per process. `test_resolve_variant_parses_each_conf_at_most_once` is the regression guard. Cleared by `clear_cache()` so per-test conf mutations stay independent.
- **Opinionated bundles** (`dev`, `ci`, `production`, `safety`, `perf`, `secure`) are tiny `<name>.conf` files using `extends = ...` to compose curated axis sets. They live alphabetically-late in `_DEFAULT_VARIANT_CANONICAL_ORDER` so a hypothetical `--variant=production,extralib` puts the bundle before the trailing unknown axis.
- **Legacy `variantaliases = {dict}`** is hard-failed by `_check_legacy_variant_config_keys` (byte-level scan, line-anchored — commented lines correctly pass). See `README.ct-config.rst` "Upgrading from variantaliases" for migration recipes.

The flow inside one parseargs invocation: `create_parser` → `extract_variant(argv)` → `resolve_variant(variant, argv)` (consults builtin/conf/env/CLI for canonical_order, resolves axes, returns `VariantResolution`). After configargparse parses, `_commonsubstitutions` re-runs `canonicalize_variant_input(args.variant, argv=args._argv)` (because `--variant=gcc,debug` is stored raw by argparse) and a second `resolve_variant(argv=args._argv)` to populate `args._variant_resolution` for the `-vv` provenance trace. The `_argv` stash on `args` (set in `parseargs`) is what lets the second resolve hit the `--config=path` short-circuit branch instead of trying to resolve the implied basename as an axis.

### Command-Line Tools

Python entry points defined in `pyproject.toml [project.scripts]`. Shell wrappers in `scripts/` (ct-build, ct-release, ct-watch-build, profile-ct).

All tools support `--variant=<config>` for build configuration selection and `--backend=<name>` for build system backend selection (make, ninja, cmake, bazel, shake, tup).

**Standard CLI surface.** Every `ct-*` entry point must build its parser via `apptools.create_parser` + `apptools.add_base_arguments`, which guarantees `--version`, `--help`, `-?`, and `--man` are present. The `test_entry_point_surface` lint walks `pyproject.toml [project.scripts]`, invokes each `main(["--help"])` in-process, and asserts that surface (a `PINNED_CLI_TOOLS` allowlist relaxes the contract for `cas_publish` / `ct_lock_helper`, which are pinned-CLI build-recipe helpers and only need `--version`). Read-only diagnostics that want only the four `--cas-*dir` flags should call `add_cas_directory_arguments` instead of `add_output_directory_arguments` to avoid pulling in `--bindir` / `--use-mtime`. The lint also asserts no SIGINT/SIGTERM handler contamination after `--help` returns — long-running tools that install their own handlers should use `apptools.graceful_shutdown(handler, *signums)` (a context manager that saves, installs, and restores in a `finally`, deduping signums to avoid the "leak the body's handler past the with-block" footgun) rather than reimplementing the install/restore boilerplate.

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
