"""Tests for trim_cache module."""

import hashlib
import io
import json
import os
import re
import time
import types

import pytest

from compiletools import trim_cache
from compiletools.trim_cache import (
    CacheTrimmer,
    _load_pch_manifest,
    build_current_hash_set,
    parse_object_filename,
    warn_if_suspicious_cas_dir,
    warn_if_wrong_checkout,
)
from compiletools.trim_cache_main import main


@pytest.fixture
def objdir(tmp_path):
    d = str(tmp_path / "obj")
    os.makedirs(d)
    return d


@pytest.fixture
def pchdir(tmp_path):
    d = str(tmp_path / "pch")
    os.makedirs(d)
    return d


@pytest.fixture
def pcmdir(tmp_path):
    d = str(tmp_path / "pcm")
    os.makedirs(d)
    return d


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
        "json": False,
        "verbose": 0,
        "keep_count": 1,
        "max_age": None,
        "parallel": 1,
        "list_unresolvable": False,
        "variant": "gcc.debug",
        "cas_objdir": None,
        "cas_pchdir": None,
        "cas_pcmdir": None,
        "cas_exedir": None,
        "cas_objdir_only": False,
        "cas_pchdir_only": False,
        "cas_pcmdir_only": False,
        "cas_exedir_only": False,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _touch_obj(
    objdir,
    basename,
    file_hash,
    dep_hash="11223344556677",
    macro_hash="0011223344556677",
    *,
    age_seconds=0,
    size=1024,
):
    """Create a fake .o file in its sharded bucket dir, with controlled mtime/size.

    Mirrors production layout: ``<objdir>/<file_hash[:2]>/<basename>_<...>.o``.
    ``dep_hash`` and ``macro_hash`` default to the canonical "11223344556677"
    / "0011223344556677" pair used by every cache-trim test (only the
    `file_hash` axis actually varies across cases). Sidecar lockdirs are
    placed explicitly by tests that exercise lockdir behaviour.
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
    def test_keeps_current_files(self, objdir):
        current_hash = "aabbccddeeff"
        p = _touch_obj(objdir, "foo", current_hash)

        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_objdir(objdir, {current_hash})

        assert os.path.exists(p)
        assert stats["current_kept"] == 1
        assert stats["removed"] == 0

    def test_removes_oldest_noncurrent(self, objdir):
        current_hash = "aabbccddeeff"

        old = _touch_obj(objdir, "foo", "111111111111", age_seconds=3600)
        newer = _touch_obj(objdir, "foo", "222222222222", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, {current_hash})

        assert not os.path.exists(old)
        assert os.path.exists(newer)
        assert stats["removed"] == 1
        assert stats["noncurrent_kept"] == 1

    def test_keeps_newest_noncurrent_per_basename(self, objdir):
        _touch_obj(objdir, "foo", "111111111111", age_seconds=3600)
        newest = _touch_obj(objdir, "foo", "222222222222", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, set())

        assert os.path.exists(newest)
        assert stats["noncurrent_kept"] == 1

    def test_keep_count_2(self, objdir):
        oldest = _touch_obj(objdir, "foo", "111111111111", age_seconds=7200)
        middle = _touch_obj(objdir, "foo", "222222222222", age_seconds=3600)
        newest = _touch_obj(objdir, "foo", "333333333333", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=2))
        stats = trimmer.trim_objdir(objdir, set())

        assert not os.path.exists(oldest)
        assert os.path.exists(middle)
        assert os.path.exists(newest)
        assert stats["removed"] == 1
        assert stats["noncurrent_kept"] == 2

    def test_max_age_interaction(self, objdir):
        # 2 days old -- beyond max_age of 1 day
        old = _touch_obj(objdir, "foo", "111111111111", age_seconds=172800)
        # 1 hour old -- within max_age of 1 day
        recent = _touch_obj(objdir, "foo", "222222222222", age_seconds=3600)
        # newest -- kept by keep_count=1
        newest = _touch_obj(objdir, "foo", "333333333333", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=1))
        trimmer.trim_objdir(objdir, set())

        assert not os.path.exists(old)
        assert os.path.exists(recent)  # within max_age, not removed
        assert os.path.exists(newest)  # kept by keep_count

    def test_safety_keeps_one_when_all_noncurrent(self, objdir):
        old = _touch_obj(objdir, "foo", "111111111111", age_seconds=7200)
        newest = _touch_obj(objdir, "foo", "222222222222", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        trimmer.trim_objdir(objdir, set())

        # Safety: at least 1 kept per basename even with keep_count=0
        assert os.path.exists(newest)
        assert not os.path.exists(old)

    def test_dry_run_does_not_remove(self, objdir):
        old = _touch_obj(objdir, "foo", "111111111111", age_seconds=3600)
        newer = _touch_obj(objdir, "foo", "222222222222", age_seconds=60)

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

    def test_scans_inside_bucket_dirs_not_flat_objdir(self, objdir):
        """Object files now live one level down in 2-hex bucket dirs
        (``<objdir>/<file_hash[:2]>/<basename>_<...>.o``). The scanner must
        descend into bucket subdirs to find them, and must ignore any stray
        ``.o`` accidentally placed flat at the top level — those would be
        leftovers from a pre-sharding install and (per the rollout doc) are
        the operator's responsibility to wipe before first run.
        """
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
            f"the bucket-resident .o has the current hash and must be counted as current_kept. Got stats={stats}"
        )
        assert stats["total_scanned"] == 1
        assert os.path.exists(bucket_obj_path), "current bucket-resident object kept"
        assert os.path.exists(stray_flat_path), "scanner must not touch flat-layout files — they are outside its world"

    def test_skips_non_bucket_top_level_entries(self, objdir):
        """Anything at the top level of ``$objdir/`` whose name is not a
        2-hex bucket directory must be invisible to the scanner.
        ``TraceStore/`` lives there by design; ``slurm-ct-*.out`` files only
        appear here if the user has overridden ``--diagnostics-dir`` to point
        back into ``--cas-objdir`` (the default is
        ``<bindir>/diagnostics/<invocation>/``).  Either way, the scanner
        ignores them.
        """

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

    def test_multiple_basenames_independent(self, objdir):
        # foo: one current, one old
        foo_current = _touch_obj(objdir, "foo", "aabbccddeeff")
        foo_old = _touch_obj(objdir, "foo", "111111111111", age_seconds=3600)

        # bar: all non-current
        bar_old = _touch_obj(objdir, "bar", "222222222222", age_seconds=7200)
        bar_newer = _touch_obj(objdir, "bar", "333333333333", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, {"aabbccddeeff"})

        assert os.path.exists(foo_current)
        assert not os.path.exists(foo_old)  # removed: foo has current file, so no safety net needed
        assert os.path.exists(bar_newer)  # kept: safety keeps newest per basename
        assert not os.path.exists(bar_old)  # removed: oldest non-current
        assert stats["basenames_found"] == 2

    def test_bytes_freed_tracked(self, objdir):
        _touch_obj(objdir, "foo", "111111111111", age_seconds=3600, size=4096)
        _touch_obj(objdir, "foo", "222222222222", age_seconds=60, size=2048)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["bytes_freed"] == 4096


# ── GPFS scan acceleration: worker sourcing, stat-elision, parallelism ─


class TestWorkerSourcing:
    """The scan worker count is sourced from ``--parallel`` (``-j``) and
    gated by the filesystem: parallel only on high-latency cluster/network
    filesystems, serial on local disk and unknown filesystems."""

    def test_parallel_filesystem_uses_parallel_arg(self, objdir, monkeypatch):
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        trimmer = CacheTrimmer(_make_args(parallel=8))
        assert trimmer._workers_for(objdir) == 8

    def test_local_filesystem_stays_serial_regardless_of_parallel(self, objdir, monkeypatch):
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "ext4")
        trimmer = CacheTrimmer(_make_args(parallel=8))
        assert trimmer._workers_for(objdir) == 1

    def test_unknown_filesystem_stays_serial(self, objdir, monkeypatch):
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "unknown")
        trimmer = CacheTrimmer(_make_args(parallel=8))
        assert trimmer._workers_for(objdir) == 1

    def test_missing_parallel_arg_defaults_to_serial(self, objdir, monkeypatch):
        # A caller that never plumbed --parallel must not crash and must stay serial.
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        args = _make_args()
        del args.parallel
        trimmer = CacheTrimmer(args)
        assert trimmer._workers_for(objdir) == 1


class TestObjdirStatElision:
    """On GPFS every ``stat()`` is a metadata round-trip. Current-hash
    objects are kept regardless of mtime/size, so they must never be
    statted; only non-current objects (ranked by mtime) are."""

    def test_current_entries_are_not_statted(self, objdir, monkeypatch):
        current_hash = "aabbccddeeff"
        _touch_obj(objdir, "foo", current_hash)  # current  -> must NOT be statted
        _touch_obj(objdir, "bar", "111111111111")  # noncurrent -> must be statted

        statted = []
        real = trim_cache._entry_mtime_size

        def spy(entry):
            statted.append(entry.name)
            return real(entry)

        monkeypatch.setattr(trim_cache, "_entry_mtime_size", spy)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_objdir(objdir, {current_hash})

        assert all(not name.startswith("foo_") for name in statted), (
            f"current-hash object must not be statted; statted={statted}"
        )
        assert any(name.startswith("bar_") for name in statted), "non-current object must be statted for mtime ranking"
        # Behavior unchanged: current kept, both scanned.
        assert stats["current_kept"] == 1
        assert stats["total_scanned"] == 2


class TestParallelScanCorrectness:
    """Forcing the parallel code path (workers > 1) on a local tmpdir must
    produce byte-for-byte the same keep/remove decisions as serial — the
    per-bucket fan-out only changes *how* entries are discovered, never the
    policy applied to them."""

    @pytest.fixture
    def _force_parallel(self, monkeypatch):
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

    def test_objdir_parallel_merge_matches_policy(self, objdir, _force_parallel):
        # Spread objects across several buckets (distinct file_hash prefixes)
        # so the fan-out has multiple units to merge.
        keep = []
        drop = []
        for i in range(6):
            fh_new = f"{i:02d}cccccccccc"
            fh_old = f"{i:02d}dddddddddd"
            keep.append(_touch_obj(objdir, f"src{i}", fh_new, age_seconds=60))
            drop.append(_touch_obj(objdir, f"src{i}", fh_old, age_seconds=3600))

        trimmer = CacheTrimmer(_make_args(keep_count=1, parallel=8))
        stats = trimmer.trim_objdir(objdir, set())

        assert all(os.path.exists(p) for p in keep), "newest per basename must survive"
        assert not any(os.path.exists(p) for p in drop), "older per basename must be removed"
        assert stats["removed"] == 6
        assert stats["basenames_found"] == 6

    def test_pchdir_parallel_merge_matches_policy(self, pchdir, _force_parallel):
        # Three cmd_hash dirs (>1 fan-out unit) for the same header in the
        # legacy (manifest-less) global bucket; keep_count=1 keeps newest.
        oldest = _make_pchdir_entry(pchdir, "a" * 16, ["h.h"], age_seconds=7200)
        middle = _make_pchdir_entry(pchdir, "b" * 16, ["h.h"], age_seconds=3600)
        newest = _make_pchdir_entry(pchdir, "c" * 16, ["h.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1, parallel=8))
        stats = trimmer.trim_pchdir(pchdir)

        assert os.path.isdir(newest)
        assert not os.path.isdir(oldest) and not os.path.isdir(middle)
        assert stats["dirs_removed"] == 2

    def test_pcmdir_parallel_merge_matches_policy(self, pcmdir, _force_parallel):
        old = _make_pcmdir_entry(pcmdir, "a" * 16, ["m.pcm"], age_seconds=3600)
        new = _make_pcmdir_entry(pcmdir, "b" * 16, ["m.pcm"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1, parallel=8))
        trimmer.trim_pcmdir(pcmdir)

        assert os.path.isdir(new) and not os.path.isdir(old)

    def test_exedir_parallel_merge_matches_policy(self, tmp_path, _force_parallel):
        exedir = str(tmp_path / "cas-exe")
        # Several buckets (distinct link-key prefixes) of the same basename.
        new = _touch_exe(exedir, "main", "aa11" * 16, age_seconds=0)
        old1 = _touch_exe(exedir, "main", "bb22" * 16, age_seconds=86400)
        old2 = _touch_exe(exedir, "main", "cc33" * 16, age_seconds=2 * 86400)

        trimmer = CacheTrimmer(_make_args(keep_count=1, parallel=8))
        stats = trimmer.trim_exedir(exedir)

        assert os.path.exists(new)
        assert not os.path.exists(old1) and not os.path.exists(old2)
        assert stats["removed"] == 2


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
    def test_keeps_newest_per_header(self, pchdir):
        old = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600)
        new = _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(old)
        assert os.path.isdir(new)
        assert stats["dirs_removed"] == 1
        assert stats["dirs_kept"] == 1

    def test_removes_oldest_per_header(self, pchdir):
        oldest = _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=7200)
        middle = _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=3600)
        newest = _make_pchdir_entry(pchdir, "c" * 16, ["stdafx.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=2))
        trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(oldest)
        assert os.path.isdir(middle)
        assert os.path.isdir(newest)

    def test_per_cmd_hash_bucketing_unrelated_basenames_coexist(self, pchdir):
        """Regression: two unrelated cmd_hash dirs that happen to
        share a header basename (e.g. ``stdafx.h`` from two different
        projects) must NOT evict each other. Each cmd_hash dir is an
        independent cache unit; the keep_count and max_age policies
        treat them globally by mtime, not bucketed by basename."""

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

    def test_max_age_keeps_recent_dirs_beyond_keep_count(self, pchdir):
        """max_age extends retention beyond keep_count for dirs younger
        than the cutoff."""

        old_outside = _make_pchdir_entry(pchdir, "a" * 16, ["x.h"], age_seconds=86400)
        recent1 = _make_pchdir_entry(pchdir, "b" * 16, ["x.h"], age_seconds=3600)
        recent2 = _make_pchdir_entry(pchdir, "c" * 16, ["x.h"], age_seconds=60)

        # keep_count=1 keeps c only; max_age=2h keeps recent1 too
        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=2.0 / 24))
        trimmer.trim_pchdir(pchdir)

        assert not os.path.isdir(old_outside)
        assert os.path.isdir(recent1)
        assert os.path.isdir(recent2)

    def test_dry_run_does_not_remove(self, pchdir):
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

    def test_skips_non_hash_directories(self, pchdir):
        os.makedirs(os.path.join(pchdir, "not-a-hash"))
        os.makedirs(os.path.join(pchdir, "AABBCCDDEE001122"))  # uppercase

        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_pchdir(pchdir)

        assert stats["total_dirs_scanned"] == 0

    def test_bytes_freed_tracked(self, pchdir):
        _make_pchdir_entry(pchdir, "a" * 16, ["stdafx.h"], age_seconds=3600, size_per_gch=4096)
        _make_pchdir_entry(pchdir, "b" * 16, ["stdafx.h"], age_seconds=60, size_per_gch=2048)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_pchdir(pchdir)

        assert stats["bytes_freed"] == 4096


# ── CLI integration ──────────────────────────────────────────────────


class TestMainCLI:
    def test_mutual_exclusion_error(self):
        rc = main(["--cas-objdir-only", "--cas-pchdir-only"])
        assert rc == 1

    def test_dry_run_with_nonexistent_dirs(self):
        rc = main(["--dry-run", "--cas-objdir=/nonexistent/obj", "--cas-pchdir=/nonexistent/pch"])
        assert rc == 0

    def test_cas_objdir_only_flag(self, objdir):
        rc = main(["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"])
        assert rc == 0

    def test_cas_pchdir_only_flag(self, pchdir):
        rc = main(["--dry-run", "--cas-pchdir-only", f"--cas-pchdir={pchdir}"])
        assert rc == 0


# ── _safe_locked_unlink / _safe_locked_rmtree behavior ───────────────


class TestSafeLockedUnlink:
    def test_refuses_when_lock_unavailable(self, tmp_path, monkeypatch):
        """When FileLock raises OSError (filesystem unsupported,
        permissions, etc.), we MUST NOT delete the file unlocked. Caller
        sees False; the file remains on disk for retry."""

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
        cmd_hash_dir = tmp_path / "abc1234567890123"
        cmd_hash_dir.mkdir()
        (cmd_hash_dir / "manifest.json").write_text(
            '{"header_realpath": "/abs/foo.h", "transitive_hashes": {"/abs/bar.h": "deadbeef"}}'
        )
        manifest = _load_pch_manifest(str(cmd_hash_dir))
        assert manifest is not None
        assert manifest["header_realpath"] == "/abs/foo.h"
        assert manifest["transitive_hashes"] == {"/abs/bar.h": "deadbeef"}

    def test_returns_none_when_missing(self, tmp_path):
        cmd_hash_dir = tmp_path / "abc1234567890123"
        cmd_hash_dir.mkdir()
        assert _load_pch_manifest(str(cmd_hash_dir)) is None

    def test_returns_none_on_corrupt_json(self, tmp_path):
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

    def test_distinct_realpaths_get_independent_keep_count(self, pchdir):
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

    def test_legacy_entries_without_manifest_use_global_ranking(self, pchdir):
        # No manifests written — legacy behavior.
        a = _make_pchdir_entry(pchdir, "1" * 16, ["x.h"], age_seconds=3600)
        b = _make_pchdir_entry(pchdir, "2" * 16, ["x.h"], age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pchdir(pchdir)

        # Legacy: global keep_count=1 keeps newest, drops older.
        assert os.path.isdir(b)
        assert not os.path.isdir(a)


def _make_pcmdir_entry(pcmdir, command_hash, leaves, *, age_seconds=0, size_per_leaf=1024):
    """Create a fake PCM cache entry: ``<pcmdir>/<command_hash>/<leaves>``.

    Mirrors ``_make_pchdir_entry``. PCM uses a single-command_hash layout
    (vs the object cache's 3-axis filename) because the compiler verifies
    BMIs at consume time -- a hash collision causes a slow re-precompile,
    not a miscompile, so the lower-entropy single key is safe.
    """
    d = os.path.join(pcmdir, command_hash)
    os.makedirs(d, exist_ok=True)
    for leaf in leaves:
        path = os.path.join(d, leaf)
        with open(path, "wb") as f:
            f.write(b"\0" * size_per_leaf)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(d, (mtime, mtime))
    return d


class TestTrimPcmdir:
    """Basic ``trim_pcmdir`` policy: keep_count, max_age, missing dir."""

    def test_missing_pcmdir_is_a_no_op(self, tmp_path):
        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_pcmdir(str(tmp_path / "nope"))
        assert stats["total_dirs_scanned"] == 0
        assert stats["dirs_removed"] == 0

    def test_keep_count_drops_oldest_in_a_legacy_bucket(self, pcmdir):
        a = _make_pcmdir_entry(pcmdir, "a" * 16, ["math.pcm"], age_seconds=3600)
        b = _make_pcmdir_entry(pcmdir, "b" * 16, ["math.pcm"], age_seconds=60)
        # No manifests -> __legacy__ bucket -> global keep_count=1.
        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pcmdir(pcmdir)
        assert os.path.isdir(b) and not os.path.isdir(a)

    def test_max_age_keeps_recent_even_beyond_keep_count(self, pcmdir):
        # Three entries: keep_count=1 would normally drop two; max_age
        # rescues the recent ones.
        old = _make_pcmdir_entry(pcmdir, "a" * 16, ["math.pcm"], age_seconds=86400 * 30)
        mid = _make_pcmdir_entry(pcmdir, "b" * 16, ["math.pcm"], age_seconds=60)
        new = _make_pcmdir_entry(pcmdir, "c" * 16, ["math.pcm"], age_seconds=10)
        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=1))
        trimmer.trim_pcmdir(pcmdir)
        assert os.path.isdir(new) and os.path.isdir(mid) and not os.path.isdir(old)

    def test_dry_run_removes_nothing(self, pcmdir):
        a = _make_pcmdir_entry(pcmdir, "a" * 16, ["math.pcm"], age_seconds=3600)
        b = _make_pcmdir_entry(pcmdir, "b" * 16, ["math.pcm"], age_seconds=60)
        trimmer = CacheTrimmer(_make_args(dry_run=True, keep_count=1))
        stats = trimmer.trim_pcmdir(pcmdir)
        assert os.path.isdir(a) and os.path.isdir(b)
        assert stats["dirs_removed"] == 1  # would-be remove count


class TestPcmPerBucketKeyBucketing:
    """``bucket_key`` from the manifest gives independent keep_count buckets."""

    @staticmethod
    def _write_manifest(
        pcmdir, cmd_hash, bucket_key, *, stage="clang_module_interface", transitive=None, age_seconds=0
    ):
        manifest = {
            "bucket_key": bucket_key,
            "stage": stage,
            "compiler": "clang++",
            "compiler_identity": "clang++|0|0",
            "transitive_hashes": transitive or {},
        }
        d = os.path.join(pcmdir, cmd_hash)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        if age_seconds:
            mtime = time.time() - age_seconds
            os.utime(d, (mtime, mtime))

    def test_distinct_buckets_get_independent_keep_count(self, pcmdir):
        # Two named modules, each with two cmd_hash variants.
        a1 = _make_pcmdir_entry(pcmdir, "a" * 16, ["math.pcm"])
        a2 = _make_pcmdir_entry(pcmdir, "b" * 16, ["math.pcm"])
        b1 = _make_pcmdir_entry(pcmdir, "c" * 16, ["util.pcm"])
        b2 = _make_pcmdir_entry(pcmdir, "d" * 16, ["util.pcm"])
        self._write_manifest(pcmdir, "a" * 16, "/proj/math.cppm", age_seconds=3600)
        self._write_manifest(pcmdir, "b" * 16, "/proj/math.cppm", age_seconds=60)
        self._write_manifest(pcmdir, "c" * 16, "/proj/util.cppm", age_seconds=3600)
        self._write_manifest(pcmdir, "d" * 16, "/proj/util.cppm", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pcmdir(pcmdir)

        # keep_count=1 PER bucket -- each newest survives.
        assert os.path.isdir(a2) and not os.path.isdir(a1)
        assert os.path.isdir(b2) and not os.path.isdir(b1)

    def test_header_unit_token_is_a_valid_bucket_key(self, pcmdir):
        """Header units bucket by token (`<vector>`, `"foo.h"`) so the
        same header in different variants/projects shares a bucket."""
        a1 = _make_pcmdir_entry(pcmdir, "1" * 16, ["vector.pcm"])
        a2 = _make_pcmdir_entry(pcmdir, "2" * 16, ["vector.pcm"])
        b1 = _make_pcmdir_entry(pcmdir, "3" * 16, ["cstdio.pcm"])
        self._write_manifest(pcmdir, "1" * 16, "<vector>", stage="clang_header_unit", age_seconds=3600)
        self._write_manifest(pcmdir, "2" * 16, "<vector>", stage="clang_header_unit", age_seconds=60)
        self._write_manifest(pcmdir, "3" * 16, "<cstdio>", stage="clang_header_unit", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_pcmdir(pcmdir)

        # Newest <vector> survives, older one is dropped; <cstdio>'s only
        # entry is kept.
        assert os.path.isdir(a2) and not os.path.isdir(a1)
        assert os.path.isdir(b1)


class TestPcmTransitiveStaleness:
    """When a transitive header changes, the cached cmd_hash dir is
    pre-evicted so the user doesn't pay the slow re-precompile after a
    silent BMI mismatch."""

    @staticmethod
    def _git_blob_sha1(content: bytes) -> str:
        return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()

    def test_stale_transitive_hash_evicts_entry(self, tmp_path):
        pcmdir = str(tmp_path / "pcm")
        os.makedirs(pcmdir)
        cmd_hash = "a" * 16
        a = _make_pcmdir_entry(pcmdir, cmd_hash, ["math.pcm"], age_seconds=60)

        config_h = tmp_path / "config.h"
        config_h.write_text("// new content\n")
        manifest = {
            "bucket_key": "/proj/math.cppm",
            "stage": "clang_module_interface",
            "compiler": "clang++",
            "compiler_identity": "clang++|0|0",
            # Bogus old hash -> mismatch with current content -> evict.
            "transitive_hashes": {str(config_h): "0" * 40},
        }
        with open(os.path.join(pcmdir, cmd_hash, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        trimmer = CacheTrimmer(_make_args(keep_count=10))  # would otherwise keep
        trimmer.trim_pcmdir(pcmdir)

        assert not os.path.isdir(a), "stale-transitive cmd_hash should be evicted"

    def test_matching_transitive_hash_keeps_entry(self, tmp_path):
        pcmdir = str(tmp_path / "pcm")
        os.makedirs(pcmdir)
        cmd_hash = "b" * 16
        a = _make_pcmdir_entry(pcmdir, cmd_hash, ["math.pcm"], age_seconds=60)

        config_h = tmp_path / "config.h"
        config_h.write_bytes(b"// stable content\n")
        expected_sha = self._git_blob_sha1(config_h.read_bytes())

        manifest = {
            "bucket_key": "/proj/math.cppm",
            "stage": "clang_module_interface",
            "compiler": "clang++",
            "compiler_identity": "clang++|0|0",
            "transitive_hashes": {str(config_h): expected_sha},
        }
        with open(os.path.join(pcmdir, cmd_hash, "manifest.json"), "w") as f:
            json.dump(manifest, f)

        trimmer = CacheTrimmer(_make_args(keep_count=10))
        trimmer.trim_pcmdir(pcmdir)

        assert os.path.isdir(a)


class TestPchTransitiveStaleness:
    """When a transitive header recorded in the manifest no longer matches
    the on-disk content, the cmd_hash dir is pre-evicted so the user never
    pays the slow ``cc1`` PCH-stamp rebuild."""

    @staticmethod
    def _git_blob_sha1(content: bytes) -> str:
        """Helper matching global_hash_registry._compute_external_file_hash."""

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
    def test_keep_count_zero_with_safety_floor(self, objdir):
        """When keep_count=0 AND no current entry exists, the
        safety pop bumps a candidate up to to_keep BEFORE the
        noncurrent_kept calculation runs. Verify the count stays
        accurate (one survivor reported, one removed)."""

        _touch_obj(objdir, "foo", "111111111111", age_seconds=7200)
        _touch_obj(objdir, "foo", "222222222222", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["noncurrent_kept"] == 1, f"safety should keep exactly 1; got {stats['noncurrent_kept']}"
        assert stats["removed"] == 1
        assert stats["current_kept"] == 0

    def test_keep_count_zero_safety_with_max_age_keeps_recent(self, objdir):
        """keep_count=0 + safety + max_age. The safety-popped file
        must be counted in noncurrent_kept. A second file inside max_age
        should also be kept and counted."""

        # Three non-current files; only the oldest is beyond max_age=1d
        _touch_obj(objdir, "foo", "111111111111", age_seconds=172800)
        _touch_obj(objdir, "foo", "222222222222", age_seconds=3600)
        _touch_obj(objdir, "foo", "333333333333", age_seconds=60)

        trimmer = CacheTrimmer(_make_args(keep_count=0, max_age=1))
        stats = trimmer.trim_objdir(objdir, set())

        # Newest popped to to_keep by safety (1), middle kept by max_age (1),
        # oldest beyond max_age → removed.
        assert stats["noncurrent_kept"] == 2, f"expected 2 kept (safety + max_age); got {stats['noncurrent_kept']}"
        assert stats["removed"] == 1

    def test_keep_count_zero_single_noncurrent_file(self, objdir):
        """Edge: single file, keep_count=0, no current → safety
        keeps the lone file. noncurrent_kept must be 1, removed 0."""

        _touch_obj(objdir, "foo", "111111111111", age_seconds=3600)

        trimmer = CacheTrimmer(_make_args(keep_count=0))
        stats = trimmer.trim_objdir(objdir, set())

        assert stats["noncurrent_kept"] == 1
        assert stats["removed"] == 0


def _touch_exe(exedir, basename, link_key, *, age_seconds=0, size=1024):
    """Create a fake cas-exe file at ``<exedir>/<linkkey[:2]>/<basename>_<linkkey>.exe``."""
    name = f"{basename}_{link_key}.exe"
    bucket_dir = os.path.join(exedir, link_key[:2])
    os.makedirs(bucket_dir, exist_ok=True)
    path = os.path.join(bucket_dir, name)
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


class TestTrimExedir:
    """``CacheTrimmer.trim_exedir`` deletes stale ``.exe`` files from the
    content-addressable executable cache while honouring keep_count,
    max_age, and hard-link refcount safety."""

    def test_returns_zero_stats_when_dir_missing(self, tmp_path):
        trimmer = CacheTrimmer(_make_args())
        stats = trimmer.trim_exedir(str(tmp_path / "does-not-exist"))
        assert stats["total_scanned"] == 0
        assert stats["removed"] == 0

    def test_keeps_newest_per_basename_evicts_rest(self, tmp_path):
        exedir = str(tmp_path / "cas-exe")
        # Same basename "main", three different link keys, three ages.
        new = _touch_exe(exedir, "main", "aa11" * 16, age_seconds=0)
        mid = _touch_exe(exedir, "main", "bb22" * 16, age_seconds=86400)
        old = _touch_exe(exedir, "main", "cc33" * 16, age_seconds=30 * 86400)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_exedir(exedir)

        assert stats["total_scanned"] == 3
        assert stats["basenames_found"] == 1
        assert stats["kept"] == 1
        assert stats["removed"] == 2
        assert os.path.exists(new), "newest entry must survive keep_count=1"
        assert not os.path.exists(mid)
        assert not os.path.exists(old)

    def test_max_age_keeps_recent_regardless_of_rank(self, tmp_path):
        exedir = str(tmp_path / "cas-exe")
        # Three entries spanning days; max_age=2 days keeps anything <2d.
        a = _touch_exe(exedir, "main", "aa11" * 16, age_seconds=0)
        b = _touch_exe(exedir, "main", "bb22" * 16, age_seconds=86400)  # 1 day
        c = _touch_exe(exedir, "main", "cc33" * 16, age_seconds=10 * 86400)  # 10 days

        trimmer = CacheTrimmer(_make_args(keep_count=1, max_age=2))
        stats = trimmer.trim_exedir(exedir)

        # keep_count=1 picks `a` (newest); max_age=2d additionally protects `b`.
        assert os.path.exists(a)
        assert os.path.exists(b)
        assert not os.path.exists(c)
        assert stats["removed"] == 1

    @pytest.mark.skipif(
        not hasattr(os, "link"),
        reason="platform lacks os.link (e.g. Termux/Android); hard-link protection inapplicable",
    )
    def test_hard_link_protects_entry_from_eviction(self, tmp_path):
        """``bin/<name>`` is published as a hard link to the cas-exe.
        While that hard link exists (st_nlink > 1), trim must skip the
        cas-exe entry — otherwise the next build's existence-only
        short-circuit would still see ``bin/<name>`` (the hard-linked
        twin) but with no cas-exe target to confirm against, breaking
        the user-visible build artefact's content guarantee."""
        exedir = str(tmp_path / "cas-exe")
        bindir = tmp_path / "bin"
        bindir.mkdir()

        # Old entry that would normally be reaped under keep_count=1 + a
        # newer rival, but is hard-linked from bindir.
        live = _touch_exe(exedir, "main", "aa11" * 16, age_seconds=30 * 86400)
        rival = _touch_exe(exedir, "main", "bb22" * 16, age_seconds=0)
        os.link(live, str(bindir / "main"))

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_exedir(exedir)

        assert os.path.exists(live), "hard-linked cas-exe must survive trim"
        assert os.path.exists(rival), "newest rival must also survive (keep_count=1)"
        assert stats["removed"] == 0

    def test_dry_run_does_not_unlink(self, tmp_path):
        exedir = str(tmp_path / "cas-exe")
        old = _touch_exe(exedir, "main", "aa11" * 16, age_seconds=30 * 86400)
        _touch_exe(exedir, "main", "bb22" * 16, age_seconds=0)

        trimmer = CacheTrimmer(_make_args(keep_count=1, dry_run=True))
        stats = trimmer.trim_exedir(exedir)

        assert os.path.exists(old), "dry-run must not unlink"
        assert stats["removed"] == 1, "stats should still reflect what would be removed"

    def test_basename_with_underscores_split_correctly(self, tmp_path):
        """The link-key separator is the LAST underscore. A basename
        like ``my_app`` must remain its own bucket and not collapse
        with ``my`` (whose basename happens to be a substring)."""
        exedir = str(tmp_path / "cas-exe")
        _touch_exe(exedir, "my_app", "aa11" * 16, age_seconds=0)
        _touch_exe(exedir, "my", "bb22" * 16, age_seconds=0)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_exedir(exedir)

        # Two separate basenames, each with a single survivor.
        assert stats["basenames_found"] == 2
        assert stats["removed"] == 0

    def test_distinct_sources_with_same_basename_do_not_co_evict(self, tmp_path):
        """C4: two distinct executables both named ``main`` (e.g.
        ``tests/main.cpp`` and ``tools/main.cpp``) with several cached
        link variants each must NOT bucket together via shared
        basename — they would prematurely evict each other.

        With keep_count=1 and 2 variants from each source, the trim
        must keep at least one variant FROM EACH SOURCE (2 total),
        not 1 from the bucket-of-merged-sources.
        """
        exedir = str(tmp_path / "cas-exe")

        # Source A: tests/main.cpp → 2 variants, both relatively new.
        a1 = _touch_exe(exedir, "main", "aaaa" * 16, age_seconds=0)
        a2 = _touch_exe(exedir, "main", "aabb" * 16, age_seconds=86400)
        # Source B: tools/main.cpp → 2 variants, both older than A.
        b1 = _touch_exe(exedir, "main", "bbaa" * 16, age_seconds=2 * 86400)
        b2 = _touch_exe(exedir, "main", "bbbb" * 16, age_seconds=3 * 86400)

        # Sidecar manifest pins source_realpath for the new bucketing
        # contract. trim_exedir reads these and buckets by
        # (source_realpath, suffix) instead of (basename, suffix).
        for path, src in (
            (a1, "/repo/tests/main.cpp"),
            (a2, "/repo/tests/main.cpp"),
            (b1, "/repo/tools/main.cpp"),
            (b2, "/repo/tools/main.cpp"),
        ):
            with open(path + ".manifest", "w") as f:
                f.write('{"source_realpath": "' + src + '"}\n')

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_exedir(exedir)

        assert stats["basenames_found"] == 2, "C4: distinct sources with same basename must form 2 buckets via sidecar"
        # keep_count=1 → newest A (a1) and newest B (b1) survive.
        assert os.path.exists(a1), "newest variant of source A must survive"
        assert os.path.exists(b1), "newest variant of source B must survive"
        assert not os.path.exists(a2), "older variant of source A must be evicted"
        assert not os.path.exists(b2), "older variant of source B must be evicted"

    def test_legacy_entries_without_sidecar_use_basename_bucketing(self, tmp_path):
        """C4 backwards-compat: entries that pre-date the sidecar contract
        (no .manifest sidecar on disk) continue to be bucketed by
        ``(basename, suffix)`` so existing caches don't suddenly behave
        differently after upgrading.
        """
        exedir = str(tmp_path / "cas-exe")
        # Two same-basename entries with NO sidecar — legacy.
        _touch_exe(exedir, "main", "aaaa" * 16, age_seconds=0)
        old = _touch_exe(exedir, "main", "bbbb" * 16, age_seconds=86400)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        stats = trimmer.trim_exedir(exedir)

        # Single legacy bucket → keep_count=1 → only newest survives.
        assert stats["basenames_found"] == 1
        assert not os.path.exists(old)

    @pytest.mark.skipif(
        not hasattr(os, "link"),
        reason="platform lacks os.link (e.g. Termux/Android); hard-link race inapplicable",
    )
    def test_publish_between_scan_and_unlink_protects_entry(self, tmp_path, monkeypatch):
        """I4 TOCTOU: scan sees nlink=1; before the per-entry unlink
        runs under the lock, a peer publish creates a hard-linked
        bin/<name>, elevating nlink to 2. The unlink must re-stat
        under the lock and bail — otherwise we'd delete an entry that
        just gained a published reference and force a relink on the
        next build.
        """

        exedir = str(tmp_path / "cas-exe")
        bindir = tmp_path / "bin"
        bindir.mkdir()

        # Two old entries that would normally be reaped under keep_count=1
        # plus a newer rival. Pre-trim nlink=1 on both old entries.
        old_a = _touch_exe(exedir, "main", "aaaa" * 16, age_seconds=10 * 86400)
        old_b = _touch_exe(exedir, "main", "bbbb" * 16, age_seconds=10 * 86400)
        _touch_exe(exedir, "main", "cccc" * 16, age_seconds=0)

        # Inject a publish race INSIDE _safe_locked_unlink: when called
        # for old_a, hardlink it into bindir before the (post-lock)
        # nlink re-check. Without the I4 fix, old_a is unlinked and the
        # bindir hardlink dangles.
        original = trim_cache._safe_locked_unlink

        def racing_unlink(path, *, skip_if_nlink_above=None):
            if path == old_a:
                # Simulate concurrent publish-as-hardlink elevating nlink.
                # Hardlink BEFORE the lock is acquired so the in-lock
                # re-stat in the production code observes nlink=2.
                os.link(path, str(bindir / "main"))
            return original(path, skip_if_nlink_above=skip_if_nlink_above)

        monkeypatch.setattr(trim_cache, "_safe_locked_unlink", racing_unlink)

        trimmer = CacheTrimmer(_make_args(keep_count=1))
        trimmer.trim_exedir(exedir)

        # I4: old_a must survive — it gained a hardlinked reference
        # mid-trim. old_b had no such race; it's removed normally.
        assert os.path.exists(old_a), (
            "I4: trim must re-stat nlink under the lock and skip entries that "
            "gained a hardlinked publish reference between scan and unlink"
        )
        assert not os.path.exists(old_b)


# ── --json mode ───────────────────────────────────────────────────────


class TestJsonMode:
    """``--json`` routes human text to stderr and emits a single parseable
    JSON object on stdout with raw integer byte counts per cache."""

    def _run_json(self, argv, capsys):
        """Run main() with --json prepended; return (rc, stdout_str, stderr_str)."""
        rc = main(["--json"] + argv)
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    def test_stdout_is_pure_json_dict_with_no_human_text(self, tmp_path, capsys):
        """Stdout must be a parseable JSON dict and contain no human text.

        A successful json.loads() of the entire stdout buffer is sufficient
        proof that no human summary lines leaked — any mixed prose would
        produce a JSONDecodeError.
        """
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"],
            capsys,
        )
        assert rc == 0
        parsed = json.loads(out)  # raises if human text leaks to stdout
        assert isinstance(parsed, dict)

    def test_byte_counts_are_integers(self, tmp_path, capsys):
        """All byte-count values in the JSON output must be plain integers."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        # Plant a couple of fake object files so there is something to scan.
        _touch_obj(objdir, "foo", "aabbccddeeff", age_seconds=3600)
        _touch_obj(objdir, "foo", "112233445566", age_seconds=0)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"],
            capsys,
        )
        assert rc == 0
        parsed = json.loads(out)
        obj = parsed.get("objdir")
        assert obj is not None
        assert isinstance(obj["bytes_freed"], int)
        assert isinstance(parsed["total_bytes_freed"], int)

    def test_objdir_stats_keys_present(self, tmp_path, capsys):
        """objdir section carries the exact keys print_summary reports."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        _touch_obj(objdir, "bar", "aabbccddeeff", age_seconds=7200)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"],
            capsys,
        )
        assert rc == 0
        obj = json.loads(out)["objdir"]
        for key in (
            "total_scanned",
            "basenames_found",
            "current_kept",
            "noncurrent_kept",
            "removed",
            "failed",
            "bytes_freed",
        ):
            assert key in obj, f"missing key: {key}"

    def test_pchdir_stats_keys_present(self, tmp_path, capsys):
        """pchdir section carries the exact keys print_summary reports."""
        pchdir = str(tmp_path / "pch")
        os.makedirs(pchdir)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-pchdir-only", f"--cas-pchdir={pchdir}"],
            capsys,
        )
        assert rc == 0
        pch = json.loads(out)["pchdir"]
        for key in ("total_dirs_scanned", "headers_found", "dirs_kept", "dirs_removed", "failed", "bytes_freed"):
            assert key in pch, f"missing key: {key}"

    def test_pcmdir_stats_keys_present(self, tmp_path, capsys):
        """pcmdir section carries the exact keys print_summary reports."""
        pcmdir = str(tmp_path / "pcm")
        os.makedirs(pcmdir)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-pcmdir-only", f"--cas-pcmdir={pcmdir}"],
            capsys,
        )
        assert rc == 0
        pcm = json.loads(out)["pcmdir"]
        for key in ("total_dirs_scanned", "buckets_found", "dirs_kept", "dirs_removed", "failed", "bytes_freed"):
            assert key in pcm, f"missing key: {key}"

    def test_exedir_stats_keys_present(self, tmp_path, capsys):
        """exedir section carries the exact keys print_summary reports."""
        exedir = str(tmp_path / "exe")
        os.makedirs(exedir)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-exedir-only", f"--cas-exedir={exedir}"],
            capsys,
        )
        assert rc == 0
        exe = json.loads(out)["exedir"]
        for key in ("total_scanned", "basenames_found", "kept", "removed", "failed", "bytes_freed"):
            assert key in exe, f"missing key: {key}"

    def test_top_level_total_bytes_freed_is_integer(self, tmp_path, capsys):
        """top-level total_bytes_freed must be an integer (sum across caches)."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"],
            capsys,
        )
        assert rc == 0
        parsed = json.loads(out)
        assert "total_bytes_freed" in parsed
        assert isinstance(parsed["total_bytes_freed"], int)

    def test_omitted_cache_absent_from_json(self, tmp_path, capsys):
        """Caches that were not run should be absent (or null) in the JSON."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        rc, out, _err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"],
            capsys,
        )
        assert rc == 0
        parsed = json.loads(out)
        # --cas-objdir-only: pchdir/pcmdir/exedir should be absent or null
        for key in ("pchdir", "pcmdir", "exedir"):
            assert parsed.get(key) is None, f"{key} should be absent/null when not run"

    def test_non_json_mode_prints_human_summary_to_stdout(self, tmp_path, capsys):
        """Without --json, human summary still goes to stdout (regression guard)."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)

        rc = main(["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Cache trim complete" in out

    def test_verbose_progress_goes_to_stderr_in_json_mode(self, tmp_path, capsys):
        """In --json mode, verbose progress lines must not appear on stdout."""
        objdir = str(tmp_path / "obj")
        os.makedirs(objdir)
        _touch_obj(objdir, "baz", "aabbccddeeff", age_seconds=3600)

        rc, out, err = self._run_json(
            ["--dry-run", "--cas-objdir-only", f"--cas-objdir={objdir}", "-v"],
            capsys,
        )
        assert rc == 0
        # stdout must still be pure JSON
        json.loads(out)
        # The verbose "Trimming object directory" line must be on stderr
        assert "Trimming object directory" in err

    def test_summary_json_method_returns_correct_structure(self, tmp_path):
        """CacheTrimmer.summary_json() returns the expected dict directly."""
        trimmer = CacheTrimmer(_make_args())
        objdir_stats = {
            "total_scanned": 10,
            "basenames_found": 3,
            "current_kept": 5,
            "noncurrent_kept": 2,
            "removed": 3,
            "failed": 0,
            "bytes_freed": 4096,
        }
        result = trimmer.summary_json(objdir_stats=objdir_stats)
        assert isinstance(result, dict)
        assert result["objdir"]["bytes_freed"] == 4096
        assert isinstance(result["objdir"]["bytes_freed"], int)
        assert result["total_bytes_freed"] == 4096
        assert isinstance(result["total_bytes_freed"], int)
        assert result.get("pchdir") is None
        assert result.get("pcmdir") is None
        assert result.get("exedir") is None

    def test_total_bytes_freed_sums_per_cache_values(self):
        """total_bytes_freed equals the sum of per-cache bytes_freed values."""
        trimmer = CacheTrimmer(_make_args())
        objdir_stats = {
            "total_scanned": 5,
            "basenames_found": 2,
            "current_kept": 2,
            "noncurrent_kept": 1,
            "removed": 2,
            "failed": 0,
            "bytes_freed": 1024,
        }
        pchdir_stats = {
            "total_dirs_scanned": 3,
            "headers_found": 1,
            "dirs_kept": 1,
            "dirs_removed": 2,
            "failed": 0,
            "bytes_freed": 2048,
        }
        result = trimmer.summary_json(objdir_stats=objdir_stats, pchdir_stats=pchdir_stats)
        assert result["total_bytes_freed"] == objdir_stats["bytes_freed"] + pchdir_stats["bytes_freed"]
        assert result["total_bytes_freed"] == 3072


# ── warn_if_suspicious_cas_dir ────────────────────────────────────────


class TestWarnIfSuspiciousCasDir:
    """``warn_if_suspicious_cas_dir`` emits targeted warnings to stderr (or the
    supplied stream) when a CAS directory is missing or empty in a way that
    suggests a wrong-path / wrong-variant mistake.  It must stay completely
    silent for legitimately empty-and-clean caches."""

    def _pool(self, tmp_path, variant="gcc.debug"):
        """Return a pool directory with ``variant`` as a subdirectory."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        target = pool / variant
        target.mkdir()
        return pool, str(target)

    # ── (a) missing dir + siblings present → warning + hint ──────────

    def test_missing_dir_with_siblings_warns_with_hint(self, tmp_path):
        """Missing target dir, pool has sibling variant dirs → warning with hint."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        (pool / "gcc.release").mkdir()
        (pool / "clang.debug").mkdir()
        target = str(pool / "gcc.debug")  # does NOT exist

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(target, "objdir", "gcc.debug", verbose=0, stream=stream)
        out = stream.getvalue()

        assert "warning:" in out
        assert "not found" in out
        assert target in out
        assert "'gcc.debug'" in out
        assert "sibling variant dirs present" in out
        assert "may be the wrong" in out

    # ── (b) missing dir + NO siblings → warning only, no hint ────────

    def test_missing_dir_no_siblings_warns_without_hint(self, tmp_path):
        """Missing target dir with no siblings → short warning, no hint line."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        target = str(pool / "gcc.debug")  # does NOT exist, pool is otherwise empty

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(target, "objdir", "gcc.debug", verbose=0, stream=stream)
        out = stream.getvalue()

        assert "warning:" in out
        assert "not found" in out
        assert "'gcc.debug'" in out
        # No hint because there are no siblings.
        assert "sibling variant dirs present" not in out
        assert "may be the wrong" not in out

    # ── (c) dir exists, scanned zero, NO siblings → silent ───────────

    def test_empty_dir_no_siblings_is_silent(self, tmp_path):
        """Existing but empty dir with no siblings (a clean cache) → no output."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        target = pool / "gcc.debug"
        target.mkdir()

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(str(target), "objdir", "gcc.debug", verbose=0, stream=stream)

        assert stream.getvalue() == "", "a legitimately empty cache must be silent"

    # ── (d) dir exists, scanned zero, siblings present → "did you mean" ─

    def test_empty_dir_with_siblings_warns_did_you_mean(self, tmp_path):
        """Existing but empty dir, pool has sibling variant dirs → wrong-variant hint."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        (pool / "gcc.release").mkdir()
        target = pool / "gcc.debug"
        target.mkdir()  # exists but is empty (caller guarantees scanned==0)

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(str(target), "pchdir", "gcc.debug", verbose=0, stream=stream)
        out = stream.getvalue()

        assert "warning:" in out
        assert "has no entries to trim" in out
        assert "sibling variant dirs" in out
        assert "'gcc.debug'" in out
        assert "may be the wrong" in out

    # ── quiet mode: verbose < 0 suppresses everything ─────────────────

    def test_quiet_mode_suppresses_warning(self, tmp_path):
        """verbose < 0 → no output regardless of path state."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        (pool / "gcc.release").mkdir()
        target = str(pool / "gcc.debug")  # missing + siblings

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(target, "objdir", "gcc.debug", verbose=-1, stream=stream)

        assert stream.getvalue() == ""

    # ── sibling listing is capped at 5 entries ─────────────────────────

    def test_sibling_listing_capped_at_five(self, tmp_path):
        """With more than 5 sibling dirs, the warning shows at most 5 names."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        siblings = [f"variant{i:02d}" for i in range(10)]
        for s in siblings:
            (pool / s).mkdir()
        target = str(pool / "missing")

        stream = io.StringIO()
        warn_if_suspicious_cas_dir(target, "objdir", "missing", verbose=0, stream=stream)
        out = stream.getvalue()

        # Count commas in the sibling-list portion; 5 names → at most 4 commas.
        # Extract the parenthesised list between "(" and ")".
        m = re.search(r"\(([^)]+)\)", out)
        assert m is not None, f"expected a parenthesised sibling list in: {out!r}"
        listed = [s.strip() for s in m.group(1).split(",")]
        assert len(listed) <= 5, f"must list at most 5 siblings; got {listed}"

    # ── stderr routing: warning goes to stderr, not stdout ─────────────

    def test_warning_goes_to_stderr_not_stdout(self, tmp_path, capsys):
        """Without an explicit stream, the warning should land on stderr."""
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        target = str(pool / "gcc.debug")  # missing

        warn_if_suspicious_cas_dir(target, "objdir", "gcc.debug", verbose=0)
        cap = capsys.readouterr()

        assert "warning:" in cap.err
        assert cap.out == ""

    # ── integration: main() emits warnings to stderr in --json mode ────

    def test_main_json_mode_warning_on_stderr_not_stdout(self, tmp_path, capsys):
        """In --json mode, a missing-dir warning must go to stderr, not stdout.

        We pass an absolute path that cannot exist so ``resolve_cas_directory_arguments``
        leaves it unchanged (it only appends a variant suffix to *unsupplied*
        dirs, not to absolute user-supplied ones) and the warning fires for
        the exact missing path.
        """
        # Build a pool so the parent contains siblings (triggers the hint path).
        pool = tmp_path / "cas-pool"
        pool.mkdir()
        existing_variant = pool / "gcc.release"
        existing_variant.mkdir()
        missing_variant = pool / "gcc.debug"
        # Do NOT create missing_variant — it must be absent.

        rc = main(
            [
                "--json",
                "--dry-run",
                "--cas-objdir-only",
                f"--cas-objdir={missing_variant}",
                "--variant=gcc.debug",
            ]
        )
        cap = capsys.readouterr()

        assert rc == 0
        # stdout must still parse as JSON (no warning bleed)
        json.loads(cap.out)
        # warning on stderr
        assert "warning:" in cap.err


