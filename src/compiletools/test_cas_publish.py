"""Tests for the atomic publish helper that backs the CAS-exe symlink rule.

Covers I1 (atomic link+rename), I2 (EXDEV-only fallback, surface other
errors visibly), and the C4 sidecar manifest write.
"""

from __future__ import annotations

import errno
import json
import os
import threading

import pytest

from compiletools.cas_publish import publish


def _make_cas_entry(tmp_path, name="payload"):
    """Create a small file under tmp_path/cas/ that stands in for the CAS entry."""
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir(exist_ok=True)
    p = cas_dir / name
    p.write_bytes(b"binary contents " + name.encode())
    return p


class TestPublishHardlinkPath:
    def test_publish_creates_hardlink_at_user_path(self, tmp_path):
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

        publish(str(cas), str(user))

        assert user.exists()
        assert user.read_bytes() == cas.read_bytes()
        # Same inode → hardlink (not symlink fallback).
        assert user.stat().st_ino == cas.stat().st_ino

    def test_publish_is_idempotent(self, tmp_path):
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

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


class TestPublishExdevFallback:
    def test_exdev_falls_back_to_symlink(self, tmp_path, monkeypatch):
        """I2: cross-filesystem EXDEV from os.link must fall back to symlink."""
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

        original_link = os.link

        def fake_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            raise OSError(errno.EXDEV, "Cross-device link")

        monkeypatch.setattr(os, "link", fake_link)
        publish(str(cas), str(user))

        assert user.is_symlink()
        assert os.readlink(str(user)) == str(cas)

        # Cleanup so monkeypatch tear-down doesn't fight the test.
        monkeypatch.setattr(os, "link", original_link)

    def test_non_exdev_errors_propagate(self, tmp_path, monkeypatch):
        """I2: a non-EXDEV OSError (ENOSPC / EPERM / EACCES / EROFS) MUST
        surface — silent symlink degradation here was the bug.
        """
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

        def fake_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "link", fake_link)
        with pytest.raises(OSError) as excinfo:
            publish(str(cas), str(user))
        assert excinfo.value.errno == errno.ENOSPC


class TestSidecarManifest:
    def test_manifest_written_with_source_realpath(self, tmp_path):
        """C4 sidecar: trim_exedir reads <cas>.manifest to bucket entries
        by source identity instead of by basename.
        """
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

        publish(str(cas), str(user), source_realpath="/repo/tests/main.cpp")

        manifest_path = str(cas) + ".manifest"
        assert os.path.exists(manifest_path)
        with open(manifest_path) as f:
            data = json.load(f)
        assert data == {"source_realpath": "/repo/tests/main.cpp"}

    def test_no_manifest_when_source_realpath_absent(self, tmp_path):
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

        publish(str(cas), str(user))

        assert not os.path.exists(str(cas) + ".manifest")

    def test_manifest_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """A read-only cas-dir must not turn the publish step itself
        into a failure — sidecar is best-effort.
        """
        cas = _make_cas_entry(tmp_path)
        user = tmp_path / "bin" / "main"

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
