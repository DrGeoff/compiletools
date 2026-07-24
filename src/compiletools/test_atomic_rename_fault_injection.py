"""Deterministic fault-injection tests for the CAS write->rename safety window.

These close the one real gap in compiletools' concurrency suite: the existing
live-race tests (``test_makefile_backend.test_concurrent_make_against_same_objdir``
and ``test_multiuser_cache.test_object_replacement_race``) exercise a SEQUENTIAL
producer, so a lockless reader is never caught mid-``os.replace`` of a CONCURRENT
producer. Here we *freeze* a producer deterministically in exactly that window
with a threading barrier/event and hammer the final path from a lockless reader,
mirroring how link rules read ``.o`` files with no lock at all.

The load-bearing protocol invariant (``locking.atomic_compile`` /
``atomic_link``): the compiler writes ``<target>.<pid>.<rand>.tmp`` then
``os.replace``s it into place. A lockless reader of the FINAL path must ALWAYS
observe the previous whole inode OR the new whole inode -- NEVER an empty or
partial one. Violation symptom in production: sporadic
``undefined reference to 'main'`` from a peer linker mmap-reading a half-written
``.o``.

Invariants covered (see src/compiletools/CLAUDE.md "Locking system"):
  * Reader safety across the write->rename window (the temp+rename invariant),
    parametrized over BOTH atomic_compile and atomic_link.
  * I4 -- the producer's temp file is unlinked BEFORE the lock is released
    (no window where a peer sees a stale temp between release and cleanup),
    tested on the compile-FAILURE path where the temp is not consumed by rename.
  * I3 -- multi-producer convergence: 2+ concurrent producers writing an
    IDENTICAL payload to the SAME final CAS path (unique pid/rand temps) leave a
    byte-identical survivor and no ``*.tmp`` residue.
  * The non-flock lock strategies (LockdirLock, FcntlLock) -- production
    HPC/NFS/GPFS users run these and they are never raced live today -- driven
    through the real ``atomic_compile`` temp+rename pipeline with a lockless
    reader.

Design: threads + real filesystem in ``tmp_path``, with the compiler subprocess
replaced by an in-process writer so the timing is fully deterministic and fast.
The lock objects, ``atomic_compile``/``atomic_link`` bodies, and the real
``os.replace`` rename are all exercised unmodified.
"""

import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

import compiletools.locking as locking
from compiletools.locking import (
    FcntlLock,
    FlockLock,
    LockdirLock,
    atomic_compile,
    atomic_link,
)

HAS_FCNTL = locking.fcntl is not None

# Distinct, multi-block payloads so an empty read (0 bytes) or a torn read
# (a truncated / mismatched prefix) is unambiguously distinguishable from
# either whole inode.
PREV = b"PREV" * 8192  # 32 KiB previous-good artefact
WHOLE = b"WHOLE" * 8192  # 40 KiB new artefact


