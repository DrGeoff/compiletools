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
    _PUBLISH_TMP_SUFFIX,
    EXE_MANIFEST_SUFFIX,
    EXE_RESULT_SUFFIX,
    EXE_XML_SUFFIX,
    atomic_copy_bytes,
    atomic_output_file,
    atomic_write,
    atomic_write_if_changed,
    build_test_sidecar_path,
    compute_test_sidecar_key,
    get_filesystem_type,
    get_lock_strategy,
    get_lockdir_sleep_interval,
    match_exe_sidecar,
    match_mountpoint,
    parse_mount_lines,
    safe_read_text_file,
    secure_publish_copy,
    should_parallelize_scan,
    supports_mmap_safely,
    unescape_mount_field,
)


@pytest.fixture(autouse=True)
def _clear_filesystem_type_cache():
    """Reset get_filesystem_type's lru_cache so each test sees a fresh probe."""
    get_filesystem_type.cache_clear()
    return


def _patch_proc_mounts_unavailable(monkeypatch, exc):
    """Make ``open("/proc/mounts", ...)`` raise ``exc``; other paths pass through."""
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/mounts":
            raise exc
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


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


@pytest.mark.parametrize(
    "fstype",
    [
        pytest.param("gpfs", id="gpfs"),
        pytest.param("GPFS", id="gpfs-uppercase"),
        pytest.param("lustre", id="lustre"),
        pytest.param("nfs", id="nfs"),
        pytest.param("nfs4", id="nfs4"),
        pytest.param("cifs", id="cifs"),
        pytest.param("smb", id="smb"),
        pytest.param("smbfs", id="smbfs"),
        pytest.param("panfs", id="panfs"),
        pytest.param("beegfs", id="beegfs"),
    ],
)
def test_should_parallelize_scan_true_for_high_latency_filesystems(fstype):
    """Cluster / network filesystems have high per-stat metadata latency
    that parallelizes well — the trim scan should fan out across threads."""
    assert should_parallelize_scan(fstype) is True


@pytest.mark.parametrize(
    "fstype",
    [
        pytest.param("ext4", id="ext4"),
        pytest.param("xfs", id="xfs"),
        pytest.param("btrfs", id="btrfs"),
        pytest.param("tmpfs", id="tmpfs"),
        pytest.param("zfs", id="zfs"),
        pytest.param("overlay", id="overlay"),
        # Unknown must stay serial: we can't confirm parallel stat helps, so
        # preserve the historical single-threaded behavior (no thread overhead
        # on the overwhelming majority of machines, which are local-disk).
        pytest.param("unknown", id="unknown"),
    ],
)
def test_should_parallelize_scan_false_for_local_filesystems(fstype):
    """Local-disk filesystems get no benefit from parallel stat (page cache
    + low latency) and must stay single-threaded to avoid thread overhead."""
    assert should_parallelize_scan(fstype) is False


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
    _patch_proc_mounts_unavailable(monkeypatch, FileNotFoundError("no /proc/mounts"))

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


def test_get_filesystem_type_all_fallbacks_fail(monkeypatch):
    """Returns 'unknown' when all detection methods fail."""
    _patch_proc_mounts_unavailable(monkeypatch, FileNotFoundError("no /proc/mounts"))

    def fake_run(cmd, *args, **kwargs):
        raise OSError("no stat")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fstype = get_filesystem_type("/tmp/test_unknown_path")
    assert fstype == "unknown"


def test_get_filesystem_type_stat_nonzero_returncode(monkeypatch):
    """Returns 'unknown' when stat command fails with non-zero return code."""
    _patch_proc_mounts_unavailable(monkeypatch, PermissionError("denied"))

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


@pytest.fixture
def restore_umask():
    """Save and restore process umask around tests that exercise it."""
    saved = os.umask(0)
    os.umask(saved)
    yield
    os.umask(saved)


_UMASK_MODE_CASES = [
    (0o000, 0o666),
    (0o002, 0o664),
    (0o022, 0o644),
    (0o077, 0o600),
]


@pytest.mark.parametrize("umask_value, expected_mode", _UMASK_MODE_CASES)
def test_atomic_write_first_create_respects_umask(tmp_path, restore_umask, umask_value, expected_mode):
    """First-create atomic_write must produce umask-derived mode, not mkstemp's 0o600."""
    os.umask(umask_value)
    target = str(tmp_path / "first.txt")
    atomic_write(target, "hello")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == expected_mode


