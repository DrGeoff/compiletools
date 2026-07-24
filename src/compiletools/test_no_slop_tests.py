"""Drift-guard lint-test: block low-value ("slop") tests at authorship.

The suite is currently clean (<1% slop). The point of this file is to
KEEP it clean forever by statically walking every ``src/compiletools/
test_*.py`` with the stdlib ``ast`` module and refusing to let a new test
land that verifies nothing, asserts a tautology, or swallows exceptions
without checking anything.

It mirrors the established in-repo lint style — ``test_entry_point_surface.py``
(its ``PINNED_CLI_TOOLS`` allowlist) and ``test_cas_dir_resolver_contract.py``
(its ``_RESOLVER_EXEMPT`` allowlist). Each rule has an explicit
``frozenset[str]`` allowlist keyed ``"filename::test_name"`` so an
intentional exception is documented inline, never silent.

Rules (all backed by ``ast`` only — no new deps, runs in well under a
second over the whole tree):

ERRORS (fail the build):
  R1 NO VERIFICATION  — a ``def test_*`` with zero ``assert`` / ``self.assert*``,
     zero ``pytest.raises``/``warns``/``fail``/``xfail``, no call to an
     asserting helper (name matches ``assert|compare|verify|expect|check|
     _still_raises|_pass``), and no ``asyncio.wait_for`` timeout wrapper.
  R2 TAUTOLOGY        — ``assert True`` / ``assert <truthy literal>`` /
     ``assert x == x`` (structurally identical operands) / ``assertTrue(True)``
     / ``assertEqual(x, x)``.  ``assert False, "msg"`` (a deliberate
     "unreachable" marker) is intentionally NOT flagged.
  R3 BARE EXCEPT      — a bare ``except:`` (NOT ``except Exception:``) with no
     assertion following it in the same function body.

WARNINGS (reported via ``warnings.warn`` + a passing report test — never fail):
  W1 MOCK-ASSERT-ONLY — the only verification is ``assert_called*`` /
     ``assert_not_called`` / ``assert_has_calls`` with no real
     ``assert`` / ``pytest.raises``.
  W2 CAPTURE-UNUSED   — a ``readouterr()`` / ``capsys`` result assigned but
     never referenced inside a later ``assert``.
  W3 NAME-PROMISE     — the name says creates/writes/removes/equals but no
     assert references ``os.path`` (for fs verbs) / an ``==`` compare (equals).
"""

from __future__ import annotations

import ast
import functools
import os
import re
import warnings

# ---------------------------------------------------------------------------
# Allowlists — one per ERROR rule. Key format: "<filename>::<test_name>".
# Every entry MUST carry an inline comment justifying the exception, and
# every entry is typo-guarded by test_allowlist_entries_are_live below.
# ---------------------------------------------------------------------------

# R1: tests that legitimately verify without a syntactic assert/raises/helper.
# Every entry is a "does-not-raise" / "does-not-crash" / no-op negative test:
# the verification IS that the call under test completes without throwing.
# Adding a *new* zero-verification test still trips R1 — these are the
# enumerated, reviewed exceptions, not a blanket exemption.
_R1_NO_VERIFICATION_ALLOWLIST: frozenset[str] = frozenset(
    {
        # _substitute_CXX_for_missing / _deduplicate_all_flags must not raise on
        # the missing-attribute inputs these two feed them.
        "test_apptools.py::test_no_ld_attribute_ok",
        "test_apptools.py::test_missing_flag_ok",
        # BazelBackend success path returns quietly (no exception, no output).
        "test_bazel_backend.py::test_success_returns_quietly",
        # clean()/realclean() must silently skip absent dirs/files.
        "test_build_backend.py::test_clean_skips_nonexistent_directories",
        "test_build_backend.py::test_realclean_skips_nonexistent_files",
        # BuildTimer is a no-op when given no output path.
        "test_build_timer.py::test_noop_without_path",
        # --static is accepted (does not raise) for the make backend.
        "test_cake_backend.py::test_static_flag_allowed_for_make_backend",
        # ccache statslog parsing tolerates a missing / unparseable file.
        "test_cake_ccache_statslog.py::test_missing_statslog_does_not_crash",
        "test_cake_ccache_statslog.py::test_unparseable_statslog_does_not_crash",
        # file_analyzer must not raise on multibyte / binary-garbage input.
        "test_file_analyzer.py::test_multibyte_before_raw_string_window",
        "test_file_analyzer.py::test_binary_garbage_does_not_crash",
        # findtargets.process at verbose>=2 must run without raising.
        "test_findtargets.py::test_process_verbose",
        # -isystem parsing / DirectHeaderDeps construction must not raise.
        "test_headerdeps.py::test_isystem_flag_parsing",
        # clear_instance_cache is a "just call it, must not raise" smoke test.
        "test_headerdeps.py::test_direct_clear_instance_cache",
        "test_hunter.py::test_clear_instance_cache_without_dynamic_attrs",
        # locking error/no-op paths must degrade gracefully, not crash.
        "test_locking.py::test_set_lockdir_permissions_chown_permission_error",
        "test_locking.py::test_release_error_handled",
        "test_locking.py::test_release_without_acquire",
        "test_locking.py::test_context_manager_no_file_locking",
        # touch-result-marker with an empty path is a documented no-op.
        "test_test_command_for.py::test_touch_result_marker_empty_path_is_noop",
        # skip_if_bazel_env_error is a no-op on unrelated stderr.
        "test_testhelper.py::test_skip_if_bazel_env_error_noop_on_unrelated_stderr",
    }
)