def _make_lock_args(**overrides):
    """Minimal args object accepted by every lock strategy in locking.py."""
    defaults = dict(
        verbose=0,
        file_locking=True,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=0.005,
        sleep_interval_cifs=0.005,
        sleep_interval_flock_fallback=0.005,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _output_path_from_cmd(cmd):
    """Extract the ``-o <path>`` value that atomic_compile/atomic_link injects."""
    return cmd[cmd.index("-o") + 1]


def _fake_compiler(payload=WHOLE, returncode=0):
    """Return a stand-in for ``locking._run_with_signal_forwarding``.

    It writes *payload* to the ``-o`` output path (the temp file) exactly as a
    real compiler/linker would, then returns a CompletedProcess with the given
    returncode. No subprocess, no compiler dependency -- fully deterministic.
    """

    def _run(cmd, cwd=None, **kwargs):
        out = _output_path_from_cmd(cmd)
        # Write the whole payload in one syscall: a real compiler is of course
        # not atomic, but this stand-in must not itself be the source of a
        # partial read at the temp path -- the invariant under test is that the
        # FINAL path never exposes a partial, which is the rename's job.
        with open(out, "wb") as f:
            f.write(payload)
        return subprocess.CompletedProcess(cmd, returncode)

    return _run


class _Reader:
    """Lockless reader thread mirroring how link rules read ``.o`` files.

    Repeatedly reads the FINAL target path (no lock). Records every distinct
    observation category. A read of a nonexistent file (Absent) is acceptable;
    any read that returns bytes MUST equal a whole payload.
    """

    def __init__(self, target):
        self.target = target
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lockless-reader")
        self.violations = []  # (prefix, length) tuples for any bad read
        self.saw_absent = False
        self.saw_prev = False
        self.saw_whole = False
        self.read_count = 0

    def _run(self):
        while not self._stop.is_set():
            try:
                with open(self.target, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                # Absent is a legal state (old inode already gone / never
                # existed). It is NOT empty-bytes: an empty *existing* file
                # would be a torn artefact and is caught below.
                self.saw_absent = True
                continue
            self.read_count += 1
            if data == PREV:
                self.saw_prev = True
            elif data == WHOLE:
                self.saw_whole = True
            else:
                # Empty or partial content at the FINAL path == invariant broken.
                self.violations.append((data[:16], len(data)))

    def start(self):
        self._thread.start()

    def stop_and_join(self, timeout=10):
        self._stop.set()
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "reader thread failed to stop"


def _no_temp_residue(directory):
    return [p for p in os.listdir(directory) if p.endswith(".tmp")]


# ---------------------------------------------------------------------------
# 1. Reader safety across the write->rename window (temp+rename invariant),
#    over BOTH rename call sites (atomic_compile and atomic_link).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("atomic_rename_fault_injection")
@pytest.mark.parametrize("call_site", ["compile", "link"])
def test_reader_never_sees_partial_across_rename_window(tmp_path, monkeypatch, call_site):
    """Freeze a producer with the temp written but the rename not yet done;
    hammer the final path from a lockless reader; assert it only ever sees the
    previous whole inode -- never empty/partial -- and the new whole inode after
    the barrier releases."""
    target = str(tmp_path / "libmain.o")

    # Pre-seed a previous-good artefact so an errant empty/partial write is
    # distinguishable from BOTH acceptable states.
    with open(target, "wb") as f:
        f.write(PREV)

    monkeypatch.setattr(locking, "_run_with_signal_forwarding", _fake_compiler(WHOLE))

    rename_reached = threading.Event()
    release_rename = threading.Event()
    real_replace = os.replace

    def blocking_replace(src, dst):
        # We are now in the fault window: temp exists (whole), target still the
        # PREVIOUS whole inode. Signal the test, then stall until released.
        rename_reached.set()
        assert release_rename.wait(timeout=10), "test failed to release rename barrier"
        return real_replace(src, dst)

    monkeypatch.setattr(locking.os, "replace", blocking_replace)

    reader = _Reader(target)

    def produce():
        if call_site == "compile":
            atomic_compile(None, target, ["fake-cc", "src.cpp"])
        else:
            atomic_link(None, target, ["fake-cc", "obj.o", "-o", target])

    producer = threading.Thread(target=produce, name="producer")

    reader.start()
    producer.start()
    try:
        # Wait until the producer is parked in the rename window.
        assert rename_reached.wait(timeout=10), "producer never reached rename"
        # Spin the reader hard while the rename is frozen: every read here must
        # observe the PREVIOUS whole inode (rename has not happened yet).
        deadline_reads = reader.read_count + 200
        while reader.read_count < deadline_reads:
            if reader.violations:
                break
        assert reader.saw_prev, "reader never observed the previous whole inode"
        # Release the rename and let the new inode land.
        release_rename.set()
        producer.join(timeout=10)
        assert not producer.is_alive(), "producer did not finish"
        # Let the reader observe the post-rename state.
        deadline_reads = reader.read_count + 200
        while reader.read_count < deadline_reads and not reader.saw_whole:
            pass
    finally:
        release_rename.set()
        reader.stop_and_join()

    assert reader.violations == [], f"lockless reader saw empty/partial content at the final path: {reader.violations}"
    assert reader.saw_whole, "reader never observed the new whole inode after rename"
    with open(target, "rb") as f:
        assert f.read() == WHOLE, "final artefact is not the whole payload"
    assert _no_temp_residue(str(tmp_path)) == [], "temp file left behind after rename"


# ---------------------------------------------------------------------------
# 2. I4: the producer's temp is unlinked BEFORE the lock is released.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FCNTL, reason="requires fcntl/flock")
@pytest.mark.xdist_group("atomic_rename_fault_injection")
def test_temp_unlinked_before_lock_release_on_compile_failure(tmp_path, monkeypatch):
    """On a FAILED compile the temp is written but never renamed, so cleanup
    (not the rename) is what removes it. Assert that at the instant the lock is
    released there is ZERO ``*.tmp`` residue -- i.e. the temp is unlinked BEFORE
    release, leaving no window for a peer to observe a stale temp.

    The failure path is used deliberately: on the success path the rename
    consumes the temp, so I4 would hold vacuously. A non-zero compiler exit is
    the case that actually exercises ``_temp_under_lock``'s unlink-then-release
    ordering."""
    target = str(tmp_path / "fail.o")

    monkeypatch.setattr(
        locking,
        "_run_with_signal_forwarding",
        _fake_compiler(WHOLE, returncode=1),
    )

    lock = FlockLock(target, _make_lock_args())
    real_release = lock.release
    residue_at_release = {}

    def instrumented_release():
        # Snapshot temp residue at the *entry* to release -- before the fd is
        # dropped and any peer could take the lock.
        residue_at_release["tmp"] = _no_temp_residue(str(tmp_path))
        return real_release()

    monkeypatch.setattr(lock, "release", instrumented_release)

    with pytest.raises(subprocess.CalledProcessError):
        atomic_compile(lock, target, ["fake-cc", "src.cpp"])

    assert "tmp" in residue_at_release, "lock.release was never called"
    assert residue_at_release["tmp"] == [], (
        "stale temp file(s) still present at the moment the lock was released "
        f"(I4 violated): {residue_at_release['tmp']}"
    )
    assert _no_temp_residue(str(tmp_path)) == [], "temp residue after failed compile"
    assert not os.path.exists(target), "failed compile must not create the target"


# ---------------------------------------------------------------------------
# 3. I3: multi-producer convergence to a byte-identical survivor.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FCNTL, reason="requires fcntl/flock")
@pytest.mark.xdist_group("atomic_rename_fault_injection")
def test_multi_producer_convergence_identical_payload(tmp_path, monkeypatch):
    """Two+ concurrent producers write the IDENTICAL payload to the SAME final
    CAS path via unique pid/rand temps. Assert the survivor is byte-identical to
    the reference payload and no ``*.tmp`` residue remains, while a lockless
    reader concurrently observes only whole content."""
    target = str(tmp_path / "converge.o")

    # No pre-seed: reader may legitimately see Absent until the first rename.
    monkeypatch.setattr(locking, "_run_with_signal_forwarding", _fake_compiler(WHOLE))

    n_producers = 4
    start = threading.Barrier(n_producers)
    reader = _Reader(target)

    def produce():
        # Each producer builds its own FlockLock on the same target -> same
        # sidecar inode -> serialised; each generates its own pid/rand temp.
        lock = FlockLock(target, _make_lock_args())
        start.wait(timeout=10)
        atomic_compile(lock, target, ["fake-cc", "converge.cpp"])

    threads = [threading.Thread(target=produce, name=f"producer-{i}") for i in range(n_producers)]

    reader.start()
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), "a producer did not finish"
    finally:
        reader.stop_and_join()

    assert reader.violations == [], f"reader saw empty/partial content during multi-producer race: {reader.violations}"
    with open(target, "rb") as f:
        assert f.read() == WHOLE, "surviving artefact is not byte-identical to the payload"
    assert _no_temp_residue(str(tmp_path)) == [], "temp residue after multi-producer convergence"


# ---------------------------------------------------------------------------
# 4. Non-flock lock strategies raced through the real temp+rename pipeline.
#    Production HPC/NFS/GPFS users run these; never raced live today.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FCNTL, reason="requires fcntl/flock")
@pytest.mark.xdist_group("atomic_rename_fault_injection")
@pytest.mark.parametrize("lock_cls", [LockdirLock, FcntlLock])
def test_nonflock_strategies_reader_safety_under_race(tmp_path, monkeypatch, lock_cls):
    """Drive several concurrent producers through ``atomic_compile`` using the
    real LockdirLock (NFS/Lustre) / FcntlLock (GPFS) strategy classes, with a
    lockless reader hammering the final path. All producers write the identical
    payload. Assert the reader only ever sees Absent OR whole content, the
    survivor is byte-identical, and no ``*.tmp`` residue remains.

    Note: FcntlLock uses POSIX record locks which are per-*process*, so
    same-process threads do not serialise on it -- that is exactly why the
    temp+rename invariant (not the lock) is what must keep the reader safe, and
    this test asserts it does. LockdirLock's mkdir serialises across threads."""
    target = str(tmp_path / "hpc.o")
    with open(target, "wb") as f:
        f.write(PREV)

    monkeypatch.setattr(locking, "_run_with_signal_forwarding", _fake_compiler(WHOLE))

    n_producers = 3
    start = threading.Barrier(n_producers)
    reader = _Reader(target)

    def produce():
        lock = lock_cls(target, _make_lock_args())
        start.wait(timeout=10)
        atomic_compile(lock, target, ["fake-cc", "hpc.cpp"])

    threads = [threading.Thread(target=produce, name=f"hpc-producer-{i}") for i in range(n_producers)]

    reader.start()
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            assert not t.is_alive(), f"a {lock_cls.__name__} producer did not finish"
    finally:
        reader.stop_and_join()

    assert reader.violations == [], (
        f"lockless reader saw empty/partial content under {lock_cls.__name__}: {reader.violations}"
    )
    assert reader.saw_prev or reader.saw_whole or reader.saw_absent, "reader never read the target"
    with open(target, "rb") as f:
        assert f.read() == WHOLE, "final artefact is not the whole payload"
    # Lock strategies create their own sidecars (.lock / .lockdir / pid) which
    # are NOT .tmp; only producer temps match, and all must be gone.
    assert _no_temp_residue(str(tmp_path)) == [], f"temp residue after {lock_cls.__name__} race"
