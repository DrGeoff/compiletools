# Plan 05 — `wrappedos` preference contract lint

Lock a currently-prose invariant into CI. The rule (in `src/compiletools/CLAUDE.md`,
"`wrappedos` preference and the documented skip cases") says: prefer the cached
`compiletools.wrappedos.<fn>` wrappers over raw `os.path.<fn>`, except in three documented
skip cases. This plan adds a test — no production behavior change.

## The contract (one sentence)

Every raw `os.path.{realpath,isfile,isdir,getmtime,getsize}` call in a production module
(non-`test_`, excluding `wrappedos.py`) must be EITHER accompanied by a skip-justification
comment (inline or the line immediately above, containing one of `CRITICAL`, `NOT wrappedos`,
`NOT cached`, `os.path.realpath directly`) OR listed in the `_WRAPPEDOS_EXEMPT` allowlist
keyed by `"basename:line"`, otherwise the test fails naming the offending `file:line`.

## Scope decision (keeps it false-positive-free)

- **Only the 5 correctness-sensitive stat functions:** `realpath`, `isfile`, `isdir`,
  `getmtime`, `getsize`. These are the ones where caching can be *wrong* (skip-case #3).
- **Deliberately EXCLUDE** `dirname`, `basename`, `join`, `normpath`, `isabs` — pure string
  ops that appear legitimately everywhere (e.g. `testhelper.py`, `git_sha_report.py`). Linting
  them would be pure noise.
- Regex with negative lookbehind so `wrappedos.realpath(` / attribute access never matches:
  `(?<![A-Za-z0-9_.])os\.path\.(realpath|isfile|isdir|getmtime|getsize)\s*\(`.
- Reuse a `_is_in_comment_or_string` helper (mirror the sibling lint) so the function names
  inside docstrings/comments aren't flagged.

## House-style mechanism

Mirror `test_cas_dir_resolver_contract.py`: a grep-scan + an explicit
`_WRAPPEDOS_EXEMPT: frozenset[str]` allowlist of `"basename:line"`, PLUS a comment-based
opt-out (because the codebase already documents skips inline with `CRITICAL` — e.g.
`locking.py:434`, `lock_utils.py:36`, `bazel_backend.py` docstring). Allowlist is primary;
comment path covers sites where an inline comment already exists. Add a typo-guard test
(`test_wrappedos_exempt_entries_refer_to_real_files`) like the resolver lint's
`test_resolver_exempt_entries_refer_to_real_files`.

## File + structure

New file `src/compiletools/test_wrappedos_preference_contract.py` (`test_` prefix → self-
excludes). It:
1. Enumerates production `.py` files (copy `_production_python_files()` from the sibling lint).
2. Scans each for the 5-function regex; computes 1-based line numbers.
3. For each hit: pass if a justification comment is on the hit line or the line above, else
   require an allowlist entry, else fail with `file:line` + the offending source line.

## Building the initial allowlist

The three skip cases that legitimately use raw `os.path`:
1. lock/sidecar stats whose mtime/existence changes concurrently;
2. build-output existence checks AFTER the producing rule ran (clean/realclean, post-build
   cache/cache-report/trim walks, diagnostics);
3. relative-path inputs subject to `chdir` (e.g. `bazel_backend` `BUILD.bazel`).

Generate it empirically: write the test, run it once, and for EACH reported site confirm by
hand it is a genuine case #1/#2/#3 skip before adding it (or add an inline justification
comment instead, which is preferable when natural). Candidate sites observed during planning
(verify each): `locking.py`, `lock_utils.py`, `ct_lock_helper.py`, `cache_report.py`,
`trim_cache.py`, `build_backend.py` (clean/realclean + post-rule), `trace_backend.py`,
`ninja_backend.py`, `apptools.py` (several `realpath` on `getcwd()`/`find_git_root()` absolute
strings — CLAUDE.md says these are safe-to-skip), `check_venv.py`, `examples_registry.py`,
`filesystem_utils.py`, `git_utils.py`, `bazel_backend.py`, `utils.py`, `timing_report.py`.

> Sequence with the splits: if Plans 02/04 run first, line numbers shift. Prefer the
> comment-based opt-out for sites that move (the comment travels with the code); reserve the
> allowlist for stable sites. If run before the splits, plan to refresh allowlist line numbers
> after each split (the typo-guard test will flag stale entries).

## Verification

```
cd <repo-root>
python -m pytest src/compiletools/test_wrappedos_preference_contract.py -v
```
Green only after every reported site is either comment-justified or allowlisted.

## Risks

- **False positives:** mitigated by the 5-function scope, the negative-lookbehind regex, the
  comment/string skip, and the allowlist. The one residual trip — a future raw call with no
  comment or allowlist entry — is the intended behavior, not a false positive.
- **Line-number churn** from the other plans — prefer inline comments; the typo-guard test
  catches stale allowlist lines.