# R2: intentional tautologies (none expected — a tautology is never useful).
_R2_TAUTOLOGY_ALLOWLIST: frozenset[str] = frozenset(set())

# R3: bare-except tests where the swallow itself is the thing under test.
_R3_BARE_EXCEPT_ALLOWLIST: frozenset[str] = frozenset(set())


# ---------------------------------------------------------------------------
# Regex vocabularies
# ---------------------------------------------------------------------------

# A call whose name matches this counts as "verification" for R1. Covers real
# asserts (self.assertEqual), delegating helpers (_check_*, _verify_*,
# expect_*, compare_*), negative-control helper names, and validator helpers
# (validate_*) whose contract is "raise on violation" — a call to one in a test
# body IS the negative "does-not-raise" assertion (e.g.
# validate_no_conf_contradictions).
_VERIFY_HELPER_RE = re.compile(r"assert|compare|verify|validat|expect|check|_still_raises|_pass", re.IGNORECASE)

# pytest verification callables (used as `with pytest.raises(...)` etc.).
_PYTEST_VERIFY_ATTRS = frozenset({"raises", "warns", "fail", "xfail"})

# timeout wrappers that ARE the verification (the test asserts "completes in time").
_TIMEOUT_ATTRS = frozenset({"wait_for"})

# mock-style pseudo-assertions (W1).
_MOCK_ASSERT_RE = re.compile(r"^assert_(called|not_called|has_calls|any_call)")

# fs-verb / equality name promises (W3).
_W3_FS_VERBS = ("creates", "writes", "removes")
_W3_EQ_VERB = "equals"


def _test_python_files():
    """Yield absolute paths of every ``test_*.py`` beside this file, except
    this linter itself (it would otherwise scan its own docstrings/regex)."""
    src_dir = os.path.dirname(__file__)
    me = os.path.basename(__file__)
    for fname in sorted(os.listdir(src_dir)):
        if fname.startswith("test_") and fname.endswith(".py") and fname != me:
            yield os.path.join(src_dir, fname)