# ── warn_if_wrong_checkout ────────────────────────────────────────────────


class TestWarnIfWrongCheckout:
    """``warn_if_wrong_checkout`` warns on stderr when the object trim was
    almost certainly run from the wrong checkout against a shared network
    pool: no ``--max-age``, network FS, non-empty scan, zero current objects.

    The guard must stay silent in every other combination."""

    # ── helper: build an objdir_stats dict ───────────────────────────────

    @staticmethod
    def _stats(current_kept=0, total_scanned=0):
        return {
            "total_scanned": total_scanned,
            "basenames_found": 0,
            "current_kept": current_kept,
            "noncurrent_kept": 0,
            "removed": 0,
            "failed": 0,
            "bytes_freed": 0,
        }

    # ── guard fires ───────────────────────────────────────────────────────

    def test_fires_on_network_fs_zero_current_no_max_age(self, tmp_path, monkeypatch):
        """All four conditions met → warning emitted to the stream."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=10),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        out = stream.getvalue()
        assert out, "expected a warning but got none"
        assert "warning:" in out

    def test_warning_mentions_checkout_and_max_age(self, tmp_path, monkeypatch):
        """The warning text must explain checkout-relative currency and recommend --max-age."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "nfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=5),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        out = stream.getvalue()
        # Must mention the checkout-relative nature and the --max-age remedy.
        assert "checkout" in out.lower()
        assert "--max-age" in out

    # ── guard does NOT fire: max_age set ─────────────────────────────────

    def test_silent_when_max_age_set(self, tmp_path, monkeypatch):
        """``--max-age`` was given → the guard must stay silent (user is aware)."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=10),
            max_age=30,
            verbose=0,
            stream=stream,
        )
        assert stream.getvalue() == ""

    # ── guard does NOT fire: current_kept > 0 ────────────────────────────

    def test_silent_when_current_kept_nonzero(self, tmp_path, monkeypatch):
        """Objects current for this checkout exist → not a wrong-checkout situation."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=1, total_scanned=10),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        assert stream.getvalue() == ""

    # ── guard does NOT fire: total_scanned == 0 ───────────────────────────

    def test_silent_when_total_scanned_zero(self, tmp_path, monkeypatch):
        """Empty scan → nothing to warn about (warn_if_suspicious_cas_dir handles that)."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=0),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        assert stream.getvalue() == ""

    # ── guard does NOT fire: local (non-network) FS ───────────────────────

    def test_silent_on_local_filesystem(self, tmp_path, monkeypatch):
        """Local-disk FS (ext4) → guard stays silent even with all other conditions met."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "ext4")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: False)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=10),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        assert stream.getvalue() == ""

    def test_silent_on_unknown_filesystem(self, tmp_path, monkeypatch):
        """Unknown FS → treated as local; guard stays silent."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "unknown")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: False)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=10),
            max_age=None,
            verbose=0,
            stream=stream,
        )
        assert stream.getvalue() == ""

    # ── quiet mode (verbose < 0) silences the guard ───────────────────────

    def test_quiet_mode_suppresses_warning(self, tmp_path, monkeypatch):
        """verbose < 0 → no output regardless of conditions."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        stream = io.StringIO()
        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=10),
            max_age=None,
            verbose=-1,
            stream=stream,
        )
        assert stream.getvalue() == ""

    # ── default stream goes to stderr, not stdout ─────────────────────────

    def test_default_stream_is_stderr(self, tmp_path, monkeypatch, capsys):
        """Without an explicit stream kwarg, the warning must land on stderr."""
        monkeypatch.setattr("compiletools.filesystem_utils.get_filesystem_type", lambda _p: "gpfs")
        monkeypatch.setattr("compiletools.filesystem_utils.should_parallelize_scan", lambda _fs: True)

        warn_if_wrong_checkout(
            str(tmp_path),
            self._stats(current_kept=0, total_scanned=5),
            max_age=None,
            verbose=0,
        )
        cap = capsys.readouterr()
        assert "warning:" in cap.err
        assert cap.out == ""


