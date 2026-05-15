# CLAUDE.md

Guidance for Claude Code working in this repository. This file is the orientation layer — `src/compiletools/CLAUDE.md` is auto-loaded for source edits and holds the deep architecture rationale. Code-level details live in module docstrings.

## Overview

`compiletools` is a Python C/C++ build-tool package. Core philosophy: "magic" — automatic dependency detection and build configuration via source analysis. Python 3.10+.

## Worktree-Based Development

Each git worktree under `compiletools/` needs its own venv (`uv pip install -e ".[dev]"` from inside it). Editable installs record the install path, so a venv from `master/` keeps importing `compiletools` from `master/src/` even when you `cd` elsewhere — `ct-cake` and e2e suites would silently exercise the wrong code. Verify with `ct-check-venv`. The pytest helper `compiletools.testhelper.skipif_e2e_unavailable` enforces this on every e2e marker.

## Build / Test Commands

```bash
uv pip install -e ".[dev]"     # dev deps: ruff, pyright, prek, pytest-xdist
prek install                   # one-time per checkout
pytest -n auto                 # full parallel run (~3.5 min)
ruff check src/compiletools/   # lint
ruff format src/compiletools/  # format
pyright src/compiletools/      # type-check (also a prek hook)
prek run --all-files           # all hooks
```

pytest config in `pyproject.toml [tool.pytest.ini_options]`; tests live next to source as `test_*.py`.

## Architecture

### Build flow (`ct-cake --auto`, in `cake.py`)
1. **Config** (`configutils.py`): merge bundled < system < venv < user < project < cwd < env < CLI. Variants are *composed* from axis confs (toolchain / linker / optimization / instrumentation); `--variant=gcc,debug,asan` synthesizes from `gcc.conf` + `debug.conf` + `asan.conf`. Canonical token order from `variant-canonical-order` in `ct.conf` (or `_DEFAULT_VARIANT_CANONICAL_ORDER` builtin) — both halves of the system must agree, drift-guarded by `test_bundled_ct_conf_comment_example_matches_builtin`. Legacy `variantaliases = {...}` is hard-failed by `_check_legacy_variant_config_keys`.
2. **Targets** (`findtargets.py`): two-stage argparse — first parse extracts variant, discovery modifies target list, second parse produces final config.
3. **Deps** (`hunter.py`, `headerdeps.py`, `magicflags.py`): walk `#include` graph (factory: `DirectHeaderDeps`/`CppHeaderDeps`); extract `//#` flag annotations; discover implied sources (`foo.h` → `foo.cpp`).
4. **LDFLAGS merge** (`utils.merge_ldflags_with_topo_sort`): soft edges (single `PKG-CONFIG`) cancel on disagreement; hard edges (multi-package `PKG-CONFIG=a b`) always kept; cycles after cancellation raise `ValueError` naming offenders.
5. **Backend dispatch** (`build_backend.py`): `--backend` selects from registry; `build_graph()` populates a `BuildGraph` of `BuildRule` objects, `generate()` writes the native build file.
6. **Execute**: `backend.execute("build")` → native tool (drives the `all` aggregate so test rules run inline alongside compile/link via `-j` scheduling); `_copyexes()` to bindir.

### Magic detection
`FileAnalyzer.analyze_file()` does a SIMD single-pass scan (stringzilla) returning `FileAnalysisResult`. `SimplePreprocessor.process_structured()` evaluates `#if`/`#ifdef`/`#define`. `PreprocessingCache` is two-tier: invariant (content_hash only, ~80% of files) vs variant (content_hash + macro_state_key for files with conditionals). `MacroState` separates ~388 immutable compiler built-ins from per-file `#define`s and only hashes the variable set.

### `args.flags` invariant
After `apptools.parseargs` returns, `args.flags` is a frozen `Flags` dataclass (`flags.py`) with `cpp`/`c`/`cxx`/`ld` tuples + `compiler_identity`. New consumers should read `args.flags`, not retokenize `args.CPPFLAGS`. The raw `args.{CPPFLAGS,CFLAGS,...}` strings, the `args.{*}_tokens` lists, and `args.flags` are **populated once and must not be mutated afterwards**. `apptools.check_flag_string_drift(args)` raises `RuntimeError` if drift is detected.

