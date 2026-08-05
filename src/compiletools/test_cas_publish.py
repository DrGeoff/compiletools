"""Tests for the atomic publish helper that backs the CAS-exe symlink rule.

Covers I1 (atomic link+rename), I2 (EXDEV-only fallback, surface other
errors visibly), the C4 sidecar manifest write, and the publish/trim race:
publish serialises against ``ct-trim-cache`` on the ``<cas_path>.lock``
sidecar, so a concurrent trim can only ever produce one of two outcomes.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

import compiletools.cas_publish
import compiletools.locking
from compiletools.cas_publish import EXIT_CONCURRENT_TRIM, ConcurrentTrimError, publish


def _make_cas_entry(tmp_path, name="payload"):
    """Create a small file under tmp_path/cas/ that stands in for the CAS entry."""
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(exist_ok=True)
    p = cas_dir / name
    p.write_bytes(b"binary contents " + name.encode())
    return p


@pytest.fixture
def cas(tmp_path):
    """Default CAS entry shared by tests that don't need a custom name."""
    return _make_cas_entry(tmp_path)


@pytest.fixture
def user(tmp_path):
    """Default user-visible publish target shared by most tests."""
    return tmp_path / "bin" / "main"


class TestPublishHardlinkPath:
    def test_publish_creates_hardlink_at_user_path(self, cas, user):
        publish(str(cas), str(user))

        assert user.exists()
        assert user.read_bytes() == cas.read_bytes()
        # Same inode → hardlink (not symlink fallback).
        assert user.stat().st_ino == cas.stat().st_ino

    def test_publish_is_idempotent(self, cas, user):
        publish(str(cas), str(user))
        publish(str(cas), str(user))  # second time must succeed

        assert user.exists()
        assert user.stat().st_ino == cas.stat().st_ino


class TestPublishAtomicityVsConcurrency:
    def test_user_path_never_disappears_under_repeated_publish(self, tmp_path):
        """I1: with a Python helper using link+rename, ``user_path`` is
        always present for any concurrent reader. The prior shell recipe
        ``ln -f`` did ``unlink + link`` which had a window.
        """
        cas1 = _make_cas_entry(tmp_path, "v1")
        cas2 = _make_cas_entry(tmp_path, "v2")
        user = tmp_path / "bin" / "main"

        # Initial publish so user_path exists.
        publish(str(cas1), str(user))
        assert user.exists()

        observations: list[bool] = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                observations.append(os.path.lexists(str(user)))

        def thrasher():
            for i in range(50):
                publish(str(cas1 if i % 2 == 0 else cas2), str(user))

        t_w = threading.Thread(target=watcher)
        t_t = threading.Thread(target=thrasher)
        t_w.start()
        t_t.start()
        t_t.join()
        stop.set()
        t_w.join()

        # Watcher should never have observed user_path missing.
        assert all(observations), (
            "I1: user_path was observed missing during a re-publish — link+rename should be atomic"
        )


@pytest.mark.skipif(
    not hasattr(os, "link"),
    reason="platform lacks os.link (e.g. Termux/Android); the EXDEV branch can't be exercised",
)
class TestPublishExdevFallback:
    def test_exdev_falls_back_to_symlink(self, cas, user, monkeypatch):
        """I2: cross-filesystem EXDEV from os.link must fall back to symlink."""
        original_link = os.link

        def fake_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            raise OSError(errno.EXDEV, "Cross-device link")

        monkeypatch.setattr(os, "link", fake_link)
        publish(str(cas), str(user))

        assert user.is_symlink()
        assert os.readlink(str(user)) == str(cas)

        # Cleanup so monkeypatch tear-down doesn't fight the test.
        monkeypatch.setattr(os, "link", original_link)

    def test_non_exdev_errors_propagate(self, cas, user, monkeypatch):
        """I2: a non-EXDEV OSError (ENOSPC / EPERM / EACCES / EROFS) MUST
        surface — silent symlink degradation here was the bug.
        """

        def fake_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "link", fake_link)
        with pytest.raises(OSError) as excinfo:
            publish(str(cas), str(user))
        assert excinfo.value.errno == errno.ENOSPC