# ── cell_pool_root: trusted pool-root resolver ─────────────────────────────


class TestCellPoolRoot:
    """``cell_pool_root`` climbs from a variant-suffixed cas dir to the pool
    root, but ONLY when the resolved path's basename actually equals the
    variant. Anything else (empty variant, basename != variant — the
    ``_ensure_variant_suffix`` no-op case) is refused with ValueError so a
    later pool-level walk can never climb above the pool it was handed."""

    def test_returns_parent_when_basename_matches_variant(self):
        pool = trim_cache.cell_pool_root("/pool/gcc.debug", "gcc.debug")
        assert pool == "/pool"

    def test_strips_trailing_separator_before_match(self):
        pool = trim_cache.cell_pool_root("/pool/gcc.debug/", "gcc.debug")
        assert pool == "/pool"

    def test_raises_when_basename_differs_from_variant(self):
        # The _ensure_variant_suffix no-op case: a bare pool path whose
        # basename already equalled the variant means the suffix was never
        # appended, so climbing one level would land ABOVE the pool.
        with pytest.raises(ValueError):
            trim_cache.cell_pool_root("/pool/other", "gcc.debug")

    def test_raises_on_empty_variant(self):
        with pytest.raises(ValueError):
            trim_cache.cell_pool_root("/pool/gcc.debug", "")

    def test_raises_on_none_variant(self):
        with pytest.raises(ValueError):
            trim_cache.cell_pool_root("/pool/gcc.debug", None)


