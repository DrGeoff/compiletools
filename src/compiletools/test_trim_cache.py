"""Tests for trim_cache module."""

import json
import os
import time
import types

from compiletools.trim_cache import (
    CacheTrimmer,
    build_current_hash_set,
    parse_object_filename,
)

# ── parse_object_filename ────────────────────────────────────────────


class TestParseObjectFilename:
    def test_standard_filename(self):
        result = parse_object_filename("foo_aabbccddeeff_11223344556677_0011223344556677.o")
        assert result == ("foo", "aabbccddeeff", "11223344556677", "0011223344556677")

    def test_basename_with_underscores(self):
        result = parse_object_filename("my_cool_module_aabbccddeeff_11223344556677_0011223344556677.o")
        assert result is not None
        assert result[0] == "my_cool_module"
        assert result[1] == "aabbccddeeff"

    def test_single_char_basename(self):
        result = parse_object_filename("x_aabbccddeeff_11223344556677_0011223344556677.o")
        assert result is not None
        assert result[0] == "x"

    def test_not_an_object_file(self):
        assert parse_object_filename("random.o") is None
        assert parse_object_filename("foo.txt") is None
        assert parse_object_filename("not_an_object") is None

    def test_wrong_hash_lengths(self):
        # file_hash too short (11 instead of 12)
        assert parse_object_filename("foo_aabbccddee_11223344556677_0011223344556677.o") is None
        # dep_hash too short (13 instead of 14)
        assert parse_object_filename("foo_aabbccddeeff_1122334455667_0011223344556677.o") is None
        # macro_hash too short (15 instead of 16)
        assert parse_object_filename("foo_aabbccddeeff_11223344556677_001122334455667.o") is None

    def test_uppercase_hex_rejected(self):
        assert parse_object_filename("foo_AABBCCDDEEFF_11223344556677_0011223344556677.o") is None

    def test_non_hex_chars_rejected(self):
        assert parse_object_filename("foo_gghhiijjkkll_11223344556677_0011223344556677.o") is None


# ── build_current_hash_set ───────────────────────────────────────────


class TestBuildCurrentHashSet:
    def test_extracts_12_char_prefixes(self, monkeypatch):
        mock_context = types.SimpleNamespace(
            file_hashes={
                "/src/foo.cpp": "aabbccddeeff11223344556677889900aabbccdd",
                "/src/bar.cpp": "1122334455667788990011223344556677889900",
            },
            reverse_hashes={},
            hash_ops={"registry_hits": 0, "computed_hashes": 0},
        )
        monkeypatch.setattr(
            "compiletools.global_hash_registry.get_tracked_files",
            lambda ctx: ctx.file_hashes,
        )
        result = build_current_hash_set(mock_context)
        assert result == {"aabbccddeeff", "112233445566"}


# ── CacheTrimmer objdir ─────────────────────────────────────────────