@pytest.mark.parametrize("umask_value, expected_mode", _UMASK_MODE_CASES)
def test_atomic_output_file_first_create_respects_umask(tmp_path, restore_umask, umask_value, expected_mode):
    """First-create atomic_output_file must produce umask-derived mode, not mkstemp's 0o600."""
    os.umask(umask_value)
    target = str(tmp_path / "first.txt")
    with atomic_output_file(target) as f:
        f.write("hello")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == expected_mode


def test_atomic_write_no_preserve_permissions_respects_umask(tmp_path, restore_umask):
    """preserve_permissions=False over an existing restrictive file resets to umask-derived mode."""
    os.umask(0o022)
    target = str(tmp_path / "out.txt")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o600)
    atomic_write(target, "new", preserve_permissions=False)
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o644


@pytest.mark.parametrize("umask_value", [0o000, 0o022, 0o077])
def test_atomic_output_file_force_mode_defeats_umask_on_first_create(tmp_path, restore_umask, umask_value):
    """force_mode must land exactly, regardless of the creator's umask."""
    os.umask(umask_value)
    target = str(tmp_path / "shared.json")
    with atomic_output_file(target, force_mode=0o666) as f:
        f.write("{}")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o666


def test_atomic_output_file_force_mode_overrides_preserved_restrictive_mode(tmp_path, restore_umask):
    """force_mode must repair a restrictive mode left by an earlier umask-dependent create."""
    os.umask(0o022)
    target = str(tmp_path / "shared.json")
    with open(target, "w") as f:
        f.write("old")
    os.chmod(target, 0o600)
    with atomic_output_file(target, force_mode=0o666) as f:
        f.write("new")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o666


class TestMountFieldUnescaping:
    """proc(5) mangles four characters in the mountpoint field. Decoding only
    ``\\040`` left the other three literal, so a mountpoint containing a tab,
    a newline or a backslash never matched a realpath and its filesystem was
    reported ``unknown`` -- silently selecting the wrong lock strategy on the
    NFS/CIFS/GPFS branches that consume this."""

    @pytest.mark.parametrize(
        ("escaped", "decoded"),
        [
            ("/mnt/two\\040words", "/mnt/two words"),
            ("/mnt/two\\011words", "/mnt/two\twords"),
            ("/mnt/two\\012words", "/mnt/two\nwords"),
            ("/mnt/two\\134words", "/mnt/two\\words"),
        ],
        ids=["space", "tab", "newline", "backslash"],
    )
    def test_each_proc5_escape_decodes(self, escaped, decoded):
        assert unescape_mount_field(escaped) == decoded

    def test_an_escaped_backslash_is_not_decoded_twice(self):
        r"""A mountpoint literally named ``/mnt/\040`` is written by the kernel
        as ``/mnt/\134040``. Any implementation that runs one str.replace per
        escape decodes the ``040`` tail a second time and yields ``/mnt/ ``,
        whichever order the replaces run in. The single left-to-right pass
        consumes all four characters of ``\134`` and resumes past them."""
        assert unescape_mount_field("/mnt/\\134040") == "/mnt/\\040"

    def test_a_bare_backslash_run_is_left_alone(self):
        """Only backslash + exactly three octal digits is an escape; \\8 and
        \\04 are not, and must survive verbatim rather than raising."""
        assert unescape_mount_field("/mnt/\\8\\04x") == "/mnt/\\8\\04x"

    def test_the_device_field_is_not_confused_for_the_mountpoint(self):
        """Field 1 is the device, field 2 the mountpoint. Both are mangled, so
        a device name that looks like a path must not be picked up."""
        line = "/dev/sda1 /mnt/two\\040words ext4 rw,relatime 0 0"
        assert parse_mount_lines([line]) == [("/mnt/two words", "ext4")]

    def test_short_lines_are_skipped_not_fatal(self):
        assert parse_mount_lines(["", "garbage\n", "/dev/sda1 /mnt ext4 rw 0 0"]) == [("/mnt", "ext4")]