def _iter_test_functions(tree: ast.AST):
    """Yield every ``def test_*`` / ``async def test_*`` node in a module tree."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def _call_names(func: ast.AST) -> list[str]:
    """Return the callee name of every Call in ``func`` (``func.attr`` for
    attribute calls, ``func.id`` for bare-name calls)."""
    names: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.append(f.attr)
            elif isinstance(f, ast.Name):
                names.append(f.id)
    return names


def _has_assert(func: ast.AST) -> bool:
    return any(isinstance(n, ast.Assert) for n in ast.walk(func))


def _has_pytest_raises(func: ast.AST) -> bool:
    return any(name in _PYTEST_VERIFY_ATTRS for name in _call_names(func))


def _is_verifying_call(name: str) -> bool:
    return name in _PYTEST_VERIFY_ATTRS or name in _TIMEOUT_ATTRS or _VERIFY_HELPER_RE.search(name) is not None


def _body_verifies(func: ast.AST, verifying_helpers: frozenset[str]) -> bool:
    """Does ``func``'s body verify anything, directly or by delegation?

    Direct: an ``assert`` / ``pytest.raises`` / an asserting-helper call
    (name matches ``_VERIFY_HELPER_RE`` or is a pytest/timeout attr).
    Delegated: a call to a locally-defined helper that itself verifies —
    ``verifying_helpers`` is the fixpoint set of such helper names. This is
    what stops the ~46 delegating tests (``self._compile_edit_compile(...)``,
    ``self._find_samples_targets(...)``, ``_test_library(...)``, ...) from
    tripping R1: the assertion lives in the helper, not the test body.
    """
    if _has_assert(func):
        return True
    return any(_is_verifying_call(name) or name in verifying_helpers for name in _call_names(func))


def _build_verifying_helper_names() -> frozenset[str]:
    """Fixpoint set of helper (non-``test_*``) function/method names — across
    every ``test_*.py`` — whose body verifies (transitively).

    Iterated to a fixpoint so a helper that only verifies by calling another
    helper is still recognised. Name-keyed (class scoping ignored): if ANY
    helper of a given name verifies, a call to that name counts as verifying.
    Over-permissive by design — it can only ever REDUCE false positives, and
    a brand-new zero-verification test that calls nothing still trips R1.
    """
    helpers: list[tuple[str, ast.AST]] = []
    for path in _test_python_files():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test_"):
                helpers.append((node.name, node))

    verifying: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, node in helpers:
            if name in verifying:
                continue
            if _body_verifies(node, frozenset(verifying)):
                verifying.add(name)
                changed = True
    return frozenset(verifying)


@functools.cache
def _verifying_helpers() -> frozenset[str]:
    return _build_verifying_helper_names()


def _has_verification(func: ast.AST) -> bool:
    """R1 predicate: does the test verify ANYTHING (direct or delegated)?"""
    return _body_verifies(func, _verifying_helpers())


# ---------------------------------------------------------------------------
# ERROR-rule collectors
# ---------------------------------------------------------------------------


def _collect_r1(func: ast.AST) -> bool:
    """True if the function is an R1 violation (no verification at all)."""
    return not _has_verification(func)


def _collect_r2(func: ast.AST) -> bool:
    """True if the function contains a tautological assertion."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            test = node.test
            # assert True / assert 1 / assert "x" — truthy literal only.
            # assert False/0/None is a deliberate "unreachable" marker; not slop.
            if isinstance(test, ast.Constant):
                try:
                    if bool(test.value):
                        return True
                except Exception:
                    pass
            # assert x == x  (structurally identical operands, single Eq).
            # Excludes operands that contain a Call: `assert f() == f()` is a
            # legitimate determinism/idempotency check (f could be impure), not
            # a tautology — real examples: _ca_target(rule) == _ca_target(rule).
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and ast.dump(test.left) == ast.dump(test.comparators[0])
                and not _contains_call(test.left)
            ):
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            # assertTrue(True) / assertFalse(False)
            if name in ("assertTrue", "assertFalse") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant):
                    want = name == "assertTrue"
                    if bool(a0.value) is want:
                        return True
            # assertEqual(x, x) / assertIs(x, x) — identical operands (same
            # Call-exclusion rationale as the assert x == x branch above).
            if (
                name in ("assertEqual", "assertEquals", "assertIs", "assertIsNot", "assertNotEqual")
                and len(node.args) >= 2
            ):
                if ast.dump(node.args[0]) == ast.dump(node.args[1]) and not _contains_call(node.args[0]):
                    return True
    return False


def _contains_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def _collect_r3(func: ast.AST) -> bool:
    """True if the function has a bare ``except:`` with no assertion after it."""
    bare_lines = [h.lineno for h in ast.walk(func) if isinstance(h, ast.ExceptHandler) and h.type is None]
    if not bare_lines:
        return False
    assert_lines = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Assert)]
    # Violation if any bare-except has no assert on a later line.
    return any(not any(al > bl for al in assert_lines) for bl in bare_lines)


# ---------------------------------------------------------------------------
# WARNING-rule collectors (non-blocking)
# ---------------------------------------------------------------------------


def _collect_w1(func: ast.AST) -> bool:
    """Mock-assert-only: mock pseudo-assert present, no real assert/raises."""
    names = _call_names(func)
    has_mock = any(_MOCK_ASSERT_RE.match(n) for n in names)
    if not has_mock:
        return False
    return not _has_assert(func) and not _has_pytest_raises(func)


def _collect_w2(func: ast.AST) -> bool:
    """A readouterr()/capsys capture assigned but never used inside an assert."""
    captured_names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            is_capture = isinstance(f, ast.Attribute) and f.attr == "readouterr"
            if is_capture:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        captured_names.add(tgt.id)
    if not captured_names:
        return False
    # Names referenced anywhere inside an assert node.
    used_in_assert: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    used_in_assert.add(sub.id)
    return bool(captured_names - used_in_assert)


def _collect_w3(name: str, func: ast.AST) -> bool:
    """Name promises a check the body lacks."""
    lowered = name.lower()
    asserts = [n for n in ast.walk(func) if isinstance(n, ast.Assert)]
    if any(verb in lowered for verb in _W3_FS_VERBS):
        # Expect at least one assert that touches os.path / a path predicate.
        for a in asserts:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Attribute) and sub.attr in (
                    "exists",
                    "isdir",
                    "isfile",
                    "islink",
                    "getsize",
                    "getmtime",
                    "lexists",
                ):
                    return False
        return True
    if _W3_EQ_VERB in lowered:
        for a in asserts:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Compare) and any(isinstance(op, ast.Eq) for op in sub.ops):
                    return False
        return True
    return False


# ---------------------------------------------------------------------------
# Shared scan
# ---------------------------------------------------------------------------