# ── enumerate_cells: pool enumeration + classification ─────────────────────


def _make_synthetic_pool(tmp_path, kind):
    """Build a synthetic pool with one of every classification case for ``kind``.

    Returns ``(pool_path, expected)`` where ``expected`` maps cell-name →
    label for the names that MUST appear in the enumeration. Names that must
    NOT appear (the stray top-level bucket, the TraceStore dir, dotfiles) are
    asserted separately by the caller.

    Layout (kind-specific inner structure so cell_shape_ok is exercised):
      * ``good.variant``  — a properly shaped cell of THIS kind (RESOLVABLE
        once resolve_variant is monkeypatched to accept it).
      * ``bogus.variant`` — a properly shaped cell of THIS kind whose name
        does NOT resolve (→ UNRESOLVABLE).
      * ``odd.variant``   — an empty dir: not resolvable, not cell-shaped
        (→ UNKNOWN).
      * ``ab``            — a stray 2-hex bucket directly under the POOL
        (must be SKIPPED, never a cell).
      * ``TraceStore``    — a known non-cell dir (must be SKIPPED).
      * ``.hidden``       — a dotfile dir (must be SKIPPED).
    """
    pool = tmp_path / "pool"
    pool.mkdir()

    def _shape(cell_dir):
        """Plant THIS kind's valid inner structure inside cell_dir."""
        if kind == "obj":
            bucket = cell_dir / "aa"
            bucket.mkdir()
            (bucket / "foo_aabbccddeeff_11223344556677_0011223344556677.o").write_bytes(b"\0" * 100)
        elif kind == "pch":
            inner = cell_dir / ("a" * 16)
            inner.mkdir()
            (inner / "foo.gch").write_bytes(b"\0" * 100)
        elif kind == "pcm":
            inner = cell_dir / ("b" * 16)
            inner.mkdir()
            (inner / "foo.pcm").write_bytes(b"\0" * 100)
        elif kind == "exe":
            bucket = cell_dir / "cc"
            bucket.mkdir()
            (bucket / "foo_deadbeef.exe").write_bytes(b"\0" * 100)
        else:  # pragma: no cover - guard
            raise AssertionError(kind)

    good = pool / "good.variant"
    good.mkdir()
    _shape(good)

    bogus = pool / "bogus.variant"
    bogus.mkdir()
    _shape(bogus)

    odd = pool / "odd.variant"
    odd.mkdir()  # empty: no kind-specific inner structure

    # Stray top-level 2-hex bucket — must be skipped (NOT a cell).
    (pool / "ab").mkdir()
    (pool / "ab" / "anything.o").write_bytes(b"\0" * 10)

    # Known non-cell dirs.
    (pool / "TraceStore").mkdir()
    (pool / ".hidden").mkdir()

    expected = {
        "good.variant": "RESOLVABLE",
        "bogus.variant": "UNRESOLVABLE",
        "odd.variant": "UNKNOWN",
    }
    return str(pool), expected