def _make_args(**overrides):
    defaults = {
        "dry_run": False,
        "verbose": 0,
        "keep_count": 1,
        "max_age": None,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _touch_obj(objdir, basename, file_hash, dep_hash, macro_hash, *, age_seconds=0, size=1024):
    """Create a fake .o file in its sharded bucket dir, with controlled mtime/size.

    Mirrors production layout: ``<objdir>/<file_hash[:2]>/<basename>_<...>.o``.
    Sidecar lockdirs/lockfiles in the same bucket are managed by the lock
    subsystem, not this fixture — tests exercising lockdir behavior place
    them explicitly.
    """
    name = f"{basename}_{file_hash}_{dep_hash}_{macro_hash}.o"
    bucket_dir = os.path.join(objdir, file_hash[:2])
    os.makedirs(bucket_dir, exist_ok=True)
    path = os.path.join(bucket_dir, name)
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


class TestTrimObjdir:
    def test_keeps_current_files(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        current_hash = "aabbccddeeff"
        p = _touch_obj(objdir, "foo", current_hash, "11223344556677", "0011223344556677")

        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_objdir(objdir, {current_hash})

        assert os.path.exists(p)
        assert stats["current_kept"] == 1
        assert stats["removed"] == 0

    def test_removes_oldest_noncurrent(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        current_hash = "aabbccddeeff"

        old = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600)
        newer = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, {current_hash})

        assert not os.path.exists(old)
        assert os.path.exists(newer)
        assert stats["removed"] == 1
        assert stats["noncurrent_kept"] == 1

    def test_keeps_newest_noncurrent_per_basename(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600)
        newest = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, set())

        assert os.path.exists(newest)
        assert stats["noncurrent_kept"] == 1

    def test_keep_count_2(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        oldest = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=7200)
        middle = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=3600)
        newest = _touch_obj(objdir, "foo", "333333333333", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=2))
        stats = trimmer.trim_objdir(objdir, set())

        assert not os.path.exists(oldest)
        assert os.path.exists(middle)
        assert os.path.exists(newest)
        assert stats["removed"] == 1
        assert stats["noncurrent_kept"] == 2

    def test_max_age_interaction(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        # 2 days old -- beyond max_age of 1 day
        old = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=172800)
        # 1 hour old -- within max_age of 1 day
        recent = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=3600)
        # newest -- kept by keep_count=1
        newest = _touch_obj(objdir, "foo", "333333333333", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=1))
        trimmer.trim_objdir(objdir, set())

        assert not os.path.exists(old)
        assert os.path.exists(recent)  # within max_age, not removed
        assert os.path.exists(newest)  # kept by keep_count

    def test_safety_keeps_one_when_all_noncurrent(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        old = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=7200)
        newest = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        trimmer.trim_objdir(objdir, set())

        # Safety: at least 1 kept per basename even with keep_count=0
        assert os.path.exists(newest)
        assert not os.path.exists(old)

    def test_dry_run_does_not_remove(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        old = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600)
        newer = _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(dry_run=True, keep_count=1))
        stats = trimmer.trim_objdir(objdir, set())

        assert os.path.exists(old)  # not actually removed
        assert os.path.exists(newer)
        assert stats["removed"] == 1  # but reported as would-remove

    def test_skips_lockdirs(self, tmp_path):
        """Lockdirs live next to their .o inside a bucket dir (sidecar
        siblings, not top-level entries). The scanner must descend into
        bucket dirs to find .o files but skip ``.lockdir`` siblings.
        """
        objdir = str(tmp_path / "obj")
        bucket_dir = os.path.join(objdir, "ab")
        os.makedirs(bucket_dir)
        lockdir = os.path.join(bucket_dir, "foo_aabbccddeeff_11223344556677_0011223344556677.o.lockdir")
        os.makedirs(lockdir)

        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_objdir(objdir, set())

        assert os.path.isdir(lockdir)
        assert stats["total_scanned"] == 0

    def test_nonexistent_directory(self, tmp_path):
        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_objdir(str(tmp_path / "nonexistent"), set())
        assert stats["total_scanned"] == 0
        assert stats["removed"] == 0

    def test_scans_inside_bucket_dirs_not_flat_objdir(self, tmp_path):
        """Object files now live one level down in 2-hex bucket dirs
        (``<objdir>/<file_hash[:2]>/<basename>_<...>.o``). The scanner must
        descend into bucket subdirs to find them, and must ignore any stray
        ``.o`` accidentally placed flat at the top level — those would be
        leftovers from a pre-sharding install and (per the rollout doc) are
        the operator's responsibility to wipe before first run.
        """
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        current_hash = "aabbccddeeff"

        # Bucket-resident object: the only one that should be discovered.
        bucket_dir = os.path.join(objdir, current_hash[:2])
        os.makedirs(bucket_dir)
        bucket_obj_name = f"in_bucket_{current_hash}_11223344556677_0011223344556677.o"
        bucket_obj_path = os.path.join(bucket_dir, bucket_obj_name)
        with open(bucket_obj_path, "wb") as f:
            f.write(b"\0" * 256)

        # Stray flat-layout object: pre-sharding leftover. Scanner must NOT
        # see it (so trim's ``current_kept`` count reflects the sharded
        # population only). It also must NOT crash on it.
        stray_flat_name = "stray_111111111111_11223344556677_0011223344556677.o"
        stray_flat_path = os.path.join(objdir, stray_flat_name)
        with open(stray_flat_path, "wb") as f:
            f.write(b"\0" * 256)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, {current_hash})

        # Discriminating assertion: the bucket-resident file's hash IS the
        # current hash, so a bucket-aware scanner reports current_kept=1.
        # A flat scanner would only see the stray (non-current) and report
        # current_kept=0 — so this assertion fails on the pre-sharding code.
        assert stats["current_kept"] == 1, (
            f"the bucket-resident .o has the current hash and must be counted "
            f"as current_kept. Got stats={stats}"
        )
        assert stats["total_scanned"] == 1
        assert os.path.exists(bucket_obj_path), "current bucket-resident object kept"
        assert os.path.exists(stray_flat_path), (
            "scanner must not touch flat-layout files — they are outside its world"
        )

    def test_skips_non_bucket_top_level_entries(self, tmp_path):
        """Slurm ``slurm-ct-*.out`` files and ``TraceStore`` directories live
        flat at ``$objdir/`` (per the proposal carve-outs). The scanner must
        skip anything at the top level whose name is not a 2-hex bucket dir.
        """
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        # Real bucket with a real .o
        os.makedirs(os.path.join(objdir, "aa"))
        with open(os.path.join(objdir, "aa", "x_aabbccddeeff_11223344556677_0011223344556677.o"), "wb") as f:
            f.write(b"\0" * 64)

        # Carve-outs that share the objdir root
        with open(os.path.join(objdir, "slurm-ct-foo-1234.out"), "wb") as f:
            f.write(b"slurm log")
        os.makedirs(os.path.join(objdir, "TraceStore"))
        os.makedirs(os.path.join(objdir, "not-a-hash"))  # 3-char dir, not a bucket
        os.makedirs(os.path.join(objdir, "AA"))  # uppercase, not lowercase hex

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, {"aabbccddeeff"})

        assert stats["total_scanned"] == 1, (
            f"only the real bucket-resident .o should be counted; carve-out files "
            f"and non-bucket dirs must be invisible. Got total_scanned={stats['total_scanned']}"
        )
        # And the carve-outs must remain on disk untouched.
        assert os.path.exists(os.path.join(objdir, "slurm-ct-foo-1234.out"))
        assert os.path.isdir(os.path.join(objdir, "TraceStore"))
        assert os.path.isdir(os.path.join(objdir, "not-a-hash"))
        assert os.path.isdir(os.path.join(objdir, "AA"))

    def test_multiple_basenames_independent(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        # foo: one current, one old
        foo_current = _touch_obj(objdir, "foo", "aabbccddeeff", "11223344556677", "0011223344556677")
        foo_old = _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600)

        # bar: all non-current
        bar_old = _touch_obj(objdir, "bar", "222222222222", "11223344556677", "0011223344556677", age_seconds=7200)
        bar_newer = _touch_obj(objdir, "bar", "333333333333", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, {"aabbccddeeff"})

        assert os.path.exists(foo_current)
        assert not os.path.exists(foo_old)  # removed: foo has current file, so no safety net needed
        assert os.path.exists(bar_newer)  # kept: safety keeps newest per basename
        assert not os.path.exists(bar_old)  # removed: oldest non-current
        assert stats["basenames_found"] == 2

    def test_bytes_freed_tracked(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600, size=4096)
        _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60, size=2048)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["bytes_freed"] == 4096