class TestSidecarManifest:
    def test_manifest_written_with_source_realpath(self, cas, user):
        """C4 sidecar: trim_exedir reads <cas>.manifest to bucket entries
        by source identity instead of by basename.
        """
        publish(str(cas), str(user), source_realpath="/repo/tests/main.cpp")

        manifest_path = str(cas) + ".manifest"
        assert os.path.exists(manifest_path)
        with open(manifest_path) as f:
            data = json.load(f)
        assert data == {"source_realpath": "/repo/tests/main.cpp"}

    def test_no_manifest_when_source_realpath_absent(self, cas, user):
        publish(str(cas), str(user))

        assert not os.path.exists(str(cas) + ".manifest")

    def test_manifest_failure_is_non_fatal(self, cas, user, monkeypatch):
        """A read-only cas-dir must not turn the publish step itself
        into a failure — sidecar is best-effort.
        """
        original_open = open

        def hostile_open(path, *args, **kwargs):
            if str(path).endswith(".manifest"):
                raise PermissionError("read-only cas dir")
            return original_open(path, *args, **kwargs)

        # Monkeypatch the builtin open ONLY inside cas_publish.
        import compiletools.cas_publish as cp

        monkeypatch.setattr(cp, "open", hostile_open, raising=False)
        publish(str(cas), str(user), source_realpath="/repo/tests/main.cpp")

        # Publish itself succeeded.
        assert user.exists()
        # Manifest absent because the write failed silently.
        assert not os.path.exists(str(cas) + ".manifest")


# ---------------------------------------------------------------------------
# Publish vs concurrent trim
#
# The trim side of every race test below runs in a SUBPROCESS, never a thread.
# POSIX fcntl record locks (the strategy filesystem_utils selects for GPFS,
# where the production CAS pools live) are per-process: two threads of one
# process never contend, so a thread-based race would pass whether or not
# publish takes the lock at all.
# ---------------------------------------------------------------------------

_TRIM_CHILD_SOURCE = '''\
"""Trim side of the publish/trim race tests.

argv: <pool> <coord> <iterations> <window_seconds>

Per iteration: wait for ``<coord>/go.<i>``, run one real ``trim_exedir`` pass
over the pool, write ``<coord>/done.<i>``. ``window_seconds`` widens the gap
between trim's under-lock ``st_nlink`` re-check and its ``os.remove`` so the
publish side gets a real chance to interleave; ``<coord>/window.<i>`` is
written on entry to that widened gap, i.e. while the entry lock is HELD.
"""

import json
import os
import sys
import time
import types

import compiletools.trim_cache as trim_cache

pool, coord = sys.argv[1], sys.argv[2]
iterations, window = int(sys.argv[3]), float(sys.argv[4])

_real_remove = os.remove
_state = {"i": 0}


def _slow_remove(path, *args, **kwargs):
    if str(path).endswith(".exe"):
        open(os.path.join(coord, "window.%d" % _state["i"]), "w").close()
        time.sleep(window)
    return _real_remove(path, *args, **kwargs)


os.remove = _slow_remove

# keep_count=0 / no max_age: maximally aggressive, so every entry in the
# pool is evicted unless something actively protects it. The only protection
# in play here is the hard link a successful publish creates.
trim_args = types.SimpleNamespace(
    dry_run=False, verbose=0, keep_count=0, max_age=None,
    max_size_bytes=None, parallel=1, json=False,
)

records = []
for i in range(iterations):
    _state["i"] = i
    go = os.path.join(coord, "go.%d" % i)
    deadline = time.monotonic() + 120.0
    while not os.path.exists(go):
        if time.monotonic() > deadline:
            raise SystemExit("trim child timed out waiting for %s" % go)
        time.sleep(0.001)
    stats = trim_cache.CacheTrimmer(trim_args).trim_exedir(pool)
    records.append({"i": i, "removed": stats["removed"], "scanned": stats["total_scanned"]})
    open(os.path.join(coord, "done.%d" % i), "w").close()

with open(os.path.join(coord, "child.json"), "w") as f:
    json.dump(records, f)
'''


