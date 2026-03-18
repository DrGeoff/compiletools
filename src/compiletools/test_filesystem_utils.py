"""Tests for filesystem_utils module."""

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
