"""Tests for filesystem_utils module."""

import os
import tempfile

from compiletools.filesystem_utils import (
    atomic_output_file,
    atomic_write,
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


def test_get_lock_strategy_gpfs():
    """GPFS should use lockdir strategy."""
    assert get_lock_strategy("gpfs") == "lockdir"


def test_get_lock_strategy_lustre():
    """Lustre should use lockdir strategy."""
    assert get_lock_strategy("lustre") == "lockdir"


def test_get_lock_strategy_nfs():
    """NFS should use lockdir strategy."""
    assert get_lock_strategy("nfs") == "lockdir"
    assert get_lock_strategy("nfs4") == "lockdir"


def test_get_lock_strategy_cifs():
    """CIFS/SMB should use cifs strategy."""
    assert get_lock_strategy("cifs") == "cifs"
    assert get_lock_strategy("smb") == "cifs"
    assert get_lock_strategy("smbfs") == "cifs"


def test_get_lock_strategy_local_filesystems():
    """Local filesystems should use flock."""
    assert get_lock_strategy("ext4") == "flock"
    assert get_lock_strategy("xfs") == "flock"
    assert get_lock_strategy("btrfs") == "flock"
    assert get_lock_strategy("tmpfs") == "flock"


def test_get_lock_strategy_unknown():
    """Unknown filesystems should default to flock."""
    assert get_lock_strategy("unknown") == "flock"


def test_supports_mmap_safely_problematic_filesystems():
    """Known problematic filesystems should return False."""
    assert supports_mmap_safely("gpfs") is False
    assert supports_mmap_safely("cifs") is False
    assert supports_mmap_safely("smb") is False
    assert supports_mmap_safely("smbfs") is False
    assert supports_mmap_safely("afs") is False


def test_supports_mmap_safely_safe_filesystems():
    """Known safe filesystems should return True."""
    assert supports_mmap_safely("ext4") is True
    assert supports_mmap_safely("xfs") is True
    assert supports_mmap_safely("btrfs") is True
    assert supports_mmap_safely("tmpfs") is True
    assert supports_mmap_safely("zfs") is True


def test_supports_mmap_safely_nfs():
    """NFS is questionable but currently treated as safe."""
    # NFS v4 usually works, but has had issues historically
    assert supports_mmap_safely("nfs") is True
    assert supports_mmap_safely("nfs4") is True


def test_supports_mmap_safely_unknown():
    """Unknown filesystems should be treated as safe."""
    assert supports_mmap_safely("unknown") is True


def test_get_lockdir_sleep_interval_lustre():
    """Lustre should have shortest sleep interval."""
    assert get_lockdir_sleep_interval("lustre") == 0.01


def test_get_lockdir_sleep_interval_nfs():
    """NFS should have longest sleep interval due to network latency."""
    assert get_lockdir_sleep_interval("nfs") == 0.1
    assert get_lockdir_sleep_interval("nfs4") == 0.1


def test_get_lockdir_sleep_interval_gpfs():
    """GPFS should use default middle-ground interval."""
    assert get_lockdir_sleep_interval("gpfs") == 0.05


def test_get_lockdir_sleep_interval_unknown():
    """Unknown filesystems should use default interval."""
    assert get_lockdir_sleep_interval("unknown") == 0.05


def test_case_insensitivity():
    """Filesystem type matching should be case-insensitive."""
    assert get_lock_strategy("GPFS") == "lockdir"
    assert get_lock_strategy("Gpfs") == "lockdir"
    assert supports_mmap_safely("CIFS") is False
    assert supports_mmap_safely("Cifs") is False


def test_atomic_write_basic(tmp_path):
    """atomic_write writes string content to file."""
    target = str(tmp_path / "out.txt")
    atomic_write(target, "hello world")
    assert open(target).read() == "hello world"


def test_atomic_write_binary(tmp_path):
    """atomic_write with binary=True writes bytes."""
    target = str(tmp_path / "out.bin")
    atomic_write(target, b"\x00\x01\x02", binary=True)
    assert open(target, "rb").read() == b"\x00\x01\x02"


def test_atomic_write_preserves_permissions(tmp_path):
    """atomic_write preserves existing file permissions."""
    import os
    import stat

    target = str(tmp_path / "out.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o644)
    atomic_write(target, "new")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o644
    assert open(target).read() == "new"


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
    assert open(target).read() == "context content"


def test_atomic_output_file_exception_cleans_up(tmp_path):
    """atomic_output_file cleans up temp file on exception."""
    import os

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
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise FileNotFoundError("no /proc/mounts")
        return real_open(path, *args, **kwargs)

    # Clear cache so we get a fresh call
    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    # Also mock subprocess to return something
    import subprocess

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
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise FileNotFoundError("no /proc/mounts")
        return real_open(path, *args, **kwargs)

    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    import subprocess

    def fake_run(cmd, *args, **kwargs):
        raise OSError("no stat")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fstype = get_filesystem_type("/tmp/test_unknown_path")
    assert fstype == "unknown"
    get_filesystem_type.cache_clear()


def test_get_filesystem_type_stat_nonzero_returncode(monkeypatch):
    """Returns 'unknown' when stat command fails with non-zero return code."""
    import builtins

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    get_filesystem_type.cache_clear()
    monkeypatch.setattr(builtins, "open", fake_open)

    import subprocess

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
    assert open(target).read() == "hello"


def test_atomic_write_binary_with_str_content(tmp_path):
    """atomic_write binary=True with str content encodes to UTF-8."""
    target = str(tmp_path / "out.bin")
    atomic_write(target, "hello", binary=True)
    assert open(target, "rb").read() == b"hello"


def test_atomic_write_text_with_bytes_content(tmp_path):
    """atomic_write binary=False with bytes content writes bytes directly."""
    target = str(tmp_path / "out.txt")
    atomic_write(target, b"raw bytes", binary=False)
    assert open(target, "rb").read() == b"raw bytes"


def test_atomic_write_no_preserve_permissions(tmp_path):
    """atomic_write with preserve_permissions=False skips permission copy."""
    import os

    target = str(tmp_path / "out.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o755)
    atomic_write(target, "new", preserve_permissions=False)
    assert open(target).read() == "new"


def test_atomic_write_error_cleanup(tmp_path, monkeypatch):
    """atomic_write cleans up temp file on write error."""
    import os

    target = str(tmp_path / "fail.txt")

    # Monkey-patch os.write to fail after fd is opened
    orig_write = os.write

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
    import glob
    temps = glob.glob(str(tmp_path / ".tmp.*"))
    assert len(temps) == 0


def test_atomic_output_file_binary_mode(tmp_path):
    """atomic_output_file works in binary mode."""
    target = str(tmp_path / "out.bin")
    with atomic_output_file(target, mode="wb") as f:
        f.write(b"\x00\x01\x02")
    assert open(target, "rb").read() == b"\x00\x01\x02"


def test_atomic_output_file_creates_directory(tmp_path):
    """atomic_output_file creates parent directory if missing."""
    target = str(tmp_path / "newdir" / "out.txt")
    with atomic_output_file(target) as f:
        f.write("content")
    assert open(target).read() == "content"


def test_atomic_output_file_preserves_permissions(tmp_path):
    """atomic_output_file preserves existing file permissions."""
    import os
    import stat

    target = str(tmp_path / "perm.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o600)

    with atomic_output_file(target) as f:
        f.write("new")

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600
    assert open(target).read() == "new"


def test_atomic_output_file_exception_cleans_up_binary(tmp_path):
    """atomic_output_file cleans up temp file on exception in binary mode."""
    import os

    target = str(tmp_path / "fail.bin")
    try:
        with atomic_output_file(target, mode="wb") as f:
            f.write(b"partial")
            raise RuntimeError("deliberate")
    except RuntimeError:
        pass
    assert not os.path.exists(target)



def test_real_filesystem_detection():
    """Integration test: detect actual filesystem type."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fstype = get_filesystem_type(tmpdir)
        assert isinstance(fstype, str)
        assert len(fstype) > 0

        # Verify policy functions work with detected type
        strategy = get_lock_strategy(fstype)
        assert strategy in ["lockdir", "cifs", "flock"]

        mmap_safe = supports_mmap_safely(fstype)
        assert isinstance(mmap_safe, bool)

        interval = get_lockdir_sleep_interval(fstype)
        assert isinstance(interval, float)
        assert interval > 0
