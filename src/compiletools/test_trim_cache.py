"""Tests for trim_cache module."""

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
        result = parse_object_filename(
            "foo_aabbccddeeff_11223344556677_0011223344556677.o"
        )
        assert result == ("foo", "aabbccddeeff", "11223344556677", "0011223344556677")

    def test_basename_with_underscores(self):
        result = parse_object_filename(
            "my_cool_module_aabbccddeeff_11223344556677_0011223344556677.o"
        )
        assert result is not None
        assert result[0] == "my_cool_module"
        assert result[1] == "aabbccddeeff"

    def test_single_char_basename(self):
        result = parse_object_filename(
            "x_aabbccddeeff_11223344556677_0011223344556677.o"
        )
        assert result is not None
        assert result[0] == "x"

    def test_not_an_object_file(self):
        assert parse_object_filename("random.o") is None
        assert parse_object_filename("foo.txt") is None
        assert parse_object_filename("not_an_object") is None

    def test_wrong_hash_lengths(self):
        # file_hash too short (11 instead of 12)
        assert parse_object_filename(
            "foo_aabbccddee_11223344556677_0011223344556677.o"
        ) is None
        # dep_hash too short (13 instead of 14)
        assert parse_object_filename(
            "foo_aabbccddeeff_1122334455667_0011223344556677.o"
        ) is None
        # macro_hash too short (15 instead of 16)
        assert parse_object_filename(
            "foo_aabbccddeeff_11223344556677_001122334455667.o"
        ) is None

    def test_uppercase_hex_rejected(self):
        assert parse_object_filename(
            "foo_AABBCCDDEEFF_11223344556677_0011223344556677.o"
        ) is None

    def test_non_hex_chars_rejected(self):
        assert parse_object_filename(
            "foo_gghhiijjkkll_11223344556677_0011223344556677.o"
        ) is None


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
    """Create a fake .o file with controlled mtime and size."""
    name = f"{basename}_{file_hash}_{dep_hash}_{macro_hash}.o"
    path = os.path.join(objdir, name)
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

        assert os.path.exists(old)   # not actually removed
        assert os.path.exists(newer)
        assert stats["removed"] == 1  # but reported as would-remove

    def test_skips_lockdirs(self, tmp_path):
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        lockdir = os.path.join(objdir, "foo.o.lockdir")
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
        assert not os.path.exists(foo_old)   # removed: foo has current file, so no safety net needed
        assert os.path.exists(bar_newer)     # kept: safety keeps newest per basename
        assert not os.path.exists(bar_old)   # removed: oldest non-current
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
        """I-B5 regression: two unrelated cmd_hash dirs that happen to
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