def _patch_resolver(monkeypatch, resolvable_names):
    """Monkeypatch resolve_variant so only ``resolvable_names`` resolve.

    Deterministic regardless of which bundled axis confs exist on disk:
    a name in the set returns normally; anything else raises
    VariantResolutionError (the exact exception enumerate_cells catches to
    label a cell UNRESOLVABLE). Other exceptions are intentionally NOT
    simulated here — the contract is that they propagate.
    """
    import compiletools.configutils as cu

    def _fake_resolve(name=None, argv=None, **kwargs):
        if name in resolvable_names:
            return object()  # a non-None resolution stand-in
        raise cu.VariantResolutionError(f"no such variant: {name}")

    monkeypatch.setattr(cu, "resolve_variant", _fake_resolve)


class TestEnumerateCells:
    """``enumerate_cells(pool, kind)`` returns one record per candidate cell,
    correctly classified, conservatively skipping non-cell children."""

    @pytest.mark.parametrize("kind", ["obj", "pch", "pcm", "exe"])
    def test_classification_labels(self, tmp_path, kind, monkeypatch):
        pool, expected = _make_synthetic_pool(tmp_path, kind)
        _patch_resolver(monkeypatch, {"good.variant"})

        records = trim_cache.enumerate_cells(pool, kind)
        by_name = {r["name"]: r for r in records}

        for name, label in expected.items():
            assert name in by_name, f"{name} missing from enumeration for kind={kind}"
            assert by_name[name]["label"] == label, (
                f"kind={kind} cell {name}: expected {label}, got {by_name[name]['label']}"
            )

    @pytest.mark.parametrize("kind", ["obj", "pch", "pcm", "exe"])
    def test_stray_bucket_and_tracestore_skipped(self, tmp_path, kind, monkeypatch):
        pool, _expected = _make_synthetic_pool(tmp_path, kind)
        _patch_resolver(monkeypatch, {"good.variant"})

        names = {r["name"] for r in trim_cache.enumerate_cells(pool, kind)}
        # The stray 2-hex top-level bucket must NEVER be treated as a cell.
        assert "ab" not in names
        # Known non-cell dirs must be skipped.
        assert "TraceStore" not in names
        assert ".hidden" not in names

    def test_per_cell_bytes_and_newest_mtime(self, tmp_path, monkeypatch):
        pool, _expected = _make_synthetic_pool(tmp_path, "obj")
        _patch_resolver(monkeypatch, {"good.variant"})

        records = {r["name"]: r for r in trim_cache.enumerate_cells(pool, "obj")}
        good = records["good.variant"]
        # The single planted .o is 100 bytes.
        assert good["total_bytes"] == 100
        assert isinstance(good["newest_mtime"], float)

        # The empty UNKNOWN cell has zero bytes and no files → newest None.
        odd = records["odd.variant"]
        assert odd["total_bytes"] == 0
        assert odd["newest_mtime"] is None

    def test_dotted_composite_name_round_trips_via_own_name(self, tmp_path, monkeypatch):
        """A cell named like a composite variant (with dots) classifies by its
        OWN directory name — the classification primitive is the cell name, not
        any ambient --variant."""
        pool = tmp_path / "pool"
        pool.mkdir()
        cell = pool / "gcc.debug.asan"
        cell.mkdir()
        bucket = cell / "aa"
        bucket.mkdir()
        (bucket / "foo_aabbccddeeff_11223344556677_0011223344556677.o").write_bytes(b"\0" * 50)

        seen = {}

        import compiletools.configutils as cu

        def _fake_resolve(name=None, argv=None, **kwargs):
            seen["name"] = name
            return object()

        monkeypatch.setattr(cu, "resolve_variant", _fake_resolve)

        records = {r["name"]: r for r in trim_cache.enumerate_cells(str(pool), "obj")}
        assert "gcc.debug.asan" in records
        assert records["gcc.debug.asan"]["label"] == "RESOLVABLE"
        # The cell's OWN name was the resolution input.
        assert seen["name"] == "gcc.debug.asan"

    def test_unrelated_exception_propagates(self, tmp_path, monkeypatch):
        """Only VariantResolutionError is caught as 'unresolvable'; any other
        exception from resolve_variant must propagate (never silently
        misclassified as a purge candidate)."""
        pool = tmp_path / "pool"
        pool.mkdir()
        cell = pool / "boom.variant"
        cell.mkdir()
        bucket = cell / "aa"
        bucket.mkdir()
        (bucket / "foo_aabbccddeeff_11223344556677_0011223344556677.o").write_bytes(b"\0" * 50)

        import compiletools.configutils as cu

        def _fake_resolve(name=None, argv=None, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(cu, "resolve_variant", _fake_resolve)

        with pytest.raises(RuntimeError):
            trim_cache.enumerate_cells(str(pool), "obj")

    def test_exe_shape_requires_artefact_suffix_in_bucket(self, tmp_path, monkeypatch):
        """For exe kind a 2-hex bucket alone is not enough — it must contain a
        file with a CAS exe suffix. A bucket of non-artefact files leaves an
        unresolvable cell UNKNOWN, not UNRESOLVABLE."""
        pool = tmp_path / "pool"
        pool.mkdir()
        cell = pool / "bogus.variant"
        cell.mkdir()
        bucket = cell / "cc"
        bucket.mkdir()
        # A file that is NOT a CAS exe artefact.
        (bucket / "notanexe.txt").write_bytes(b"\0" * 10)
        _patch_resolver(monkeypatch, set())  # nothing resolves

        records = {r["name"]: r for r in trim_cache.enumerate_cells(str(pool), "exe")}
        assert records["bogus.variant"]["label"] == "UNKNOWN"


# ── _format_age_days ────────────────────────────────────────────────────────


class TestFormatAgeDays:
    """Unit tests for ``_format_age_days``."""

    def test_none_mtime_renders_dash(self):
        assert trim_cache._format_age_days(None) == "-"

    def test_past_mtime_renders_age(self):
        now = 1_000_000.0
        mtime = now - 5 * 86400  # 5 days ago
        assert trim_cache._format_age_days(mtime, now=now) == "5d"

    def test_zero_age(self):
        now = 1_000_000.0
        assert trim_cache._format_age_days(now, now=now) == "0d"

    def test_future_mtime_clamped_to_zero(self):
        """Clock skew on a shared FS can yield a future mtime; age must not go negative."""
        now = 1_000_000.0
        future_mtime = now + 86400  # 1 day in the future
        assert trim_cache._format_age_days(future_mtime, now=now) == "0d"


# ── --list-unresolvable read-only listing mode ─────────────────────────────


class TestListUnresolvableMode:
    """``--list-unresolvable`` runs a standalone READ-ONLY listing and returns
    0 without trimming anything."""

    def _build_pool(self, tmp_path, monkeypatch):
        """Build an obj pool with a known set of cells; classify only gcc.debug as resolvable.

        Uses ``gcc.debug`` as the resolvable cell because that variant resolves
        against the checkout's real conf hierarchy at parse time (``main`` calls
        ``apptools.parseargs`` which calls the real ``resolve_variant``).
        Classification inside ``enumerate_cells`` is controlled separately by
        patching ``trim_cache._variant_resolvable``, so parse-time resolution
        stays unaffected.

        Pool layout (obj-shaped inner structure):
          * ``gcc.debug``   — valid obj cell → RESOLVABLE (patched classifier)
          * ``bogus.variant`` — valid obj cell → UNRESOLVABLE
          * ``odd.variant``   — empty dir → UNKNOWN
        """
        pool = str(tmp_path / "pool")
        os.makedirs(pool)

        def _obj_shape(cell_dir):
            bucket = os.path.join(cell_dir, "aa")
            os.makedirs(bucket)
            open(
                os.path.join(bucket, "foo_aabbccddeeff_11223344556677_0011223344556677.o"),
                "wb",
            ).close()

        for cell_name in ("gcc.debug", "bogus.variant"):
            cell_dir = os.path.join(pool, cell_name)
            os.makedirs(cell_dir)
            _obj_shape(cell_dir)

        os.makedirs(os.path.join(pool, "odd.variant"))  # empty → UNKNOWN

        # Patch only the CLASSIFICATION helper inside trim_cache, not the
        # parse-time configutils.resolve_variant path.  gcc.debug resolves for
        # real at parse time; bogus.variant and odd.variant do not, but they
        # never reach apptools.parseargs — they are cells in the pool, not the
        # active variant.
        resolvable = {"gcc.debug"}
        monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: name in resolvable)

        return pool

    def test_json_output_shape(self, tmp_path, monkeypatch, capsys):
        pool = self._build_pool(tmp_path, monkeypatch)
        # cas-objdir points at the resolvable cell; --variant matches its
        # basename so _ensure_variant_suffix is a no-op and cell_pool_root
        # climbs to the pool.
        objdir = os.path.join(pool, "gcc.debug")
        rc = main(
            [
                "--json",
                "--list-unresolvable",
                "--cas-objdir-only",
                f"--cas-objdir={objdir}",
                "--variant=gcc.debug",
            ]
        )
        cap = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(cap.out)
        assert isinstance(parsed, dict)
        # Schema marker and mode field.
        assert parsed.get("schema") == 1
        assert parsed.get("mode") == "list-unresolvable"
        # The obj cache section is present and lists cells with labels/sizes.
        obj = parsed.get("objdir")
        assert obj is not None
        cells = {c["name"]: c for c in obj["cells"]}
        assert cells["gcc.debug"]["label"] == "RESOLVABLE"
        assert cells["bogus.variant"]["label"] == "UNRESOLVABLE"
        assert cells["odd.variant"]["label"] == "UNKNOWN"
        # Raw int bytes in JSON mode.
        assert isinstance(cells["gcc.debug"]["total_bytes"], int)
        # Per-label byte rollups are integers.
        assert isinstance(obj["unresolvable_bytes"], int)
        assert isinstance(obj["unknown_bytes"], int)
        # bogus.variant (UNRESOLVABLE) and odd.variant (UNKNOWN) both have
        # 0-byte files in _build_pool, so rollups are 0.
        assert obj["unresolvable_bytes"] == sum(c["total_bytes"] for c in obj["cells"] if c["label"] == "UNRESOLVABLE")
        assert obj["unknown_bytes"] == sum(c["total_bytes"] for c in obj["cells"] if c["label"] == "UNKNOWN")

    def test_human_table_to_stdout(self, tmp_path, monkeypatch, capsys):
        pool = self._build_pool(tmp_path, monkeypatch)
        objdir = os.path.join(pool, "gcc.debug")
        rc = main(
            [
                "--list-unresolvable",
                "--cas-objdir-only",
                f"--cas-objdir={objdir}",
                "--variant=gcc.debug",
            ]
        )
        cap = capsys.readouterr()
        assert rc == 0
        # Human report on stdout names each label and cell.
        assert "UNRESOLVABLE" in cap.out
        assert "bogus.variant" in cap.out
        assert "gcc.debug" in cap.out

    def test_is_read_only_deletes_nothing(self, tmp_path, monkeypatch, capsys):
        pool = self._build_pool(tmp_path, monkeypatch)
        objdir = os.path.join(pool, "gcc.debug")

        # Snapshot every file under the pool before the listing.
        def _snapshot(root):
            out = set()
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    out.add(os.path.join(dirpath, f))
            return out

        before = _snapshot(pool)
        rc = main(
            [
                "--list-unresolvable",
                "--cas-objdir-only",
                f"--cas-objdir={objdir}",
                "--variant=gcc.debug",
            ]
        )
        capsys.readouterr()
        after = _snapshot(pool)
        assert rc == 0
        assert before == after, "--list-unresolvable must not delete or create any files"

    def test_untrusted_pool_root_diagnostic_does_not_abort(self, tmp_path, monkeypatch, capsys):
        """If cell_pool_root refuses for one cache (basename != variant or
        empty variant), a diagnostic goes to stderr and the listing continues
        across the OTHER caches without aborting the whole run.

        We drive ``list_unresolvable_cells`` directly with an args namespace
        whose objdir basename disagrees with the variant (untrusted → refused)
        while the pchdir is trusted, and assert: a stderr diagnostic for objdir,
        no exception, and the pchdir section still produced.
        """
        pool, _expected = _make_synthetic_pool(tmp_path, "pch")
        _patch_resolver(monkeypatch, {"good.variant"})

        args = _make_args(
            list_unresolvable=True,
            variant="good.variant",
            # objdir basename ('mismatch') != variant → cell_pool_root refuses.
            cas_objdir=os.path.join(pool, "mismatch"),
            cas_pchdir=os.path.join(pool, "good.variant"),  # trusted
            cas_pcmdir=os.path.join(pool, "good.variant"),
            cas_exedir=os.path.join(pool, "good.variant"),
            cas_objdir_only=False,
            cas_pchdir_only=False,
            cas_pcmdir_only=False,
            cas_exedir_only=False,
        )
        result = trim_cache.list_unresolvable_cells(args)
        cap = capsys.readouterr()
        # A diagnostic for the untrusted objdir went to stderr.
        assert cap.err != ""
        # ...but the pch section was still produced (no whole-run abort).
        assert result.get("pchdir") is not None


