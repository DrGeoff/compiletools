"""Tests for filesystem_utils module."""

import builtins
import glob
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from compiletools.filesystem_utils import (
    atomic_output_file,
    atomic_write,
    atomic_write_if_changed,
    get_filesystem_type,
    get_lock_strategy,
    get_lockdir_sleep_interval,
    safe_read_text_file,
    supports_mmap_safely,
)


def test_get_filesystem_type_returns_string():
    """Filesystem type should always return a non-empty string."""
    fstype = get_filesystem_type("/")
    assert isinstance(fstype, str)
    assert len(fstype) > 0


def test_get_filesystem_type_for_tmp():
    """Test detection on /tmp which should exist on all systems."""
    fstype = get_filesystem_type("/tmp")
    assert isinstance(fstype, str)
    assert fstype != ""
    # Common tmpfs or local filesystems
    assert fstype in ["tmpfs", "ext4", "xfs", "btrfs", "zfs", "apfs", "unknown"]


def test_get_filesystem_type_caching():
    """Verify that filesystem type detection is cached."""
    path = "/tmp"
    result1 = get_filesystem_type(path)
    result2 = get_filesystem_type(path)
    # Should return same result
    assert result1 == result2


def test_get_filesystem_type_nonexistent_path():
    """Should handle nonexistent paths gracefully."""
    # Parent directory exists, so should return its filesystem type
    fstype = get_filesystem_type("/tmp/nonexistent_path_12345")
    assert isinstance(fstype, str)


@pytest.mark.parametrize(
    ("fstype", "expected"),
    [
        pytest.param("gpfs", "fcntl", id="gpfs"),
        pytest.param("GPFS", "fcntl", id="gpfs-uppercase"),
        pytest.param("Gpfs", "fcntl", id="gpfs-mixed-case"),
        pytest.param("lustre", "lockdir", id="lustre"),
        pytest.param("nfs", "lockdir", id="nfs"),
        pytest.param("nfs4", "lockdir", id="nfs4"),
        pytest.param("cifs", "cifs", id="cifs"),
        pytest.param("smb", "cifs", id="smb"),
        pytest.param("smbfs", "cifs", id="smbfs"),
        pytest.param("ext4", "flock", id="ext4"),
        pytest.param("xfs", "flock", id="xfs"),
        pytest.param("btrfs", "flock", id="btrfs"),
        pytest.param("tmpfs", "flock", id="tmpfs"),
        pytest.param("unknown", "flock", id="unknown"),
    ],
)
def test_get_lock_strategy(fstype, expected):
    assert get_lock_strategy(fstype) == expected


@pytest.mark.parametrize(
    ("fstype", "expected"),
    [
        pytest.param("gpfs", False, id="gpfs"),
        pytest.param("cifs", False, id="cifs"),
        pytest.param("CIFS", False, id="cifs-uppercase"),
        pytest.param("Cifs", False, id="cifs-mixed-case"),
        pytest.param("smb", False, id="smb"),
        pytest.param("smbfs", False, id="smbfs"),
        pytest.param("afs", False, id="afs"),
        pytest.param("ext4", True, id="ext4"),
        pytest.param("xfs", True, id="xfs"),
        pytest.param("btrfs", True, id="btrfs"),
        pytest.param("tmpfs", True, id="tmpfs"),
        pytest.param("zfs", True, id="zfs"),
        # NFS v4 usually works, but has had mmap issues historically.
        pytest.param("nfs", True, id="nfs"),
        pytest.param("nfs4", True, id="nfs4"),
        pytest.param("unknown", True, id="unknown"),
    ],
)
def test_supports_mmap_safely(fstype, expected):
    assert supports_mmap_safely(fstype) is expected


@pytest.mark.parametrize(
    ("fstype", "expected"),
    [
        pytest.param("lustre", 0.01, id="lustre"),
        pytest.param("nfs", 0.1, id="nfs"),
        pytest.param("nfs4", 0.1, id="nfs4"),
        pytest.param("gpfs", 0.05, id="gpfs-default"),
        pytest.param("unknown", 0.05, id="unknown-default"),
    ],
)
def test_get_lockdir_sleep_interval(fstype, expected):
    assert get_lockdir_sleep_interval(fstype) == expected


