# Deferred / cross-cutting follow-ups from v7.1.0..HEAD code review

These items came out of the parallel code-review pass and were NOT addressed in
the current fix sweep because each requires changes to files that crossed
worktree ownership boundaries. They are tracked here for a future pass.

---

## From backend architecture review

### Out-of-scope observation: `_all_outputs_current` contract

The "by accident" pattern fixed for cmake/bazel exists for *any* future backend
whose outputs are not at namer-derived paths. The fix here makes the contract
explicit only for `cmake` and `bazel`. A future contributor adding a new
"external build dir" backend should remember to override `_all_outputs_current`
themselves; the base-class docstring is already explicit that a `True` result
short-circuits the build.
