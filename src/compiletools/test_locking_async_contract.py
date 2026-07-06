"""Async-lock contract tests: parity with the sync atomic_compile/atomic_link
invariants (temp+rename, skip_if_exists, non-zero raises, no leaked temp) plus
the non-blocking acquire and the async child runner. No compiler required — the
"compile" is a shell command that writes to the rewritten ``-o`` temp path.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from compiletools import locking
from compiletools.locking import (
    HAS_FCNTL,
    FlockLock,
    atomic_compile_async,
    atomic_link_async,
)


def _writer_cmd(content: str = "obj"):
    """A compile_cmd that, once atomic_compile_async appends ``-o <temp>``,
    writes ``content`` to that temp path. Positional layout after append:
    ``sh -c SCRIPT seed -o <temp>`` → in the script $0=seed, $1=-o, $2=<temp>."""
    return ["sh", "-c", f'printf %s "{content}" > "$2"', "seed"]


def _fail_cmd(rc: int = 3):
    """A compile_cmd that exits non-zero without writing the temp."""
    return ["sh", "-c", f"exit {rc}", "seed"]


def test_atomic_compile_async_temp_then_rename(tmp_path):
    target = str(tmp_path / "out.o")
    rc = asyncio.run(atomic_compile_async(None, target, _writer_cmd("hello")))
    assert rc == 0
    assert os.path.exists(target)
    with open(target) as f:
        assert f.read() == "hello"
    # no temp files left behind
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_atomic_compile_async_skip_if_exists_returns_none(tmp_path):
    target = str(tmp_path / "out.o")
    with open(target, "w") as f:
        f.write("prebuilt")
    # _fail_cmd would raise if spawned; skip_if_exists must short-circuit first.
    rc = asyncio.run(atomic_compile_async(None, target, _fail_cmd(), skip_if_exists=True))
    assert rc is None
    with open(target) as f:
        assert f.read() == "prebuilt"  # untouched


def test_atomic_compile_async_nonzero_raises_and_leaves_no_temp(tmp_path):
    target = str(tmp_path / "out.o")
    with pytest.raises(subprocess.CalledProcessError):
        asyncio.run(atomic_compile_async(None, target, _fail_cmd(5)))
    assert not os.path.exists(target)
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_atomic_link_async_rewrites_o_and_renames(tmp_path):
    target = str(tmp_path / "app")
    # atomic_link keeps the -o in the command; the rewriter redirects it to temp.
    cmd = ["sh", "-c", 'printf %s "linked" > "$2"', "seed", "-o", target]
    rc = asyncio.run(atomic_link_async(None, target, cmd))
    assert rc == 0
    with open(target) as f:
        assert f.read() == "linked"
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == [], leftovers


@pytest.mark.skipif(not HAS_FCNTL, reason="requires fcntl/flock")
def test_flock_acquire_nonblocking_false_on_contention(tmp_path):
    import fcntl

    target = str(tmp_path / "x.o")
    ns = SimpleNamespace(verbose=0)
    sidecar = os.path.realpath(target) + ".lock"
    # A peer holds the sidecar exclusively via a distinct open file description.
    peer_fd = os.open(sidecar, os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(peer_fd, fcntl.LOCK_EX)
    try:
        lock = FlockLock(target, ns)
        assert lock.acquire_nonblocking() is False
        assert lock.fd is None  # no dangling fd on contention
    finally:
        fcntl.flock(peer_fd, fcntl.LOCK_UN)
        os.close(peer_fd)


@pytest.mark.skipif(not HAS_FCNTL, reason="requires fcntl/flock")
def test_flock_acquire_nonblocking_true_when_free(tmp_path):
    target = str(tmp_path / "x.o")
    lock = FlockLock(target, SimpleNamespace(verbose=0))
    assert lock.acquire_nonblocking() is True
    assert lock.fd is not None
    lock.release()
    assert lock.fd is None


def test_run_child_async_spawn_reap_and_returncode():
    spawned: list[int] = []
    reaped: list[int] = []
    rc = asyncio.run(
        locking._run_child_async(
            ["sh", "-c", "exit 7"],
            on_spawn=spawned.append,
            on_reap=reaped.append,
        )
    )
    assert rc == 7
    assert len(spawned) == 1 and isinstance(spawned[0], int)
    assert reaped == spawned  # reaped exactly the spawned pgid


# ---------------------------------------------------------------------------
# _async_acquire cancellation handshake: exactly one side releases, so a
# cancelled awaiter (sibling rule failed / build aborted) can never strand the
# eventually-acquired lock for the process lifetime.
# ---------------------------------------------------------------------------


class _SlowLock:
    """Deterministic stand-in for a blocking lock WITHOUT acquire_nonblocking
    (forces _async_acquire onto the executor path). ``acquire()`` parks on
    ``gate`` so the test controls exactly when the executor thread wins the
    lock relative to the awaiter's cancellation."""

    def __init__(self):
        import threading

        self.gate = threading.Event()  # acquire() blocks until set
        self.acquire_started = threading.Event()
        self.acquire_done = threading.Event()
        self._mutex = threading.Lock()
        self.releases = 0
        self.held = False

    def acquire(self):
        self.acquire_started.set()
        assert self.gate.wait(10), "test gate never opened"
        with self._mutex:
            self.held = True
        self.acquire_done.set()

    def release(self):
        with self._mutex:
            self.releases += 1
            self.held = False


def test_async_acquire_cancel_while_contended_releases_late_acquire():
    """Cancel the awaiter while the executor thread is still parked in
    acquire(); when the thread eventually wins the lock it must see the
    abandon flag and release it (the reviewed lock-strand bug)."""
    import time

    lock = _SlowLock()

    async def main():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(locking._async_acquire(lock))
        # Wait until the executor thread is parked inside acquire().
        await loop.run_in_executor(None, lock.acquire_started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Now the peer "releases": the thread's acquire() completes and the
        # handshake must release the abandoned lock, not strand it.
        lock.gate.set()
        deadline = time.time() + 10
        while lock.releases == 0 and time.time() < deadline:
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert lock.releases == 1
    assert not lock.held


def test_async_acquire_cancel_after_thread_acquired_releases():
    """Cancel after the executor thread recorded the acquire but before the
    awaiter resumed: the canceller side of the handshake must release."""
    lock = _SlowLock()
    lock.gate.set()  # acquire() completes as soon as the thread runs it

    async def main():
        task = asyncio.ensure_future(locking._async_acquire(lock))
        await asyncio.sleep(0)  # let the task reach the executor await
        # Block the LOOP thread until the executor thread has finished
        # acquire(); the task cannot resume while we hold the loop, so the
        # cancel below lands after acquired=True but before the awaiter runs.
        assert lock.acquire_done.wait(10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert lock.releases == 1
    assert not lock.held