class TestMountpointMatching:
    _MOUNTS = (
        ("/", "ext4"),
        ("/data", "gpfs"),
        ("/data2", "nfs"),
        ("/data/nested", "cifs"),
    )

    def test_a_sibling_mount_sharing_a_name_prefix_wins_on_its_own(self):
        assert match_mountpoint("/data2/file.o", self._MOUNTS) == "nfs"
        assert match_mountpoint("/data/file.o", self._MOUNTS) == "gpfs"

    def test_a_prefix_sibling_that_is_not_itself_a_mount_falls_through(self):
        """The case the ``trimmed + "/"`` boundary actually decides. When the
        sibling IS a mount (``/data2`` above) the longest-first sort reaches it
        before ``/data``, so a bare startswith gives the right answer by
        accident and that test cannot see the boundary at all -- verified by
        mutation. Here ``/database`` is not a mount, so nothing shadows the
        comparison: without the boundary it matches ``/data`` and reports gpfs,
        selecting the GPFS fcntl lock strategy for a path that is really on
        ext4."""
        mounts = [("/", "ext4"), ("/data", "gpfs")]
        assert match_mountpoint("/database/file.o", mounts) == "ext4"

    def test_the_most_specific_mountpoint_wins(self):
        assert match_mountpoint("/data/nested/file.o", self._MOUNTS) == "cifs"

    def test_the_mountpoint_itself_matches_exactly(self):
        assert match_mountpoint("/data", self._MOUNTS) == "gpfs"

    def test_root_catches_what_nothing_else_does(self):
        assert match_mountpoint("/usr/lib/x.so", self._MOUNTS) == "ext4"

    def test_no_match_returns_none_rather_than_a_guess(self):
        assert match_mountpoint("/data/file.o", [("/other", "xfs")]) is None

    def test_an_escaped_mountpoint_matches_the_real_path(self):
        """End to end over the two helpers: the realpath the caller holds is
        unescaped, so the comparison only lands if parsing decoded first."""
        mounts = parse_mount_lines(["/dev/sdb1 /mnt/two\\040words nfs rw 0 0"])
        assert match_mountpoint("/mnt/two words/obj.o", mounts) == "nfs"


