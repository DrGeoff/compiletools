"""Tests for ``compiletools.cache_report``."""

from __future__ import annotations

import json
import pathlib

import pytest

from compiletools import cache_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_obj(
    path_root,
    file_hash,
    dep_hash,
    macro_state_hash,
    basename="myobj",
    size=100,
):
    """Create a fake object file in the bucket-sharded layout.

    Returns the absolute path of the created file.
    """
    bucket = file_hash[:2]
    name = f"{basename}_{file_hash}_{dep_hash}_{macro_state_hash}.o"
    bucket_dir = pathlib.Path(path_root) / bucket
    bucket_dir.mkdir(exist_ok=True)
    path = bucket_dir / name
    path.write_bytes(b"x" * size)
    return str(path)


# ---------------------------------------------------------------------------
# scan_objdir
# ---------------------------------------------------------------------------


def test_scan_objdir_empty(tmp_path):
    assert cache_report.scan_objdir(str(tmp_path)) == []


def test_scan_objdir_finds_objects_in_buckets(tmp_path):
    # file_hash 12 hex, dep_hash 14 hex, macro_state_hash 16 hex
    file_hash = "aabbccddeeff"
    dep_hash = "11223344556677"
    macro_state_hash = "00112233aabbccdd"
    path = _make_obj(tmp_path, file_hash, dep_hash, macro_state_hash, basename="foo", size=42)

    entries = cache_report.scan_objdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.path == path
    assert e.basename == "foo"
    assert e.file_hash == file_hash
    assert e.dep_hash == dep_hash
    assert e.macro_state_hash == macro_state_hash
    assert e.size_bytes == 42


def test_scan_objdir_skips_non_bucket_dirs(tmp_path):
    # Non-bucket top-level dirs to be ignored.
    (tmp_path / "TraceStore").mkdir()
    (tmp_path / "TraceStore" / "something.o").write_bytes(b"junk")
    (tmp_path / "diagnostics").mkdir()
    (tmp_path / "diagnostics" / "deeper").mkdir()
    (tmp_path / "diagnostics" / "deeper" / "ignored.o").write_bytes(b"junk")
    # Stray top-level file (not even a dir) — also ignored.
    (tmp_path / "stray.txt").write_bytes(b"hi")

    # One legitimate bucket entry.
    _make_obj(tmp_path, "aabbccddeeff", "11223344556677", "00112233aabbccdd", basename="kept")

    entries = cache_report.scan_objdir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].basename == "kept"


def test_scan_objdir_skips_unparseable_filenames(tmp_path):
    bucket = tmp_path / "ab"
    bucket.mkdir()
    (bucket / "garbage.o").write_bytes(b"junk")
    (bucket / "alsobad_xxxxxxxxxxxx_yyyyyyyyyyyyyy_zzzzzzzzzzzzzzzz.o").write_bytes(b"junk")
    # A valid file alongside.
    _make_obj(tmp_path, "abcdefabcdef", "11223344556677", "00112233aabbccdd", basename="ok")

    entries = cache_report.scan_objdir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].basename == "ok"


# ---------------------------------------------------------------------------
# group_by_src_deps
# ---------------------------------------------------------------------------


def test_group_by_src_deps_groups_by_hashes(tmp_path):
    file_hash_ab = "aabbccddeeff"
    dep_hash_ab = "11223344556677"
    file_hash_c = "112233445566"
    dep_hash_c = "aabbccddeeff11"
    _make_obj(tmp_path, file_hash_ab, dep_hash_ab, "0000000000000001", basename="src1")
    _make_obj(tmp_path, file_hash_ab, dep_hash_ab, "0000000000000002", basename="src1")
    _make_obj(tmp_path, file_hash_c, dep_hash_c, "0000000000000003", basename="src2")

    entries = cache_report.scan_objdir(str(tmp_path))
    groups = cache_report.group_by_src_deps(entries)
    assert len(groups) == 2
    assert len(groups[(file_hash_ab, dep_hash_ab)]) == 2
    assert len(groups[(file_hash_c, dep_hash_c)]) == 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_no_duplication(tmp_path):
    for i in range(5):
        fh = f"{i:012x}"
        dh = f"{i:014x}"
        mh = f"{i:016x}"
        _make_obj(tmp_path, fh, dh, mh, basename=f"src{i}", size=50)

    rep = cache_report.report(str(tmp_path))
    assert rep.total_entries == 5
    assert rep.total_bytes == 5 * 50
    assert rep.unique_src_deps_count == 5
    assert rep.duplicated_groups == []
    assert rep.wasted_bytes == 0