# ── CacheTrimmer pchdir ──────────────────────────────────────────────


def _make_pchdir_entry(pchdir, command_hash, headers, *, age_seconds=0, size_per_gch=1024):
    """Create a fake PCH command-hash directory with .gch files."""
    d = os.path.join(pchdir, command_hash)
    os.makedirs(d, exist_ok=True)
    for h in headers:
        path = os.path.join(d, h + ".gch")
        with open(path, "wb") as f:
            f.write(b"\0" * size_per_gch)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(d, (mtime, mtime))
    return d


class TestTrimPchdir:
    def test_keeps_newest_per_header(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        old = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600)
        new = _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(old)
        assert os.path.isdir(new)
        assert stats["dirs_removed"] == 1
        assert stats["dirs_kept"] == 1

    def test_removes_oldest_per_header(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        oldest = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=7200)
        middle = _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=3600)
        newest = _make_pchdir_entry(pchdir, "c" * 16, ["stdafx.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=2))
        trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(oldest)
        assert os.path.isdir(middle)
        assert os.path.isdir(newest)

    def test_per_cmd_hash_bucketing_unrelated_basenames_coexist(self, tmp_path):
        """Regression: two unrelated cmd_hash dirs that happen to
        share a header basename (e.g. ``stdafx.h`` from two different
        projects) must NOT evict each other. Each cmd_hash dir is an
        independent cache unit; the keep_count and max_age policies
        treat them globally by mtime, not bucketed by basename."""
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        # Three projects all using stdafx.h, each with its own cmd_hash
        # (different compiler/flags/realpath). Without keep_count limit,
        # all three should coexist.
        a = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600)
        b = _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=1800)
        c = _make_pchdir_entry(pchdir, "c" * 16, ["stdafx.h"], age_seconds=60)

        # keep_count=3 → all kept
        trimmer = CacheTrimmer(_make_args(keep_count=3))
        stats = trimmer.trim_pchdir(pchdir)
        assert os.path.isdir(a)
        assert os.path.isdir(b)
        assert os.path.isdir(c)
        assert stats["dirs_removed"] == 0
        assert stats["dirs_kept"] == 3

    def test_max_age_keeps_recent_dirs_beyond_keep_count(self, tmp_path):
        """max_age extends retention beyond keep_count for dirs younger
        than the cutoff."""
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        old_outside = _make_pchdir_entry(pchdir, "a" * 16, ["x.h"], age_seconds=86400)
        recent1 = _make_pchdir_entry(pchdir, "b" * 16, ["x.h"], age_seconds=3600)
        recent2 = _make_pchdir_entry(pchdir, "c" * 16, ["x.h"], age_seconds=60)

        # keep_count=1 keeps c only; max_age=2h keeps recent1 too
        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=2.0 / 24))
        trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(old_outside)
        assert os.path.isdir(recent1)
        assert os.path.isdir(recent2)

    def test_dry_run_does_not_remove(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        old = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600)
        _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(dry_run=True, keep_count=1))
        stats = trimmer.trim_pchdir(pchdir)

        assert os.path.isdir(old)
        assert stats["dirs_removed"] == 1

    def test_nonexistent_directory(self, tmp_path):
        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_pchdir(str(tmp_path / "nonexistent"))
        assert stats["total_dirs_scanned"] == 0

    def test_skips_non_hash_directories(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        os.makedirs(os.path.join(pchdir, "not-a-hash"))
        os.makedirs(os.path.join(pchdir, "AABBCCDDEE001122"))  # uppercase

        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_pchdir(pchdir)

        assert stats["total_dirs_scanned"] == 0

    def test_bytes_freed_tracked(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600, size_per_gch=4096)
        _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=60, size_per_gch=2048)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_pchdir(pchdir)

        assert stats["bytes_freed"] == 4096