def _touch_exe(exedir, basename, link_key, *, age_seconds=0, payload=b"\0" * 1024):
    """Create a cas-exedir entry at ``<exedir>/<linkkey[:2]>/<basename>_<linkkey>.exe``.

    Mirrors the production layout ``trim_cache._split_exe_leaf_name`` parses.
    """
    bucket_dir = os.path.join(exedir, link_key[:2])
    os.makedirs(bucket_dir, exist_ok=True)
    path = os.path.join(bucket_dir, f"{basename}_{link_key}.exe")
    with open(path, "wb") as f:
        f.write(payload)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


def _seed_evictable_entry(pool, payload):
    """Seed the pool with one entry the trim child will try to evict."""
    return _touch_exe(pool, "main", "aa11" * 16, payload=payload)


def _wait_for(path, child, timeout=120.0):
    """Block until ``path`` appears; fail fast if the trim child died first."""
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if child.poll() is not None:
            raise AssertionError(f"trim child exited ({child.returncode}) while waiting for {path}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.001)


class _TrimChild:
    """A running trim-side subprocess plus its coordination directory."""

    def __init__(self, proc, coord):
        self.proc = proc
        self.coord = coord

    def go(self, i):
        (self.coord / f"go.{i}").touch()

    def await_window(self, i):
        _wait_for(str(self.coord / f"window.{i}"), self.proc)

    def await_done(self, i):
        _wait_for(str(self.coord / f"done.{i}"), self.proc)

    def finish(self):
        assert self.proc.wait(timeout=120) == 0, "trim child failed"
        with open(self.coord / "child.json") as f:
            return json.load(f)


@pytest.fixture
def trim_child(tmp_path):
    """Factory spawning the trim-side subprocess for a pool."""
    script = tmp_path / "race_trim_child.py"
    script.write_text(_TRIM_CHILD_SOURCE)
    coord = tmp_path / "coord"
    coord.mkdir()
    spawned = []

    def spawn(pool, *, iterations=1, window=0.0):
        proc = subprocess.Popen([sys.executable, str(script), str(pool), str(coord), str(iterations), str(window)])
        child = _TrimChild(proc, coord)
        spawned.append(child)
        return child

    yield spawn

    for child in spawned:
        if child.proc.poll() is None:
            child.proc.kill()
            child.proc.wait()


@pytest.mark.skipif(not hasattr(os, "link"), reason="platform lacks os.link; publish cannot raise nlink above 1")
class TestPublishVersusConcurrentTrim:
    def test_published_entry_survives_a_later_trim(self, tmp_path, trim_child):
        """Outcome 1: publish first. The hard link it creates raises the cas
        entry's ``st_nlink`` to 2, which trim's hard-link protection honours —
        the entry is kept even though ``keep_count=1`` would otherwise evict it.
        """
        pool = tmp_path / "cas-exe"
        payload = b"outcome-one-payload"
        target = _seed_evictable_entry(str(pool), payload)
        user = tmp_path / "bin" / "main"
        child = trim_child(pool)

        publish(target, str(user))
        child.go(0)
        child.await_done(0)
        records = child.finish()

        assert records[0]["removed"] == 0, "trim must not evict a freshly published entry"
        assert os.path.exists(target)
        assert os.stat(target).st_nlink >= 2
        assert user.read_bytes() == payload

    def test_publish_blocks_on_the_entry_lock_and_reports_the_loss(self, tmp_path, trim_child):
        """Outcome 2: trim first, holding the entry lock across a deliberately
        widened window between its ``st_nlink`` re-check and its ``os.remove``.

        Publish must BLOCK on that lock rather than slipping its hard link into
        the window — so once the lock is released the entry is gone and publish
        reports the loss. Without the publish-side lock the link lands inside
        the window, publish returns success, and trim then deletes the entry it
        just published: this test is the one that goes red on that mutation.
        """
        pool = tmp_path / "cas-exe"
        target = _seed_evictable_entry(str(pool), b"outcome-two-payload")
        user = tmp_path / "bin" / "main"
        child = trim_child(pool, window=0.5)

        child.go(0)
        child.await_window(0)
        with pytest.raises(ConcurrentTrimError):
            publish(target, str(user))

        child.await_done(0)
        records = child.finish()
        assert records[0]["removed"] == 1, "trim held the lock first, so it must have evicted the entry"
        assert not os.path.exists(target)
        assert not user.exists(), "a lost publish must not leave a user-facing path behind"

    def test_race_loop_only_ever_produces_the_two_sanctioned_outcomes(self, tmp_path, trim_child):
        """Free race, repeated: publish and trim start together, with the
        publish side stepping its start offset across the trim side's window so
        the interleaving varies from iteration to iteration. Every iteration
        must land on exactly one of the two sanctioned outcomes, and a
        *successful* publish must always leave the cas entry alive with the
        published hard link on it.
        """
        iterations = 40
        pool = tmp_path / "cas-exe"
        child = trim_child(pool, iterations=iterations, window=0.004)
        outcomes = {"published": 0, "lost": 0}

        for i in range(iterations):
            payload = f"race-payload-{i}".encode()
            target = _seed_evictable_entry(str(pool), payload)
            user = tmp_path / "bin" / f"main.{i}"

            child.go(i)
            time.sleep((i % 8) * 0.001)
            try:
                publish(target, str(user))
                published = True
            except ConcurrentTrimError:
                published = False
            child.await_done(i)

            if published:
                outcomes["published"] += 1
                assert os.path.exists(target), (
                    f"iteration {i}: publish reported success but trim evicted the cas entry — "
                    "the publish-side entry lock is not serialising against trim"
                )
                assert os.stat(target).st_nlink >= 2, f"iteration {i}: published entry lost its hard link"
                assert user.read_bytes() == payload
            else:
                outcomes["lost"] += 1
                assert not os.path.exists(target), (
                    f"iteration {i}: publish reported a trim loss but the entry is present"
                )
                assert not user.exists(), f"iteration {i}: lost publish left a user-facing path behind"

            shutil.rmtree(pool)

        child.finish()
        assert outcomes["published"] + outcomes["lost"] == iterations


class TestConcurrentTrimLossReporting:
    def test_missing_cas_entry_raises_concurrent_trim_error(self, tmp_path):
        """A vanished entry surfaces as the named error, never a raw
        FileNotFoundError out of os.link."""
        missing = str(tmp_path / "cas" / "already-trimmed")
        os.makedirs(os.path.dirname(missing))
        user = tmp_path / "bin" / "main"

        with pytest.raises(ConcurrentTrimError) as excinfo:
            publish(missing, str(user))

        assert missing in str(excinfo.value)
        assert not user.exists()

    def test_a_vanished_entry_is_reported_before_any_destination_work(self, tmp_path):
        """The existence re-check under the lock, not the ENOENT that os.link
        would eventually raise, is what reports the loss.

        Discriminated by making the destination unwritable: reaching
        atomic_replace at all would mkstemp there and report a PermissionError
        naming the destination, burying the real cause. Publishing must name
        the trim instead.
        """
        missing = str(tmp_path / "cas" / "already-trimmed")
        os.makedirs(os.path.dirname(missing))
        bindir = tmp_path / "bin"
        bindir.mkdir()
        os.chmod(bindir, 0o500)
        if os.access(bindir, os.W_OK):
            pytest.skip("destination stayed writable after chmod; cannot discriminate (running as root?)")

        try:
            with pytest.raises(ConcurrentTrimError) as excinfo:
                publish(missing, str(bindir / "main"))
        finally:
            os.chmod(bindir, 0o700)

        assert missing in str(excinfo.value)

    @pytest.mark.skipif(not hasattr(os, "link"), reason="platform lacks os.link; the ENOENT branch is unreachable")
    def test_enoent_from_link_maps_to_concurrent_trim_error(self, cas, user, monkeypatch):
        """Defence in depth for pools where the lock could not be taken: a trim
        landing between the existence check and the link still reports the same
        named loss, and leaves no orphan publish temp behind."""

        def vanishing_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            os.unlink(src)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(os, "link", vanishing_link)
        with pytest.raises(ConcurrentTrimError):
            publish(str(cas), str(user))

        assert not list(user.parent.glob("*.publish.tmp"))

    @pytest.mark.skipif(not hasattr(os, "link"), reason="platform lacks os.link; the ENOENT branch is unreachable")
    def test_enoent_with_the_cas_entry_present_is_not_a_trim_loss(self, cas, user, monkeypatch):
        """An ENOENT that is not the cas entry (a vanished destination dir, say)
        must surface as itself rather than being mislabelled a trim loss."""

        def failing_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(os, "link", failing_link)
        with pytest.raises(FileNotFoundError) as excinfo:
            publish(str(cas), str(user))

        assert not isinstance(excinfo.value, ConcurrentTrimError)

    def test_main_exits_with_the_distinct_code_and_names_the_cause(self, tmp_path, capsys):
        missing = str(tmp_path / "cas" / "already-trimmed")
        os.makedirs(os.path.dirname(missing))
        user = str(tmp_path / "bin" / "main")

        rc = compiletools.cas_publish.main(["--cas-path", missing, "--user-path", user])

        assert rc == EXIT_CONCURRENT_TRIM
        assert rc not in (0, 1, 2), "the loss code must be distinguishable from success/generic/usage"
        err = capsys.readouterr().err
        assert "concurrent trim" in err.lower()
        assert missing in err
        assert "rerun" in err.lower(), "the message must tell the user what to do"

    def test_main_exits_zero_on_a_normal_publish(self, cas, user, capsys):
        rc = compiletools.cas_publish.main(["--cas-path", str(cas), "--user-path", str(user)])

        assert rc == 0
        assert user.read_bytes() == cas.read_bytes()
        assert capsys.readouterr().err == ""


class TestCasEntryFreshening:
    def test_publish_freshens_the_cas_entry_mtime(self, cas, user):
        """Age-gated sweeps (``--max-age``, the oldest-first byte budget) read
        mtime, so an entry that is still being published must not look cold."""
        stale = time.time() - 45 * 86400
        os.utime(str(cas), (stale, stale))

        publish(str(cas), str(user))

        assert time.time() - os.stat(str(cas)).st_mtime < 60

    def test_freshening_failure_does_not_fail_the_publish(self, cas, user, monkeypatch):
        """A peer's entry on a shared pool is not ours to utime (EPERM) — the
        publish itself must still succeed."""

        def denied_utime(*args, **kwargs):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "utime", denied_utime)
        publish(str(cas), str(user))

        assert user.read_bytes() == cas.read_bytes()