### Path-canonical CAS keys
All four CAS layers hash *gitroot-relative* paths via `apptools.canonicalize_path_for_cache_key` (`<gitroot>/...` → `<GITROOT>/...` sentinel) and `canonicalize_for_cache_key` for path-bearing flag tokens (`-I -isystem -iquote -idirafter -F -B -include -include-pch`, `-Wl,...`, `-Xlinker /abs`). Empty `anchor_root` is identity. `compiler_identity = realpath|size|mtime_ns` is folded into hashes so in-place toolchain swaps still invalidate. Manifest writers require `anchor_root` as a kwarg; lint `test_every_production_caller_passes_anchor_root` catches drops.

### Workspace-relative compile paths (`-ffile-prefix-map`)
`apptools._inject_ffile_prefix_map` appends `-ffile-prefix-map=<gitroot>=<target>` (default `.`) to CXXFLAGS/CFLAGS for cross-user `.o` byte-identity. Skipped per-slot if the user already set any `-f{file,debug,macro,canon}-prefix-map=`. **PCH/BMI bytes themselves diverge across workspaces** (gcc layout depends on cwd path *length* — no flag controls it), but cross-user CAS sharing works because PCH and clang-PCM precompile rules emit workspace-relative source + set `BuildRule.cwd = anchor_root` (gated on `_is_under`). Backends honour `cwd` via `cd <cwd> && ` in the lock wrapper or `subprocess.run(cwd=)`. For gcc modules, `-gno-record-gcc-switches` is appended when `-fmodule-mapper=` is in use to suppress the per-user mapper-path leak in `DW_AT_producer`. See `src/compiletools/CLAUDE.md` for full background.

### Locking (`locking.py`)
Four strategies auto-selected by FS (`FcntlLock` GPFS, `LockdirLock` NFS/Lustre, `CIFSLock`, `FlockLock` local). `atomic_compile`/`atomic_link` run children in a new session and forward SIGINT/SIGTERM. **Two invariants for concurrent peer-make safety on the object CAS** (violation symptom: sporadic `undefined reference to 'main'`):
1. **Lock a sidecar (`<target>.lock`), never the target** — locking the target creates an empty file with `mtime=now` that peer makes treat as up-to-date.
2. **Producer-side temp+rename** — compiler writes `<target>.compiletools.tmp` then `mv -f`. Link rules read `.o` without locking; rename guarantees old-or-new inode, never partial bytes.

Must hold in `wrap_compile_with_lock`/`wrap_link_with_lock` (Make/Ninja native flock) and `atomic_compile`/`atomic_link` (Shake/Slurm via `trace_backend.py`). See `src/compiletools/CLAUDE.md` for the full rationale.