def _scan(collector, allowlist: frozenset[str]) -> list[str]:
    """Run ``collector(func)`` over every test function; return non-allowlisted
    ``"<file>::<name>"`` keys where it returned True."""
    hits: list[str] = []
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            key = f"{basename}::{func.name}"
            if key in allowlist:
                continue
            if collector(func):
                hits.append(key)
    return hits


# ---------------------------------------------------------------------------
# ERROR tests
# ---------------------------------------------------------------------------


def test_no_test_lacks_verification():
    """R1: every ``def test_*`` must verify something."""
    hits = _scan(_collect_r1, _R1_NO_VERIFICATION_ALLOWLIST)
    assert not hits, (
        "These tests contain no verification (no assert / self.assert* / "
        "pytest.raises|warns|fail|xfail / asserting-helper call / timeout "
        "wrapper):\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: add a real assertion of the observable postcondition. "
        "If the test genuinely verifies via a helper the linter can't see, "
        "either rename the helper to match "
        "assert|compare|verify|expect|check|_still_raises|_pass, or add the "
        "test to _R1_NO_VERIFICATION_ALLOWLIST with a one-line justification."
    )


def test_no_tautological_assertions():
    """R2: no ``assert True`` / ``assert x == x`` / ``assertTrue(True)`` etc."""
    hits = _scan(_collect_r2, _R2_TAUTOLOGY_ALLOWLIST)
    assert not hits, (
        "These tests assert a tautology (always-true literal or identical "
        "operands) — they can never fail and verify nothing:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: assert the real value/postcondition instead."
    )


def test_no_bare_except_without_assertion():
    """R3: no bare ``except:`` that swallows without a following assertion."""
    hits = _scan(_collect_r3, _R3_BARE_EXCEPT_ALLOWLIST)
    assert not hits, (
        "These tests use a bare `except:` (which also swallows "
        "KeyboardInterrupt/SystemExit) with no assertion following it — a "
        "silent pass-through that hides failures:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: catch a specific exception (or `except Exception:`) and "
        "assert the expected outcome. If the swallow is deliberately the "
        "thing under test, add an assertion after it or allowlist it."
    )


# ---------------------------------------------------------------------------
# Allowlist typo-guard (mirrors test_entry_point_surface / cas-dir-resolver)
# ---------------------------------------------------------------------------


def test_allowlist_entries_are_live():
    """Every allowlist key must name a real ``<file>::<test>`` that still
    exists, so a rename/removal can't leave a stale silent exemption."""
    live: set[str] = set()
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            live.add(f"{basename}::{func.name}")

    for label, allowlist in (
        ("_R1_NO_VERIFICATION_ALLOWLIST", _R1_NO_VERIFICATION_ALLOWLIST),
        ("_R2_TAUTOLOGY_ALLOWLIST", _R2_TAUTOLOGY_ALLOWLIST),
        ("_R3_BARE_EXCEPT_ALLOWLIST", _R3_BARE_EXCEPT_ALLOWLIST),
    ):
        stale = sorted(allowlist - live)
        assert not stale, f"{label} has entries that no longer exist: {stale}"


# ---------------------------------------------------------------------------
# WARNING report (always passes; surfaces W1/W2/W3 without blocking)
# ---------------------------------------------------------------------------


def _collect_all_warnings() -> dict[str, list[str]]:
    w1: list[str] = []
    w2: list[str] = []
    w3: list[str] = []
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            key = f"{basename}::{func.name}"
            if _collect_w1(func):
                w1.append(key)
            if _collect_w2(func):
                w2.append(key)
            if _collect_w3(func.name, func):
                w3.append(key)
    return {"W1": w1, "W2": w2, "W3": w3}


def test_report_soft_warnings():
    """Non-blocking: emit W1/W2/W3 findings via ``warnings.warn`` so they are
    visible in the pytest warnings summary without ever failing the build.

    This test always passes — the ERROR rules above are the gate; these are
    advisory signals for reviewers.
    """
    found = _collect_all_warnings()
    descriptions = {
        "W1": "MOCK-ASSERT-ONLY (only assert_called*/assert_not_called/assert_has_calls, no real assert)",
        "W2": "CAPTURE-UNUSED (readouterr()/capsys captured but never used in an assert)",
        "W3": "NAME-PROMISE (name says creates/writes/removes/equals but body lacks the matching check)",
    }
    for code, keys in found.items():
        if keys:
            warnings.warn(
                f"[no-slop {code}] {descriptions[code]} — {len(keys)} test(s):\n" + "\n".join(f"    {k}" for k in keys),
                stacklevel=2,
            )
    # Advisory only. Assert the collector returned a well-formed mapping so this
    # report test is itself verified (and can never silently break its scan).
    assert set(found) == {"W1", "W2", "W3"}
    assert all(isinstance(v, list) for v in found.values())
