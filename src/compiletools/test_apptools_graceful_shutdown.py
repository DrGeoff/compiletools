"""Tests for ``apptools.graceful_shutdown``.

The context manager is the canonical way to install SIGINT/SIGTERM
handlers in compiletools; the four sites that previously hand-rolled
the save/install/restore dance (cake.py, ct_lock_helper.py,
locking.atomic_compile, trace_backend.execute) all delegate to it.

These tests focus on the structural guarantees the helper provides:

* handlers are restored after the with block exits, including on
  exception
* off-main-thread use is a silent no-op (mirrors the pre-existing
  guards in locking.py)
* invalid / platform-conditional signums don't crash the caller
* nested with-blocks restore in reverse order (last-in / first-out)
"""

from __future__ import annotations

import signal
import threading

import pytest

from compiletools import apptools


def _capture_handlers(*signums):
    return {sig: signal.getsignal(sig) for sig in signums}


def test_handlers_restored_after_normal_exit():
    """Happy path: with-block exits cleanly, original handler comes back."""
    before = _capture_handlers(signal.SIGINT, signal.SIGTERM)

    def my_handler(signum, frame):  # pragma: no cover - never invoked in this test
        pass

    with apptools.graceful_shutdown(my_handler):
        # Inside the block, the handler is ours.
        assert signal.getsignal(signal.SIGINT) is my_handler
        assert signal.getsignal(signal.SIGTERM) is my_handler

    after = _capture_handlers(signal.SIGINT, signal.SIGTERM)
    assert before == after


def test_handlers_restored_when_body_raises():
    """The whole point of the helper: restoration is guaranteed."""
    before = _capture_handlers(signal.SIGINT, signal.SIGTERM)

    def my_handler(signum, frame):  # pragma: no cover
        pass

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with apptools.graceful_shutdown(my_handler):
            raise Boom()

    after = _capture_handlers(signal.SIGINT, signal.SIGTERM)
    assert before == after, "handlers leaked after the body raised"


def test_default_signums_are_sigint_and_sigterm():
    """No-args form covers the common ``Ctrl-C / kill`` pair."""
    def my_handler(signum, frame):  # pragma: no cover
        pass

    with apptools.graceful_shutdown(my_handler):
        assert signal.getsignal(signal.SIGINT) is my_handler
        assert signal.getsignal(signal.SIGTERM) is my_handler


def test_explicit_signums_only_install_those():
    """Caller can target a subset (e.g. just SIGTERM)."""
    before_int = signal.getsignal(signal.SIGINT)

    def my_handler(signum, frame):  # pragma: no cover
        pass

    with apptools.graceful_shutdown(my_handler, signal.SIGTERM):
        assert signal.getsignal(signal.SIGTERM) is my_handler
        # SIGINT was NOT in the signums list — it must be untouched.
        assert signal.getsignal(signal.SIGINT) is before_int

    # And SIGTERM is restored on exit.
    assert signal.getsignal(signal.SIGINT) is before_int


def test_off_main_thread_is_silent_noop():
    """``signal.signal`` raises ValueError off the main thread; the
    helper swallows that and yields without installing anything."""
    handler_invocations = []

    def my_handler(signum, frame):  # pragma: no cover
        handler_invocations.append(signum)

    body_ran = threading.Event()
    body_saw_unchanged_handlers = []
    main_thread_before = signal.getsignal(signal.SIGINT)

    def worker():
        with apptools.graceful_shutdown(my_handler):
            # On a non-main thread, the install was skipped, so the
            # main thread's view of the handler should be unchanged.
            body_saw_unchanged_handlers.append(signal.getsignal(signal.SIGINT))
        body_ran.set()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert body_ran.is_set(), "body did not run — helper raised on non-main thread?"
    # No mutation should have happened from the worker.
    assert body_saw_unchanged_handlers == [main_thread_before]
    assert signal.getsignal(signal.SIGINT) is main_thread_before


def test_unknown_signum_is_ignored():
    """A bogus signum (e.g. an int that the platform doesn't recognise)
    must not crash the caller. We use a sentinel that's almost certainly
    not a real signal on any supported platform."""
    bogus_signum = 12345

    def my_handler(signum, frame):  # pragma: no cover
        pass

    # The body runs cleanly even though the install of the bogus signum
    # silently failed.
    with apptools.graceful_shutdown(my_handler, bogus_signum, signal.SIGTERM):
        # The valid signum was still installed.
        assert signal.getsignal(signal.SIGTERM) is my_handler

    # And restoration of the valid signum still works.
    assert signal.getsignal(signal.SIGTERM) is not my_handler


def test_nested_blocks_restore_in_reverse_order():
    """Two nested with-blocks: the inner one's exit restores the outer's
    handler, then the outer's exit restores the original."""
    original = signal.getsignal(signal.SIGINT)

    def outer_handler(signum, frame):  # pragma: no cover
        pass

    def inner_handler(signum, frame):  # pragma: no cover
        pass

    with apptools.graceful_shutdown(outer_handler, signal.SIGINT):
        assert signal.getsignal(signal.SIGINT) is outer_handler
        with apptools.graceful_shutdown(inner_handler, signal.SIGINT):
            assert signal.getsignal(signal.SIGINT) is inner_handler
        # Inner exited -> back to outer.
        assert signal.getsignal(signal.SIGINT) is outer_handler

    assert signal.getsignal(signal.SIGINT) is original


def test_sentinel_handlers_work():
    """``signal.SIG_IGN`` and ``signal.SIG_DFL`` are valid handler
    arguments and the helper must accept them — they're the way to
    *suppress* a signal during the with block."""
    original = signal.getsignal(signal.SIGINT)

    with apptools.graceful_shutdown(signal.SIG_IGN, signal.SIGINT):
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN

    assert signal.getsignal(signal.SIGINT) is original