def test_report_with_duplication_computes_waste(tmp_path):
    # Two pairs that share (file_hash, dep_hash).
    _make_obj(tmp_path, "aaaaaaaaaaaa", "11111111111111", "0000000000000001", basename="A", size=100)
    _make_obj(tmp_path, "aaaaaaaaaaaa", "11111111111111", "0000000000000002", basename="A", size=100)
    _make_obj(tmp_path, "bbbbbbbbbbbb", "22222222222222", "0000000000000003", basename="B", size=100)
    _make_obj(tmp_path, "bbbbbbbbbbbb", "22222222222222", "0000000000000004", basename="B", size=100)

    rep = cache_report.report(str(tmp_path))
    assert rep.total_entries == 4
    assert rep.total_bytes == 400
    assert rep.unique_src_deps_count == 2
    assert len(rep.duplicated_groups) == 2
    # Each group: sum=200, min=100 -> wasted=100. Two groups -> 200.
    assert rep.wasted_bytes == 200


def test_report_top_basenames(tmp_path):
    # Three groups, varying duplication per basename.
    # basename "Big": one group with 3 variants of 100 bytes -> wasted 200
    _make_obj(tmp_path, "aaaaaaaaaaaa", "11111111111111", "0000000000000001", basename="Big", size=100)
    _make_obj(tmp_path, "aaaaaaaaaaaa", "11111111111111", "0000000000000002", basename="Big", size=100)
    _make_obj(tmp_path, "aaaaaaaaaaaa", "11111111111111", "0000000000000003", basename="Big", size=100)
    # basename "Med": one group with 2 variants of 50 bytes -> wasted 50
    _make_obj(tmp_path, "bbbbbbbbbbbb", "22222222222222", "0000000000000001", basename="Med", size=50)
    _make_obj(tmp_path, "bbbbbbbbbbbb", "22222222222222", "0000000000000002", basename="Med", size=50)
    # basename "Solo": single entry, no waste
    _make_obj(tmp_path, "cccccccccccc", "33333333333333", "0000000000000001", basename="Solo", size=10)

    rep = cache_report.report(str(tmp_path))
    top = cache_report.top_basenames_by_waste(rep, n=10)
    # Only basenames with >0 wasted bytes appear, sorted by wasted desc.
    assert [b.basename for b in top] == ["Big", "Med"]
    assert top[0].wasted_bytes == 200
    assert top[0].variants == 3
    assert top[1].wasted_bytes == 50
    assert top[1].variants == 2

    # Top-1 returns only the biggest.
    top1 = cache_report.top_basenames_by_waste(rep, n=1)
    assert len(top1) == 1
    assert top1[0].basename == "Big"


# ---------------------------------------------------------------------------
# _format_bytes
# ---------------------------------------------------------------------------


def test_format_bytes_units():
    assert cache_report._format_bytes(0) == "0 B"
    assert cache_report._format_bytes(1500) == "1.46 KB"
    assert cache_report._format_bytes(2 * 1024**3) == "2.00 GB"
    # Mid-range MB.
    assert cache_report._format_bytes(5 * 1024 * 1024) == "5.00 MB"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_text_output_minimal(tmp_path, capsys):
    rc = cache_report.main([f"--cas-objdir={tmp_path}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Object cache report for" in out
    assert "Total entries:" in out
    assert "0" in out


def test_cli_json_output_round_trips(tmp_path, capsys):
    # The cache_report resolver auto-appends /<variant> to --cas-objdir.
    # Drive the variant via --config=<extras.conf> so impliedvariant short-circuits
    # to "extras" without needing to find composable axis conf files on the host.
    conf = tmp_path / "extras.conf"
    conf.write_text("")
    variant_dir = tmp_path / "extras"
    variant_dir.mkdir()
    _make_obj(variant_dir, "aaaaaaaaaaaa", "11111111111111", "0000000000000001", basename="A", size=100)
    _make_obj(variant_dir, "aaaaaaaaaaaa", "11111111111111", "0000000000000002", basename="A", size=100)
    _make_obj(variant_dir, "bbbbbbbbbbbb", "22222222222222", "0000000000000003", basename="B", size=200)

    # --config=<extras.conf> makes impliedvariant short-circuit axis
    # canonicalization; --variant=extras then ensures args.variant after
    # cap.parse_args wins over whatever the host's ct.conf set.
    rc = cache_report.main([f"--cas-objdir={tmp_path}", f"--config={conf}", "--variant=extras", "--json"])
    out = capsys.readouterr().out
    assert rc == 0

    data = json.loads(out)
    assert data["cas-objdir"] == str(variant_dir)
    assert data["total-entries"] == 3
    assert data["total-bytes"] == 400
    assert data["unique-src-deps-count"] == 2
    # Only the (A,A) group is duplicated.
    assert data["duplicated-groups-count"] == 1
    assert data["wasted-bytes"] == 100
    assert "top-basenames" in data
    assert isinstance(data["top-basenames"], list)


def test_cli_help_runs():
    with pytest.raises(SystemExit) as excinfo:
        cache_report.main(["--help"])
    assert excinfo.value.code == 0