# ── CLI integration ──────────────────────────────────────────────────


class TestMainCLI:
    def test_mutual_exclusion_error(self):
        from compiletools.trim_cache_main import main

        rc = main(["--objdir-only", "--pchdir-only"])
        assert rc == 1

    def test_dry_run_with_nonexistent_dirs(self):
        from compiletools.trim_cache_main import main

        rc = main(["--dry-run", "--objdir=/nonexistent/obj", "--pchdir=/nonexistent/pch"])
        assert rc == 0

    def test_objdir_only_flag(self, tmp_path):
        from compiletools.trim_cache_main import main

        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        rc = main(["--dry-run", "--objdir-only", f"--objdir={objdir}"])
        assert rc == 0

    def test_pchdir_only_flag(self, tmp_path):
        from compiletools.trim_cache_main import main

        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        rc = main(["--dry-run", "--pchdir-only", f"--pchdir={pchdir}"])
        assert rc == 0


# ── _safe_locked_unlink / _safe_locked_rmtree behavior ───────────────


class TestSafeLockedUnlink:
    def test_refuses_when_lock_unavailable(self, tmp_path, monkeypatch):
        """When FileLock raises OSError (filesystem unsupported,
        permissions, etc.), we MUST NOT delete the file unlocked. Caller
        sees False; the file remains on disk for retry."""
        from compiletools import trim_cache

        target = tmp_path / "victim.o"
        target.write_bytes(b"x" * 1024)

        class _RaisingLock:
            def __init__(self, *_args, **_kwargs):
                raise OSError("lock subsystem unavailable")

        monkeypatch.setattr("compiletools.locking.FileLock", _RaisingLock)
        result = trim_cache._safe_locked_unlink(str(target))

        assert result is False, "must refuse to delete when lock unavailable"
        assert target.exists(), "file must NOT be deleted unlocked"


