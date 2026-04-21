# Deferred / cross-cutting follow-ups from v7.1.0..HEAD code review

These items came out of the parallel code-review pass and were NOT addressed in
the current fix sweep because each requires changes to files that crossed
worktree ownership boundaries. They are tracked here for a future pass.

---

## From trim_cache review

### I-4: Per-realpath PCH bucketing requires sidecar manifest

**Owner file:** `src/compiletools/build_backend.py`

`trim_pchdir` ranks all `<pchdir>/<cmd_hash>/` dirs globally by mtime and keeps
`keep_count` overall. With many cmd_hash dirs (cross-variant, cross-user)
sharing the same underlying header realpath, `keep_count=1` evicts still-needed
cross-variant PCHs.

The `cmd_hash` is `sha256(compiler+flags+realpath_of_header)[:16]`. The realpath
itself is **not stored on disk** anywhere inside the cmd_hash dir — only
`<basename>.gch` files appear there. Bucketing by basename was already rejected
in v8.0.2 (I-B5: two unrelated projects both using `stdafx.h` evicted each
other).

**Fix shape.** `build_backend._pch_command_hash` (or the PCH compile rule
emitter near `build_backend.py:444`) should write a sidecar manifest:

    <pchdir>/<cmd_hash>/manifest.json
        { "header_realpath": "/abs/path/to/stdafx.h",
          "compiler": "...", "compiler_identity": "..." }

Then `trim_cache.trim_pchdir` can group cmd_hash dirs by `header_realpath`
(loaded from each manifest) and apply `keep_count` per bucket. The manifest also
gives a natural place to record transitive-header content hashes (see I-5),
letting trim pre-evict known-stale entries instead of relying on the GCC PCH
stamp at consume time.

**Pinned test.** `TestPchPerRealpathBucketing.test_current_global_keep_count_documented`
in `test_trim_cache.py` pins the current global-keep behavior. When sidecar
manifests land, that test must be updated (or replaced) to assert per-realpath
bucketing.

### I-5: PCH transitive-header content not in cache key

**Owner file:** `src/compiletools/build_backend.py`

The cmd_hash captures the immediate header's realpath but NOT the content of
headers it transitively includes. GCC's PCH stamp is the backstop — silently
rejected at consume time, slow rebuild for the user. Fix shape lives with the
I-4 manifest above (record transitive-header content hashes there too). A
matching comment already exists at `build_backend.py:1081-1087` (M-B6).

### I-6: `_warn_if_pchdir_not_cross_user_safe` fires inappropriately for cwd-bin pchdir

**Owner file:** `src/compiletools/build_backend.py`

When `pchdir` falls back to `bin/<variant>/pch` (under cwd, not a shared
cross-user location), `_warn_if_pchdir_not_cross_user_safe` (build_backend.py:994)
still fires the "directory not group-writable + SGID" warning. For a per-user
cwd path that warning is noise.

**Fix shape.** Detect cwd-relative pchdir paths (or paths under the build's
`objdir`/`bin` tree) and skip the warning. Possibly: accept a hint that the
caller intentionally chose a non-shared location and silently no-op in that
case.

---

## From backend architecture review

### Issue 8: `makefile.py` 59-line shim removal

**Owner file:** `src/compiletools/makefile.py`

The shim is intentionally deferred to a separate follow-up PR — cross-cutting
and risky.

### Out-of-scope observation: `_all_outputs_current` contract

The "by accident" pattern fixed for cmake/bazel exists for *any* future backend
whose outputs are not at namer-derived paths. The fix here makes the contract
explicit only for `cmake` and `bazel`. A future contributor adding a new
"external build dir" backend should remember to override `_all_outputs_current`
themselves; the base-class docstring is already explicit that a `True` result
short-circuits the build.

---

## From magic flags review

### Issue 6 (Minor): `_HARD_ORDERINGS_KEY` consumer-side documentation

**Owner file:** `src/compiletools/build_backend.py`
**Function:** `BuildBackend._merge_ldflags_for_sources`

The producer side (`magicflags._handle_pkg_config` / `_HARD_ORDERINGS_KEY`) is
now documented in `src/compiletools/magicflags.py` (the comment block immediately
above the sentinel). The consumer side that reads this sentinel out of the
per-file flags dict, aggregates it across files, and forwards it to
`utils.merge_ldflags_with_topo_sort(..., hard_orderings=...)` is in
build_backend.py and was NOT documented in the current pass.

Please add a docstring section to `BuildBackend._merge_ldflags_for_sources`
cross-referencing the contract documented at `magicflags._HARD_ORDERINGS_KEY`.
Specifically note:

* The key MUST be popped/filtered out of the per-file flags dict before the
  dict is otherwise consumed, so it never leaks into a real flag list.
* The aggregated value type fed to `merge_ldflags_with_topo_sort` is
  `list[tuple[str, str]]` of pairwise (pred_lib, succ_lib) constraints.
* Source-file provenance for cycle diagnostics should be carried in a parallel
  `hard_ordering_sources` list whose indices align with the flattened
  hard-orderings list.