# ── --purge-unresolvable: DESTRUCTIVE orphan reclamation ───────────────────


def _make_purge_pool(tmp_path, monkeypatch, *, warm_age_seconds, cold_age_seconds):
    """Build an obj pool with the full classification matrix for purge tests.

    Returns ``(pool, paths)`` where ``paths`` maps a logical role to the artefact
    path planted under that cell, so callers can assert presence/absence after a
    purge. ``trim_cache._variant_resolvable`` is patched so ONLY ``gcc.debug``
    resolves (matching the resolvable cell used by the CLI's real parse-time
    resolver).

    Cells planted (all obj-shaped except where noted):
      * ``gcc.debug``       — RESOLVABLE, cold      → never purged.
      * ``cold.variant``    — UNRESOLVABLE + cold   → THE purge target.
      * ``warm.variant``    — UNRESOLVABLE + warm   → spared (peer-owned guard).
      * ``odd.variant``     — empty dir → UNKNOWN   → never purged.
      * ``ab``              — stray 2-hex pool bucket → never a cell.
      * ``TraceStore``      — known non-cell dir    → never touched.
    """
    pool = str(tmp_path / "pool")
    os.makedirs(pool)
    paths = {}

    def _plant_obj(cell_name, *, age_seconds, role):
        cell_dir = os.path.join(pool, cell_name)
        bucket = os.path.join(cell_dir, "aa")
        os.makedirs(bucket)
        artefact = os.path.join(bucket, "foo_aabbccddeeff_11223344556677_0011223344556677.o")
        with open(artefact, "wb") as f:
            f.write(b"\0" * 100)
        if age_seconds:
            mtime = time.time() - age_seconds
            os.utime(artefact, (mtime, mtime))
        paths[role] = artefact
        return cell_dir

    _plant_obj("gcc.debug", age_seconds=cold_age_seconds, role="resolvable_cold")
    _plant_obj("cold.variant", age_seconds=cold_age_seconds, role="cold_target")
    _plant_obj("warm.variant", age_seconds=warm_age_seconds, role="warm_spared")

    # Empty UNKNOWN cell (no obj-shaped inner structure).
    os.makedirs(os.path.join(pool, "odd.variant"))
    paths["unknown_cell"] = os.path.join(pool, "odd.variant")

    # Stray 2-hex pool bucket (never a cell).
    os.makedirs(os.path.join(pool, "ab"))
    stray = os.path.join(pool, "ab", "stray.o")
    with open(stray, "wb") as f:
        f.write(b"\0" * 10)
    paths["stray_bucket_file"] = stray

    # TraceStore non-cell dir.
    os.makedirs(os.path.join(pool, "TraceStore"))
    trace_file = os.path.join(pool, "TraceStore", "trace.bin")
    with open(trace_file, "wb") as f:
        f.write(b"\0" * 10)
    paths["tracestore_file"] = trace_file

    resolvable = {"gcc.debug"}
    monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: name in resolvable)

    return pool, paths