class TestSafeLockedRmtree:
    def test_refuses_when_lock_unavailable(self, tmp_path, monkeypatch):
        """When FileLock raises OSError on a contained file, we
        MUST NOT rmtree unlocked. Caller sees False; dir remains."""
        from compiletools import trim_cache

        d = tmp_path / "cmd_hash_dir"
        d.mkdir()
        gch = d / "stdafx.h.gch"
        gch.write_bytes(b"x" * 1024)

        class _RaisingLock:
            def __init__(self, *_args, **_kwargs):
                raise OSError("lock subsystem unavailable")

        monkeypatch.setattr("compiletools.locking.FileLock", _RaisingLock)
        result = trim_cache._safe_locked_rmtree(str(d))

        assert result is False
        assert d.exists()
        assert gch.exists()

    def test_aborts_on_concurrent_file_creation(self, tmp_path, monkeypatch):
        """If a peer build creates a fresh file in the dir between
        the initial scan and the lock window, we re-scan inside the lock
        and abort the rmtree. The new (unlocked) file would be deleted
        half-written otherwise."""
        from compiletools import trim_cache

        d = tmp_path / "cmd_hash_dir"
        d.mkdir()
        existing = d / "stdafx.h.gch"
        existing.write_bytes(b"x" * 1024)
        new_file_path = d / "newheader.h.gch"

        # Fake lock that simulates the lock-window pause by creating a
        # new file as a side-effect of __enter__ (mimics a peer build
        # racing in between scan and lock acquisition).
        class _RacingLock:
            def __init__(self, target_file, _args):
                self.target_file = target_file

            def __enter__(self):
                # Simulate concurrent peer creating a NEW file in the
                # same dir during our lock window.
                if not new_file_path.exists():
                    new_file_path.write_bytes(b"y" * 512)
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("compiletools.locking.FileLock", _RacingLock)
        result = trim_cache._safe_locked_rmtree(str(d))

        assert result is False, "must refuse to rmtree when new files appeared"
        assert d.exists(), "dir must still exist"
        assert new_file_path.exists(), "the racing peer file must NOT be deleted"


# ── _load_pch_manifest helper ────────────────────────────────────────


class TestLoadPchManifest:
    def test_returns_dict_when_manifest_present(self, tmp_path):
        from compiletools.trim_cache import _load_pch_manifest
        cmd_hash_dir = tmp_path / "abc1234567890123"
        cmd_hash_dir.mkdir()
        (cmd_hash_dir / "manifest.json").write_text(
            '{"header_realpath": "/abs/foo.h", "transitive_hashes": {"/abs/bar.h": "deadbeef"}}'
        )
        manifest = _load_pch_manifest(str(cmd_hash_dir))
        assert manifest["header_realpath"] == "/abs/foo.h"
        assert manifest["transitive_hashes"] == {"/abs/bar.h": "deadbeef"}

    def test_returns_none_when_missing(self, tmp_path):
        from compiletools.trim_cache import _load_pch_manifest
        cmd_hash_dir = tmp_path / "abc1234567890123"
        cmd_hash_dir.mkdir()
        assert _load_pch_manifest(str(cmd_hash_dir)) is None

    def test_returns_none_on_corrupt_json(self, tmp_path):
        from compiletools.trim_cache import _load_pch_manifest
        cmd_hash_dir = tmp_path / "abc1234567890123"
        cmd_hash_dir.mkdir()
        (cmd_hash_dir / "manifest.json").write_text("{not json")
        assert _load_pch_manifest(str(cmd_hash_dir)) is None