class TestLockUnavailableFallback:
    def test_publish_proceeds_and_warns_when_the_entry_lock_is_unavailable(self, cas, user, monkeypatch, capsys):
        """A read-only or lock-hostile cas pool must not break every publish.
        The lock is race protection, not a precondition: warn and carry on.
        """

        class UnlockableFileLock:
            def __init__(self, target_file, args):
                pass

            def __enter__(self):
                raise OSError(errno.EROFS, "Read-only file system")

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        monkeypatch.setattr(compiletools.locking, "FileLock", UnlockableFileLock)
        publish(str(cas), str(user))

        assert user.read_bytes() == cas.read_bytes()
        assert "lock" in capsys.readouterr().err.lower()

    def test_a_transient_lock_error_is_not_swallowed(self, cas, user, monkeypatch):
        """Only "this pool cannot host a lock sidecar" errnos fall back. An EIO
        would otherwise be hidden behind a silently unlocked publish.
        """

        class FailingFileLock:
            def __init__(self, target_file, args):
                pass

            def __enter__(self):
                raise OSError(errno.EIO, "Input/output error")

            def __exit__(self, exc_type, exc_val, exc_tb):
                return False

        monkeypatch.setattr(compiletools.locking, "FileLock", FailingFileLock)
        with pytest.raises(OSError) as excinfo:
            publish(str(cas), str(user))

        assert excinfo.value.errno == errno.EIO
        assert not user.exists()