class TestPurgeUnresolvable:
    """``--purge-unresolvable`` reclaims UNRESOLVABLE + COLD cells ONLY, with a
    mandatory coldness gate and leaf-level lock-safe removal."""

    def _objdir(self, pool):
        # cas-objdir points at the resolvable cell; --variant matches its
        # basename so cell_pool_root climbs to the pool.
        return os.path.join(pool, "gcc.debug")

    def test_purges_only_cold_unresolvable_cell(self, tmp_path, monkeypatch, capsys):
        # warm = 1 day old, cold = 30 days old, cutoff = 7 days.
        pool, paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)
        rc = main(
            [
                "--purge-unresolvable",
                "--max-age=7",
                "--cas-objdir-only",
                f"--cas-objdir={self._objdir(pool)}",
                "--variant=gcc.debug",
            ]
        )
        capsys.readouterr()
        assert rc == 0
        # The cold unresolvable cell is GONE (cell dir removed).
        assert not os.path.exists(os.path.join(pool, "cold.variant"))
        # Everything else survives.
        assert os.path.exists(paths["resolvable_cold"]), "RESOLVABLE cell must never be purged"
        assert os.path.exists(paths["warm_spared"]), "warm UNRESOLVABLE cell must be spared"
        assert os.path.exists(paths["unknown_cell"]), "UNKNOWN cell must never be purged"
        assert os.path.exists(paths["stray_bucket_file"]), "stray 2-hex pool bucket must never be touched"
        assert os.path.exists(paths["tracestore_file"]), "TraceStore must never be touched"

    def test_warm_unresolvable_reported_skipped(self, tmp_path, monkeypatch, capsys):
        pool, _paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)
        rc = main(
            [
                "--json",
                "--purge-unresolvable",
                "--max-age=7",
                "--cas-objdir-only",
                f"--cas-objdir={self._objdir(pool)}",
                "--variant=gcc.debug",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed.get("schema") == 1
        assert parsed.get("mode") == "purge-unresolvable"
        obj = parsed["objdir"]
        assert obj["cells_purged"] == 1
        assert obj["cells_skipped_warm"] == 1
        assert obj["cells_deferred"] == 0
        assert isinstance(obj["bytes_freed"], int)
        assert parsed["total_bytes_freed"] == obj["bytes_freed"]

    def test_hard_error_without_max_age(self, tmp_path, monkeypatch, capsys):
        pool, paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)
        rc = main(
            [
                "--purge-unresolvable",
                "--cas-objdir-only",
                f"--cas-objdir={self._objdir(pool)}",
                "--variant=gcc.debug",
            ]
        )
        cap = capsys.readouterr()
        assert rc == 1
        assert "max-age" in cap.err.lower()
        # Nothing removed — the cold cell still exists.
        assert os.path.exists(paths["cold_target"])
        assert os.path.exists(os.path.join(pool, "cold.variant"))

    def test_dry_run_reports_but_removes_nothing(self, tmp_path, monkeypatch, capsys):
        pool, paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)

        def _snapshot(root):
            out = set()
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    out.add(os.path.join(dirpath, f))
            return out

        before = _snapshot(pool)
        rc = main(
            [
                "--json",
                "--dry-run",
                "--purge-unresolvable",
                "--max-age=7",
                "--cas-objdir-only",
                f"--cas-objdir={self._objdir(pool)}",
                "--variant=gcc.debug",
            ]
        )
        out = capsys.readouterr().out
        after = _snapshot(pool)
        assert rc == 0
        assert before == after, "--dry-run must remove nothing"
        parsed = json.loads(out)
        # The candidate is still REPORTED as a (would-be) purge.
        assert parsed["objdir"]["cells_purged"] == 1
        assert parsed["objdir"]["bytes_freed"] == 100
        # ...but the bytes are still on disk.
        assert os.path.exists(paths["cold_target"])

    def test_coldness_boundary_at_and_past_cutoff(self, tmp_path, monkeypatch):
        """A cell newer than the cutoff is warm (spared); a cell at/just past
        the cutoff is cold (purged)."""
        # cutoff = 7 days. just_warm = 6 days (newer than cutoff → spared),
        # just_cold = 8 days (older than cutoff → purged).
        pool = str(tmp_path / "pool")
        os.makedirs(pool)

        def _plant(cell_name, age_days):
            bucket = os.path.join(pool, cell_name, "aa")
            os.makedirs(bucket)
            art = os.path.join(bucket, "foo_aabbccddeeff_11223344556677_0011223344556677.o")
            with open(art, "wb") as f:
                f.write(b"\0" * 10)
            mt = time.time() - age_days * 86400
            os.utime(art, (mt, mt))

        _plant("justwarm.variant", 6)
        _plant("justcold.variant", 8)
        monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: False)

        args = _make_args(
            variant="justwarm.variant",
            max_age=7,
            cas_objdir=os.path.join(pool, "justwarm.variant"),
            cas_objdir_only=True,
        )
        result = trim_cache.purge_unresolvable_cells(args)
        obj = result["objdir"]
        assert obj["cells_purged"] == 1
        assert obj["cells_skipped_warm"] == 1
        # justcold purged, justwarm spared.
        assert not os.path.exists(os.path.join(pool, "justcold.variant"))
        assert os.path.exists(os.path.join(pool, "justwarm.variant"))

    def test_empty_unresolvable_cell_is_cold(self, tmp_path, monkeypatch):
        """An UNRESOLVABLE cell with no artefacts (newest_mtime is None) is
        treated as cold and purged — but only if it is cell-shaped. An empty
        dir is UNKNOWN, not UNRESOLVABLE, so it is NOT purged; this asserts the
        cold-by-None rule on a *shaped* cell whose single artefact we delete
        first to leave newest_mtime None."""
        pool = str(tmp_path / "pool")
        os.makedirs(pool)
        # Cell-shaped but the bucket directory is empty (shape predicate only
        # needs a 2-hex subdir to exist for obj). newest_mtime → None.
        bucket = os.path.join(pool, "empty.variant", "aa")
        os.makedirs(bucket)
        monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: False)

        args = _make_args(
            variant="empty.variant",
            max_age=7,
            cas_objdir=os.path.join(pool, "empty.variant"),
            cas_objdir_only=True,
        )
        result = trim_cache.purge_unresolvable_cells(args)
        assert result["objdir"]["cells_purged"] == 1
        assert result["objdir"]["bytes_freed"] == 0  # empty cell has no files to count
        assert not os.path.exists(os.path.join(pool, "empty.variant"))

    def test_lock_safety_defers_locked_artifact_leaf_level(self, tmp_path, monkeypatch, capsys):
        """CRITICAL lock-safety proof.

        An artefact TWO LEVELS DOWN (inside a bucket, not at the cell top) is
        held by a peer. We simulate the peer by making the leaf-level removal
        helper refuse (return False) for THAT bucket only — exactly what the
        real ``_safe_locked_rmtree`` returns when it cannot acquire the lock on
        the bucket's contained sidecar.

        This proves:
          1. Leaf-level descent: the helper is invoked with the BUCKET path
             (cell/<2hex>), NEVER the cell root. If the production code wrongly
             called ``_safe_locked_rmtree`` on the cell root, the spy below
             would record the cell path and the assertion would fail.
          2. The locked artefact still exists afterward (refused removal).
          3. The cell is NOT rmtree'd as a root — it survives, partially
             populated.
          4. The run reports the cell as DEFERRED, not as a hard failure.
        """
        pool = str(tmp_path / "pool")
        os.makedirs(pool)
        cell_dir = os.path.join(pool, "cold.variant")
        bucket = os.path.join(cell_dir, "aa")
        os.makedirs(bucket)
        artefact = os.path.join(bucket, "foo_aabbccddeeff_11223344556677_0011223344556677.o")
        with open(artefact, "wb") as f:
            f.write(b"\0" * 100)
        mt = time.time() - 30 * 86400
        os.utime(artefact, (mt, mt))
        monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: False)

        # Spy on the leaf-level rmtree: record every path it is asked to remove,
        # and refuse (return False) for the locked bucket — the peer-active
        # signal. The cell root must NEVER appear in seen_paths.
        seen_paths = []
        real_rmtree = trim_cache._safe_locked_rmtree

        def _spy_rmtree(dir_path):
            seen_paths.append(dir_path)
            if os.path.realpath(dir_path) == os.path.realpath(bucket):
                return False  # peer holds the lock on the contained sidecar
            return real_rmtree(dir_path)

        monkeypatch.setattr(trim_cache, "_safe_locked_rmtree", _spy_rmtree)

        args = _make_args(
            variant="cold.variant",
            max_age=7,
            cas_objdir=cell_dir,
            cas_objdir_only=True,
        )
        result = trim_cache.purge_unresolvable_cells(args)
        capsys.readouterr()

        # (1) Leaf-level descent: helper saw the BUCKET, never the cell root.
        assert any(os.path.realpath(p) == os.path.realpath(bucket) for p in seen_paths)
        assert all(os.path.realpath(p) != os.path.realpath(cell_dir) for p in seen_paths), (
            "_safe_locked_rmtree must NEVER be called on a cell root"
        )
        # (2) The locked artefact still exists.
        assert os.path.exists(artefact)
        # (3) The cell was not rmtree'd as a root — it survives.
        assert os.path.isdir(cell_dir)
        # (4) Deferred, NOT a hard failure.
        obj = result["objdir"]
        assert obj["cells_deferred"] == 1
        assert obj["cells_purged"] == 0