class TestSecurePublishCopy:
    """``secure_publish_copy`` is the deposit path for workspace -> CAS
    artefacts (a shared, peer-writable directory). It must resist a peer
    pre-implanting a symlink at the guessable/reused temp name that older
    helpers relied on, and must let two peers depositing the same src at
    the same dst both succeed without ENOENT."""

    def test_control_uncontended_copy_produces_identical_bytes(self, tmp_path):
        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite>pass</testsuite>")
        dst = tmp_path / "cas" / "e.xml"
        dst.parent.mkdir()

        secure_publish_copy(str(src), str(dst))

        assert dst.is_file()
        assert not dst.is_symlink()
        assert dst.read_bytes() == b"<testsuite>pass</testsuite>"
        assert list(dst.parent.glob("*.publish.tmp")) == []

    def test_symlink_implantation_does_not_write_through_or_publish_the_link(self, tmp_path):
        """Pre-create every temp name a naive hash-of-src helper would have
        used, as a symlink pointing at a victim file. A helper vulnerable
        to implantation (no O_EXCL|O_NOFOLLOW on a freshly-created path, or
        one that reopens a pathname after creating it) either writes the
        deposited bytes through the symlink into the victim, or moves the
        symlink itself into dst. Neither may happen."""
        import hashlib

        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite>pass</testsuite>")
        cas_dir = tmp_path / "cas"
        cas_dir.mkdir()
        dst = cas_dir / "e.xml"
        victim = tmp_path / "victim_original.txt"
        victim.write_bytes(b"ORIGINAL")

        # The exact deterministic name the old hash-of-src helper used --
        # a peer who knows the workspace layout can precompute this.
        legacy_deterministic_tmp = cas_dir / f"e.xml.{hashlib.sha1(str(src).encode()).hexdigest()[:12]}.publish.tmp"
        legacy_deterministic_tmp.symlink_to(victim)

        secure_publish_copy(str(src), str(dst))

        assert victim.read_bytes() == b"ORIGINAL", "deposit wrote through the pre-implanted symlink"
        assert legacy_deterministic_tmp.is_symlink(), "the planted symlink itself must be left alone"
        assert dst.is_file()
        assert not dst.is_symlink(), "the published symlink itself was published instead of a regular file"
        assert dst.read_bytes() == b"<testsuite>pass</testsuite>"

    def test_symlink_implantation_defeats_the_naive_atomic_copy_bytes_helper(self, monkeypatch, tmp_path):
        """Demonstrates the FINDING A18/A21 failure mode BEFORE the fix:
        ``atomic_copy_bytes`` derives its temp name from ``dst`` alone and
        reopens that pathname (via ``shutil.copyfile``) after its O_EXCL
        placeholder is unlinked. A peer who wins the race between the
        unlink and the reopen can make that reopen follow a symlink into a
        victim file. This pins the defect atomic_copy_bytes has, which
        secure_publish_copy above is verified not to share."""
        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite>pass</testsuite>")
        cas_dir = tmp_path / "cas"
        cas_dir.mkdir()
        dst = cas_dir / "e.xml"
        victim = tmp_path / "victim_original.txt"
        victim.write_bytes(b"ORIGINAL")

        real_unlink = os.unlink

        def unlink_then_implant(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            if str(path).startswith(str(dst)) and str(path).endswith(".publish.tmp"):
                os.symlink(str(victim), path)

        monkeypatch.setattr(os, "unlink", unlink_then_implant)
        atomic_copy_bytes(str(src), str(dst))

        assert victim.read_bytes() == b"<testsuite>pass</testsuite>", (
            "expected atomic_copy_bytes to be vulnerable (write through the implanted "
            "symlink); if this fails, the helper this test documents as unsafe has "
            "changed and the comparison claim in secure_publish_copy's docstring is stale"
        )

    def test_concurrent_peers_depositing_the_same_src_both_succeed(self, tmp_path):
        """A5: two peer 'workspaces' racing the same src -> shared dst must
        not ENOENT each other. Runtime-random temp names never collide, so
        both copies run to completion and dst ends up a regular file with
        valid content from one of the two (content-identical) writers."""
        import threading

        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite>pass</testsuite>")
        dst = tmp_path / "cas" / "e.xml"
        dst.parent.mkdir()

        errors: list[BaseException] = []

        def deposit():
            try:
                secure_publish_copy(str(src), str(dst))
            except BaseException as exc:  # captured for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=deposit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert dst.is_file()
        assert not dst.is_symlink()
        assert dst.read_bytes() == b"<testsuite>pass</testsuite>"
        assert list(dst.parent.glob("*.publish.tmp")) == []

    def test_the_temp_path_carries_the_publish_tmp_suffix(self, monkeypatch, tmp_path):
        """``trim_cache.reclaim_orphan_temps`` matches purely on the
        ``.publish.tmp`` suffix, so a helper that changed the suffix would
        silently stop a killed-mid-copy temp from ever being reclaimed.
        Record the path the helper actually opens (the temp is unlinked by
        the rename, so it cannot be observed after the fact) and assert
        the suffix on that non-empty observation."""
        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite/>")
        dst = tmp_path / "cas" / "e.xml"
        dst.parent.mkdir()

        opened: list[str] = []
        real_open = os.open

        def recording_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", recording_open)
        secure_publish_copy(str(src), str(dst))

        assert dst.read_bytes() == b"<testsuite/>", "the deposit itself must still land"
        temps = [p for p in opened if p != str(dst)]
        assert temps, "helper opened no temp file; the atomic temp+rename contract is gone"
        assert not any(os.path.exists(p) for p in temps), "the temp must be renamed away, not left behind"
        assert all(p.endswith(_PUBLISH_TMP_SUFFIX) for p in temps), (
            f"temp names must end in {_PUBLISH_TMP_SUFFIX} for reclaim_orphan_temps to find them, got {temps}"
        )
        assert all(os.path.dirname(p) == str(dst.parent) for p in temps), (
            "the temp must sit beside dst so the rename is same-filesystem"
        )

    def test_a_failed_deposit_leaves_no_orphan_temp(self, tmp_path):
        """A deposit that raises mid-copy must clean up its own temp. Force
        an interruption with a directory src, so ``open(src, 'rb')`` raises
        after the temp exists. A MISSING src is a sanctioned no-op rather
        than an interruption -- see
        ``TestSecurePublishCopy::test_missing_src_warns_and_skips_rather_than_raising``."""
        unreadable_src = tmp_path / "a_directory_not_a_file"
        unreadable_src.mkdir()
        dst = tmp_path / "cas" / "e.xml"
        dst.parent.mkdir()

        with pytest.raises(IsADirectoryError):
            secure_publish_copy(str(unreadable_src), str(dst))

        assert not dst.exists()
        assert list(dst.parent.glob("*.publish.tmp")) == [], "a failed deposit must not leave an orphan temp behind"

    def test_in_process_shake_deposit_uses_the_secure_helper(self, monkeypatch, tmp_path):
        """A21: the in-process Shake path (trace_backend._record_test_outcome)
        must route through the same secure helper as the shell recipe, not
        the vulnerable atomic_copy_bytes."""
        import compiletools.filesystem_utils
        import compiletools.trace_backend as trace_backend_module

        calls: list[tuple[str, str]] = []

        def fake_secure_publish_copy(src, dst):
            calls.append((src, dst))
            Path(dst).write_bytes(Path(src).read_bytes())

        monkeypatch.setattr(compiletools.filesystem_utils, "secure_publish_copy", fake_secure_publish_copy)

        src = tmp_path / "workspace.xml"
        src.write_bytes(b"<testsuite>pass</testsuite>")
        dst = tmp_path / "cas" / "e.xml"
        dst.parent.mkdir()
        marker = tmp_path / "cas" / "e.result"

        rule = trace_backend_module.BuildRule(
            output=str(dst),
            inputs=[],
            command=["dummy"],
            rule_type="test",
            success_marker=str(marker),
            success_copies=((str(src), str(dst)),),
        )

        backend = trace_backend_module.ShakeBackend.__new__(trace_backend_module.ShakeBackend)
        backend._test_failures = []

        def fake_touch_result_marker(result_path: str) -> None:
            Path(result_path).touch()

        monkeypatch.setattr(backend, "_touch_result_marker", fake_touch_result_marker)

        trace_backend_module.ShakeBackend._record_test_outcome(backend, rule, 0, ["dummy"])

        assert calls == [(str(src), str(dst))]
        assert marker.exists()


class TestExeSidecarMatchAndStrip:
    """``match_exe_sidecar`` / ``compute_test_sidecar_key`` / ``build_test_sidecar_path`` —
    the single match-and-strip abstraction A12 layers the argv-keyed
    ``.result``/``.xml`` naming on top of (A8)."""

    def test_identity_sidecar_round_trips(self):
        """A ``.manifest`` (IDENTITY sidecar) strips bare-suffix-only —
        it carries no argv key."""
        matched = match_exe_sidecar(f"main_{'a' * 64}.exe{EXE_MANIFEST_SUFFIX}")
        assert matched == (f"main_{'a' * 64}.exe", EXE_MANIFEST_SUFFIX)

    def test_keyed_test_sidecar_strips_back_to_the_primary_exe(self):
        """A TEST sidecar built via ``build_test_sidecar_path`` strips back to
        exactly the primary exe leaf name ``build_test_sidecar_path`` was given —
        the trap-1/trap-3 guarantee this whole abstraction exists for."""
        primary = f"main_{'a' * 64}.exe"
        key = compute_test_sidecar_key(["valgrind"], "gtest", anchor_root="")
        keyed_name = os.path.basename(build_test_sidecar_path(primary, key, EXE_XML_SUFFIX))

        matched = match_exe_sidecar(keyed_name)

        assert matched == (primary, EXE_XML_SUFFIX)

    def test_legacy_unkeyed_test_sidecar_still_strips(self):
        """A pre-migration ``.result``/``.xml`` with no key segment (or a
        key-shaped-but-wrong-length trailer) must still strip bare-suffix
        -only, matching pre-A12 behaviour, so it is neither corrupted nor
        treated as belonging to a different primary."""
        primary = f"main_{'a' * 64}.exe"
        assert match_exe_sidecar(primary + EXE_RESULT_SUFFIX) == (primary, EXE_RESULT_SUFFIX)

    def test_non_sidecar_name_does_not_match(self):
        assert match_exe_sidecar("main.exe") is None
        assert match_exe_sidecar("main.exe.lock") is None

    def test_test_sidecar_path_rejects_identity_suffix(self):
        with pytest.raises(ValueError):
            build_test_sidecar_path("main.exe", "0" * 16, EXE_MANIFEST_SUFFIX)

    def test_different_testprefix_tokens_yield_different_keys(self):
        """(a)-supporting unit: the key alone must fork on TESTPREFIX."""
        key_none = compute_test_sidecar_key([], None, anchor_root="")
        key_valgrind = compute_test_sidecar_key(["valgrind", "--leak-check=full"], None, anchor_root="")

        assert key_none != key_valgrind

    def test_key_is_blind_to_workspace_path_but_not_to_semantics(self):
        """(e): the SAME logical TESTPREFIX token, canonicalised against two
        DIFFERENT gitroots, must hash to the SAME key -- proving the key is
        built from canonicalised (gitroot-relative) semantics, not the
        literal per-workspace argv, so cross-workspace CAS sharing survives.
        A control differing TESTPREFIX must still fork the key even across
        those same two workspaces."""
        key_workspace_a = compute_test_sidecar_key(["-B/ws/a/tool"], "gtest", anchor_root="/ws/a")
        key_workspace_b = compute_test_sidecar_key(["-B/ws/b/tool"], "gtest", anchor_root="/ws/b")
        assert key_workspace_a == key_workspace_b

        key_workspace_b_different_prefix = compute_test_sidecar_key(
            ["-B/ws/b/tool", "--extra"], "gtest", anchor_root="/ws/b"
        )
        assert key_workspace_b_different_prefix != key_workspace_a
