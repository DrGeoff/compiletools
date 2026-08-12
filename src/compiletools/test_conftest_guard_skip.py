"""The conftest toolchain-guard skip must never mask a guard regression.

``conftest.pytest_runtest_call`` converts "this host's toolchain is
under-spec" errors into skips. The decision function is
``conftest._guard_skip_reason``; these tests pin the boundary between the
two shapes that CONTAIN guard text but mean opposite things:

* a guard error escaping a test (or hidden behind ``pytest.warns``'s
  ``DID NOT WARN`` wrapper) -> skip, the host cannot run the test;
* a ``pytest.raises(..., match=...)`` mismatch quoting a guard message ->
  FAIL, the guard fired with the wrong text and the test caught it.
"""

import pytest

from compiletools import conftest

_GUARD_TEXT = "Resolved CXX='clang++' is clang 9, which is below compiletools' minimum supported toolchain (clang >= 10, the C++20 floor)."
_STD_TEXT = "g++ 11 does not support -std=c++26 (needs gcc >= 14)"


def test_direct_guard_error_skips():
    reason = conftest._guard_skip_reason(RuntimeError(_GUARD_TEXT))
    assert reason is not None
    assert "minimum supported toolchain" in reason


def test_direct_std_error_skips():
    reason = conftest._guard_skip_reason(RuntimeError(_STD_TEXT))
    assert reason is not None
    assert "does not support -std=" in reason


def test_raises_match_mismatch_quoting_guard_text_fails():
    """The regression the probe found: a pytest.raises match mismatch whose
    Failed/AssertionError message QUOTES the actual guard error. Skipping
    here masks a regressed guard on every host, capable or not."""
    try:
        with pytest.raises(RuntimeError, match="clang >= 10"):
            raise RuntimeError(_GUARD_TEXT.replace(">= 10", ">= 9"))
    except BaseException as mismatch:
        assert conftest._guard_skip_reason(mismatch) is None, (
            "a raises-match mismatch on a guard message must fail, not skip"
        )
    else:
        pytest.fail("pytest.raises unexpectedly matched")


def test_guard_error_behind_did_not_warn_skips():
    """The wrapper shape the chain walk exists for: a guard error raised
    inside pytest.warns surfaces as Failed('DID NOT WARN') with the real
    error only in __context__."""
    try:
        with pytest.warns(UserWarning, match="never emitted"):
            raise RuntimeError(_GUARD_TEXT)
    except BaseException as wrapped:
        assert "DID NOT WARN" in str(wrapped)
        reason = conftest._guard_skip_reason(wrapped)
        assert reason is not None
        assert "minimum supported toolchain" in reason


def test_guard_text_in_context_of_an_ordinary_failure_fails():
    """A plain assertion failure whose __context__ happens to hold a guard
    error is a test OBSERVING that error on a capable host — the chain walk
    must not reach it outside the DID NOT WARN wrapper."""
    try:
        try:
            raise RuntimeError(_GUARD_TEXT)
        except RuntimeError:
            # Implicit chaining is the shape under test: __context__ set,
            # no `from` — exactly what a stray assert in a handler produces.
            raise AssertionError("handler made a different claim")  # noqa: B904
    except AssertionError as chained:
        assert chained.__context__ is not None
        assert conftest._guard_skip_reason(chained) is None


def test_unrelated_error_does_not_skip():
    assert conftest._guard_skip_reason(RuntimeError("segmentation fault")) is None


def test_a_bare_pytest_fail_quoting_guard_text_fails():
    """``pytest.fail.Exception`` was widened into the caught tuple to catch
    the ``DID NOT WARN`` wrapper shape (see
    ``test_guard_error_behind_did_not_warn_skips``), but the same widening
    also let a completely unrelated, test-authored ``pytest.fail(...)`` call
    skip instead of fail whenever its own message happens to quote text
    that matches a guard pattern -- e.g. a test asserting on captured
    compiler stderr with ``pytest.fail(f"unexpected output: {stderr}")``.
    Unlike the DID NOT WARN wrapper (whose message is exactly that literal
    string, with the real guard error only in __context__), a bare
    pytest.fail() call's message IS the test's own claim and must never be
    treated as guard-worthy directly -- only the DID NOT WARN wrapper shape
    may reach a guard error through pytest.fail.Exception."""
    try:
        pytest.fail(f"unexpected compiler output: {_STD_TEXT}")
    except pytest.fail.Exception as exc:
        # A bool, not the raw reason string: pytest's assertion-rewrite would
        # otherwise embed the (possibly guard-matching) reason text straight
        # into THIS assertion's own failure message, which the outer
        # pytest_runtest_call hook would then re-classify as a guard skip —
        # the exact defect under test, tripped by the test itself.
        would_skip = conftest._guard_skip_reason(exc) is not None
        assert not would_skip, "a bare pytest.fail() call quoting guard-like text must fail, not skip"
    else:
        pytest.fail("pytest.fail() unexpectedly did not raise")