class TestPurgeUnresolvableModeExclusivity:
    """Mode-exclusivity contract for the two standalone pool-level modes.

    The authoritative decision:
      * The four ``--cas-*-only`` flags are still "at most one" (existing guard).
      * A single ``--cas-*-only`` flag MAY be combined with ``--list-unresolvable``
        OR ``--purge-unresolvable``; it SCOPES that pool mode to the one selected
        cache. This combination is ALLOWED.
      * ``--list-unresolvable`` and ``--purge-unresolvable`` are MUTUALLY
        EXCLUSIVE WITH EACH OTHER (the only NEW guard).
    """

    def test_list_and_purge_together_error(self):
        """The one NEW guard: the two pool modes cannot run in the same call."""
        rc = main(["--list-unresolvable", "--purge-unresolvable", "--max-age=7"])
        assert rc == 1

    def test_purge_with_single_cas_only_is_allowed(self, tmp_path, monkeypatch, capsys):
        """``--purge-unresolvable --cas-objdir-only`` is ALLOWED — the
        ``--cas-objdir-only`` flag SCOPES the purge to the object pool."""
        pool, _paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)
        rc = main(
            [
                "--json",
                "--purge-unresolvable",
                "--max-age=7",
                "--cas-objdir-only",
                f"--cas-objdir={os.path.join(pool, 'gcc.debug')}",
                "--variant=gcc.debug",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, "purge + a single --cas-*-only scope flag must be allowed"
        parsed = json.loads(out)
        # Scoped to obj: obj ran, the other caches did not.
        assert parsed["objdir"] is not None
        assert parsed["pchdir"] is None
        assert parsed["pcmdir"] is None
        assert parsed["exedir"] is None

    def test_purge_scoped_to_objdir_only_purges_obj_orphans(self, tmp_path, monkeypatch, capsys):
        """``--purge-unresolvable --cas-objdir-only`` purges only obj orphans
        and never touches a sibling pcm pool."""
        pool, _paths = _make_purge_pool(tmp_path, monkeypatch, warm_age_seconds=86400, cold_age_seconds=30 * 86400)
        # A separate pcm pool with a cold unresolvable cell that MUST be spared
        # because the purge is scoped to objdir only.
        pcm_pool = str(tmp_path / "pcmpool")
        pcm_cell = os.path.join(pcm_pool, "cold.variant")
        pcm_inner = os.path.join(pcm_cell, "b" * 16)
        os.makedirs(pcm_inner)
        pcm_art = os.path.join(pcm_inner, "mod.pcm")
        with open(pcm_art, "wb") as f:
            f.write(b"\0" * 100)
        mt = time.time() - 30 * 86400
        os.utime(pcm_art, (mt, mt))
        # cell_pool_root needs a resolvable cell at <pcm_pool>/<variant>; reuse
        # gcc.debug as the suffix target.
        os.makedirs(os.path.join(pcm_pool, "gcc.debug"))

        rc = main(
            [
                "--purge-unresolvable",
                "--max-age=7",
                "--cas-objdir-only",
                f"--cas-objdir={os.path.join(pool, 'gcc.debug')}",
                f"--cas-pcmdir={os.path.join(pcm_pool, 'gcc.debug')}",
                "--variant=gcc.debug",
            ]
        )
        capsys.readouterr()
        assert rc == 0
        # obj orphan purged.
        assert not os.path.exists(os.path.join(pool, "cold.variant"))
        # pcm orphan untouched — purge was scoped to objdir only.
        assert os.path.exists(pcm_art)

    def test_list_with_single_cas_only_is_allowed(self, tmp_path, monkeypatch, capsys):
        """``--list-unresolvable --cas-objdir-only`` is ALLOWED — it scopes the
        listing to the object pool (matches the committed Task-4 list tests)."""
        pool = str(tmp_path / "pool")
        os.makedirs(os.path.join(pool, "gcc.debug", "aa"))
        monkeypatch.setattr(trim_cache, "_variant_resolvable", lambda name: name == "gcc.debug")
        rc = main(
            [
                "--json",
                "--list-unresolvable",
                "--cas-objdir-only",
                f"--cas-objdir={os.path.join(pool, 'gcc.debug')}",
                "--variant=gcc.debug",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, "list + a single --cas-*-only scope flag must be allowed"
        parsed = json.loads(out)
        assert parsed["objdir"] is not None
        assert parsed["pchdir"] is None