def test_atomic_write_basic(tmp_path):
    """atomic_write writes string content to file."""
    target = str(tmp_path / "out.txt")
    atomic_write(target, "hello world")
    assert Path(target).read_text() == "hello world"


def test_atomic_write_binary(tmp_path):
    """atomic_write with binary=True writes bytes."""
    target = str(tmp_path / "out.bin")
    atomic_write(target, b"\x00\x01\x02", binary=True)
    assert Path(target).read_bytes() == b"\x00\x01\x02"


def test_atomic_write_preserves_permissions(tmp_path):
    """atomic_write preserves existing file permissions."""
    target = str(tmp_path / "out.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o644)
    atomic_write(target, "new")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o644
    assert Path(target).read_text() == "new"


def test_safe_read_text_file(tmp_path):
    """safe_read_text_file reads file content."""
    target = str(tmp_path / "read.txt")
    with open(target, "w") as f:
        f.write("test content")
    result = safe_read_text_file(target)
    assert str(result) == "test content"


def test_atomic_output_file_basic(tmp_path):
    """atomic_output_file context manager writes atomically."""
    target = str(tmp_path / "ctx.txt")
    with atomic_output_file(target) as f:
        f.write("context content")
    assert Path(target).read_text() == "context content"


def test_atomic_output_file_exception_cleans_up(tmp_path):
    """atomic_output_file cleans up temp file on exception."""

    target = str(tmp_path / "fail.txt")
    try:
        with atomic_output_file(target) as f:
            f.write("partial")
            raise ValueError("deliberate")
    except ValueError:
        pass
    assert not os.path.exists(target)


def test_get_filesystem_type_proc_mounts_unavailable(monkeypatch):
    """Falls back when /proc/mounts is not available."""
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise FileNotFoundError("no /proc/mounts")
        return real_open(path, *args, **kwargs)

    # Clear cache so we get a fresh call
    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    # Also mock subprocess to return something
    orig_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "stat":

            class FakeResult:
                returncode = 0
                stdout = "ext4\n"

            return FakeResult()
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fstype = get_filesystem_type("/tmp/test_fallback_path")
    assert fstype == "ext4"
    get_filesystem_type.cache_clear()


def test_get_filesystem_type_all_fallbacks_fail(monkeypatch):
    """Returns 'unknown' when all detection methods fail."""
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise FileNotFoundError("no /proc/mounts")
        return real_open(path, *args, **kwargs)

    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    def fake_run(cmd, *args, **kwargs):
        raise OSError("no stat")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fstype = get_filesystem_type("/tmp/test_unknown_path")
    assert fstype == "unknown"
    get_filesystem_type.cache_clear()


def test_get_filesystem_type_stat_nonzero_returncode(monkeypatch):
    """Returns 'unknown' when stat command fails with non-zero return code."""
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    orig_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "stat":

            class FakeResult:
                returncode = 1
                stdout = ""

            return FakeResult()
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fstype = get_filesystem_type("/tmp/test_stat_fail_path")
    assert fstype == "unknown"
    get_filesystem_type.cache_clear()


def test_atomic_write_creates_directory(tmp_path):
    """atomic_write creates parent directory if it doesn't exist."""
    target = str(tmp_path / "subdir" / "deep" / "out.txt")
    atomic_write(target, "hello")
    assert Path(target).read_text() == "hello"


def test_atomic_write_binary_with_str_content(tmp_path):
    """atomic_write binary=True with str content encodes to UTF-8."""
    target = str(tmp_path / "out.bin")
    atomic_write(target, "hello", binary=True)
    assert Path(target).read_bytes() == b"hello"


def test_atomic_write_text_with_bytes_content(tmp_path):
    """atomic_write binary=False with bytes content writes bytes directly."""
    target = str(tmp_path / "out.txt")
    atomic_write(target, b"raw bytes", binary=False)
    assert Path(target).read_bytes() == b"raw bytes"


def test_atomic_write_no_preserve_permissions(tmp_path):
    """atomic_write with preserve_permissions=False skips permission copy."""

    target = str(tmp_path / "out.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o755)
    atomic_write(target, "new", preserve_permissions=False)
    assert Path(target).read_text() == "new"


def test_atomic_write_error_cleanup(tmp_path, monkeypatch):
    """atomic_write cleans up temp file on write error."""

    target = str(tmp_path / "fail.txt")

    # Monkey-patch os.write to fail after fd is opened

    def bad_write(fd, data):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", bad_write)

    try:
        atomic_write(target, "data")
    except OSError:
        pass

    # Target should not exist, and no temp files should remain
    assert not os.path.exists(target)
    # Check no temp files left
    temps = glob.glob(str(tmp_path / ".tmp.*"))
    assert len(temps) == 0


def test_atomic_output_file_binary_mode(tmp_path):
    """atomic_output_file works in binary mode."""
    target = str(tmp_path / "out.bin")
    with atomic_output_file(target, mode="wb") as f:
        f.write(b"\x00\x01\x02")
    assert Path(target).read_bytes() == b"\x00\x01\x02"


def test_atomic_output_file_creates_directory(tmp_path):
    """atomic_output_file creates parent directory if missing."""
    target = str(tmp_path / "newdir" / "out.txt")
    with atomic_output_file(target) as f:
        f.write("content")
    assert Path(target).read_text() == "content"


def test_atomic_output_file_preserves_permissions(tmp_path):
    """atomic_output_file preserves existing file permissions."""
    target = str(tmp_path / "perm.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o600)

    with atomic_output_file(target) as f:
        f.write("new")

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600
    assert Path(target).read_text() == "new"


def test_atomic_output_file_exception_cleans_up_binary(tmp_path):
    """atomic_output_file cleans up temp file on exception in binary mode."""

    target = str(tmp_path / "fail.bin")
    try:
        with atomic_output_file(target, mode="wb") as f:
            f.write(b"partial")
            raise RuntimeError("deliberate")
    except RuntimeError:
        pass
    assert not os.path.exists(target)


def test_real_filesystem_detection(tmp_path):
    """Integration test: detect actual filesystem type."""
    fstype = get_filesystem_type(str(tmp_path))
    assert isinstance(fstype, str)
    assert len(fstype) > 0

    # Verify policy functions work with detected type
    strategy = get_lock_strategy(fstype)
    assert strategy in ["fcntl", "lockdir", "cifs", "flock"]

    mmap_safe = supports_mmap_safely(fstype)
    assert isinstance(mmap_safe, bool)

    interval = get_lockdir_sleep_interval(fstype)
    assert isinstance(interval, float)
    assert interval > 0


def test_atomic_write_if_changed_skips_when_byte_identical(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello")
    initial_mtime_ns = target.stat().st_mtime_ns
    initial_inode = target.stat().st_ino
    time.sleep(0.01)  # widen the window

    wrote = atomic_write_if_changed(str(target), "hello")

    assert wrote is False
    assert target.stat().st_mtime_ns == initial_mtime_ns, "skipped write must not change mtime"
    assert target.stat().st_ino == initial_inode, "skipped write must not change inode"


def test_atomic_write_if_changed_writes_when_content_differs(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello")
    initial_inode = target.stat().st_ino

    wrote = atomic_write_if_changed(str(target), "world")

    assert wrote is True
    assert target.read_text() == "world"
    assert target.stat().st_ino != initial_inode, "atomic write replaces inode"


def test_atomic_write_if_changed_writes_when_target_absent(tmp_path):
    target = tmp_path / "f.txt"
    wrote = atomic_write_if_changed(str(target), "hello")
    assert wrote is True
    assert target.read_text() == "hello"