# ── Issue #4 placeholder (per-realpath bucketing) ────────────────────


class TestPchPerRealpathBucketing:
    """With sidecar manifests present, keep_count applies per-realpath
    so cross-variant builds of the same header are not mutually evicted
    at the default keep_count=1."""

    @staticmethod
    def _write_manifest(pchdir, cmd_hash, header_realpath, transitive=None, age_seconds=0):
        manifest = {
            "header_realpath": header_realpath,
            "compiler": "g++",
            "compiler_identity": "g++|0|0",
            "transitive_hashes": transitive or {},
        }
        d = os.path.join(pchdir, cmd_hash)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        # Re-stamp mtime AFTER writing manifest, since writing the file
        # bumps the directory's mtime back to "now".
        if age_seconds:
            mtime = time.time() - age_seconds
            os.utime(d, (mtime, mtime))

    def test_distinct_realpaths_get_independent_keep_count(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        # Two different headers, each with two cmd_hash variants.
        a1 = _make_pchdir_entry(pchdir, "a" * 16, ["headerA.h"])
        a2 = _make_pchdir_entry(pchdir, "b" * 16, ["headerA.h"])
        b1 = _make_pchdir_entry(pchdir, "c" * 16, ["headerB.h"])
        b2 = _make_pchdir_entry(pchdir, "d" * 16, ["headerB.h"])
        # Manifests first, then re-stamp mtime so write doesn't reset it.
        self._write_manifest(pchdir, "a" * 16, "/proj/headerA.h", age_seconds=3600)
        self._write_manifest(pchdir, "b" * 16, "/proj/headerA.h", age_seconds=60)
        self._write_manifest(pchdir, "c" * 16, "/proj/headerB.h", age_seconds=3600)
        self._write_manifest(pchdir, "d" * 16, "/proj/headerB.h", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pchdir(pchdir)

        # keep_count=1 PER realpath bucket — newest of each survives.
        assert os.path.isdir(a2) and not os.path.isdir(a1)
        assert os.path.isdir(b2) and not os.path.isdir(b1)

    def test_legacy_entries_without_manifest_use_global_ranking(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        # No manifests written — legacy behavior.
        a = _make_pchdir_entry(pchdir, "1" * 16, ["x.h"], age_seconds=3600)
        b = _make_pchdir_entry(pchdir, "2" * 16, ["x.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pchdir(pchdir)

        # Legacy: global keep_count=1 keeps newest, drops older.
        assert os.path.isdir(b)
        assert not os.path.isdir(a)


class TestPchTransitiveStaleness:
    """When a transitive header recorded in the manifest no longer matches
    the on-disk content, the cmd_hash dir is pre-evicted so the user never
    pays the slow ``cc1`` PCH-stamp rebuild."""

    @staticmethod
    def _git_blob_sha1(content: bytes) -> str:
        """Helper matching global_hash_registry._compute_external_file_hash."""
        import hashlib
        return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()

    def test_stale_transitive_hash_evicts_entry(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        cmd_hash = "a" * 16
        a = _make_pchdir_entry(pchdir, cmd_hash, ["stdafx.h"], age_seconds=60)

        # Real transitive header on disk with current hash X; manifest
        # claims old hash Y — staleness should pre-evict.
        config_h = tmp_path / "config.h"
        config_h.write_text("// new content\n")
        manifest = {
            "header_realpath": "/proj/include/stdafx.h",
            "compiler": "g++",
            "compiler_identity": "g++|0|0",
            "transitive_hashes": {str(config_h): "0" * 40},  # bogus old sha
        }
        with open(os.path.join(pchdir, cmd_hash, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        trimmer = CacheTrimmer(_make_args(keep_count=10))  # would otherwise keep
        trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(a), "stale-transitive cmd_hash should be evicted"

    def test_matching_transitive_hash_keeps_entry(self, tmp_path):
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        cmd_hash = "b" * 16
        a = _make_pchdir_entry(pchdir, cmd_hash, ["stdafx.h"], age_seconds=60)

        config_h = tmp_path / "config.h"
        config_h.write_bytes(b"// stable content\n")
        expected_sha = self._git_blob_sha1(config_h.read_bytes())

        manifest = {
            "header_realpath": "/proj/include/stdafx.h",
            "compiler": "g++",
            "compiler_identity": "g++|0|0",
            "transitive_hashes": {str(config_h): expected_sha},
        }
        with open(os.path.join(pchdir, cmd_hash, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        trimmer = CacheTrimmer(_make_args(keep_count=10))
        trimmer.trim_pchdir(pchdir)

        assert os.path.isdir(a)

    def test_missing_transitive_header_does_not_evict(self, tmp_path):
        """If the transitive header file is gone (deleted, moved), do NOT
        evict — staleness pre-eviction is best-effort."""
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)
        cmd_hash = "c" * 16
        a = _make_pchdir_entry(pchdir, cmd_hash, ["stdafx.h"], age_seconds=60)

        manifest = {
            "header_realpath": "/proj/stdafx.h",
            "compiler": "g++",
            "compiler_identity": "g++|0|0",
            # Path that does not exist on disk.
            "transitive_hashes": {str(tmp_path / "missing.h"): "0" * 40},
        }
        with open(os.path.join(pchdir, cmd_hash, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        trimmer = CacheTrimmer(_make_args(keep_count=10))
        trimmer.trim_pchdir(pchdir)

        assert os.path.isdir(a)


# ── Issue #7: noncurrent_kept accounting ─────────────────────────────


class TestNoncurrentKeptAccounting:
    def test_keep_count_zero_with_safety_floor(self, tmp_path):
        """When keep_count=0 AND no current entry exists, the
        safety pop bumps a candidate up to to_keep BEFORE the
        noncurrent_kept calculation runs. Verify the count stays
        accurate (one survivor reported, one removed)."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=7200)
        _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["noncurrent_kept"] == 1, f"safety should keep exactly 1; got {stats['noncurrent_kept']}"
        assert stats["removed"] == 1
        assert stats["current_kept"] == 0

    def test_keep_count_zero_safety_with_max_age_keeps_recent(self, tmp_path):
        """keep_count=0 + safety + max_age. The safety-popped file
        must be counted in noncurrent_kept. A second file inside max_age
        should also be kept and counted."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        # Three non-current files; only the oldest is beyond max_age=1d
        _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=172800)
        _touch_obj(objdir, "foo", "222222222222", "11223344556677", "0011223344556677", age_seconds=3600)
        _touch_obj(objdir, "foo", "333333333333", "11223344556677", "0011223344556677", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0, max_age=1))
        stats = trimmer.trim_objdir(objdir, set())

        # Newest popped to to_keep by safety (1), middle kept by max_age (1),
        # oldest beyond max_age → removed.
        assert stats["noncurrent_kept"] == 2, f"expected 2 kept (safety + max_age); got {stats['noncurrent_kept']}"
        assert stats["removed"] == 1

    def test_keep_count_zero_single_noncurrent_file(self, tmp_path):
        """Edge: single file, keep_count=0, no current → safety
        keeps the lone file. noncurrent_kept must be 1, removed 0."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        _touch_obj(objdir, "foo", "111111111111", "11223344556677", "0011223344556677", age_seconds=3600)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["noncurrent_kept"] == 1
        assert stats["removed"] == 0