### CAS layers
- **`cas-objdir`**: `{basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o` — 168-bit entropy because there's no in-band `.o` verification at link time (collision = silent miscompile).
- **`cas-pchdir` / `cas-pcmdir`**: `<cmd_hash>/<name>.{gch,pcm,gcm}` + `manifest.json`. Single 64-bit hash is safe because the compiler verifies BMI compatibility at consume time. PCH consumer wiring is `-include <cas-pchdir>/<hash>/<basename>` — NOT `-I` (gcc's `#include "h"` searches the source dir first and would bypass the cache). `_stage_pch_header_alongside_gch` hardlinks the `.h` next to the `.gch` for gcc fallback; bazel uses a second-stage hardlink into `.ct-bazel-pch/` plus `-std=` pinning via `apptools.compiler_default_cxx_std(args.CXX)` for dialect alignment. gcc BMI placement steered by per-makefile `<dirname(makefilename)>/.module-mapper.txt`.
- **`cas-exedir`**: `<linkkey[:2]>/<basename>_<linkkey>.<ext>`. Producer writes CAS path; `symlink` rule publishes `bin/<variant>/<name>` via `ct-cas-publish` (atomic `link()`+`rename()`, `EXDEV`-only symlink fallback). Sidecars: `.manifest` (source_realpath) and `.result` (content-keyed test-pass marker — NOT mtime-based, since published exes inherit cas creation time).
- **Link key** folds: linker identity, canonical LDFLAGS, canonical sorted obj paths, `SOURCE_DATE_EPOCH`, `LIBRARY_PATH`, `LD_LIBRARY_PATH`, `LD_PRELOAD`, `ar` identity, and **full canonical bindir** (`bin/blank` vs `out/blank` collide on basename alone).

### Mtime-vs-CAS rebuild (`--use-mtime`)
Default `False` (CAS-only): make/ninja drop sources/objects from prereqs; CAS artefact existence is the only rebuild signal. `True` restores mtime semantics for interactive workflows. Only make/ninja honour it (`_honors_use_mtime()`); other backends warn on stderr. CAS-only caveats: generated headers must exist at headerdep time; `make -t` / `ninja -t restat` are inappropriate (create empty files that corrupt the cache); `/usr/include` isn't in the cache key (matches ccache contract).

### Variant resolution + compile DB
Variants compose O(N+M+K) axis confs on-the-fly. `extends = ...` is DFS-resolved with cycle detection (`VariantResolutionError`) and "highest-priority conf wins" semantics. Traversal order is behavior-affecting — guarded by `test_bundled_composite_extends_obeys_canonical_order` (CI) and `_check_extends_canonical_order` (runtime). Canonical-order override hierarchy: CLI > `CT_VARIANT_CANONICAL_ORDER` env > `variant-canonical-order` in any ct.conf > builtin tuple. `_parse_conf_file_cached` collapses ~13 file opens per parseargs to one per file per process. Per-variant `compile_commands.<variant>.json` + atomic `compile_commands.json` symlink to the most recent build (clangd opens the bare name; CDB spec allows multiple entries per source but consumers pick one).

### Module notes (only what filenames don't tell you)
- `flags.py` — frozen `Flags` dataclass, built once per `parseargs` as `args.flags`; `hash_relevant`, `existing_include_paths`, `append_include`.
- `build_backend.py` — `BuildBackend` ABC + registry; `build_graph()`, PCH/PCM/link-key hashing, `_has_native_cas_exe` dispatch.
- `trace_backend.py` — both Shake and Slurm backends (self-executing verifying traces).
- `trim_cache.py` / `cache_report.py` — share on-disk format helpers so format drift is impossible; `trim_exedir` re-stats `nlink` under lock to close scan-to-unlink TOCTOU.
- `test_framework.py` — single source for `--test-xml-dir` framework table (gtest/doctest/Catch2); multi-match raises, no-match warns at verbose ≥ 1.

### CLI surface
Every `ct-*` entry point uses `apptools.create_parser` + `add_base_arguments` (guarantees `--version`/`--help`/`-?`/`--man`); lint `test_entry_point_surface` enforces, with `PINNED_CLI_TOOLS` allowlisting `cas_publish` / `ct_lock_helper`. Read-only diagnostics that want only the four `--cas-*dir` flags should use `add_cas_directory_arguments` (no `--bindir` / `--use-mtime`). Tools installing signal handlers must use `apptools.graceful_shutdown(handler, *signums)`.

### Configuration files
- `ct.conf.d/ct.conf` — default variant, canonical order, exe/test markers, locking
- `ct.conf.d/{axis}.conf` — toolchain (`gcc.conf`, `clang.conf`), linker (`ld/gold/mold/wild.conf`), optimization (`debug/release.conf`), instrumentation (`asan/ubsan/tsan/coverage/lto.conf`)
- `ct.conf.d/{variant}.conf` — composite overrides (e.g. `gcc.debug.conf`)
- Priority: bundled < system (`/etc/xdg/ct`) < venv < user (`~/.config/ct`) < project (`{gitroot}/ct.conf.d/`) < cwd < env < CLI

## Test Conventions

- Function-based for simple cases; `BaseCompileToolsTestCase` (`test_base.py`) when cache isolation is needed. Helpers in `testhelper.py` (`TempDirContext`, `create_temp_config()`, `@requires_functional_compiler`), `examples_registry.py` (`example_path()`, `example_file()`), and `conftest.py` (`ensure_lock_helper_in_path`, `pkgconfig_env`).
- Never hardcode compiler names — use `@requires_functional_compiler` and `apptools.get_functional_cxx_compiler()`.
- Use `monkeypatch.chdir()` not raw `os.chdir()`. When removing/renaming methods, grep tests for `patch.object(...)`.
- `BuildRule.rule_type` validated against `VALID_RULE_TYPES` in `build_graph.py` — new type requires updating the frozenset.
