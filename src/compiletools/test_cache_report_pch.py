"""Tests for the PCH-cache duplication reporting in ``compiletools.cache_report``."""

from __future__ import annotations

import json
import pathlib

from compiletools import cache_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pch_entry(pch_root, cmd_hash, header_realpath, gch_size=50, manifest_pad=0):
    """Create a fake PCH cmd_hash directory with a .gch and a manifest.

    ``manifest_pad`` adds ``b' '*manifest_pad`` whitespace to the JSON to
    make the on-disk manifest size predictable for tests that assert on
    total bytes inside a cmd_hash dir.
    """
    d = pathlib.Path(pch_root) / cmd_hash
    d.mkdir(exist_ok=True)
    (d / "header.gch").write_bytes(b"x" * gch_size)
    payload = json.dumps({"header_realpath": header_realpath})
    if manifest_pad > 0:
        payload = payload + (" " * manifest_pad)
    (d / "manifest.json").write_text(payload)
    return d


# ---------------------------------------------------------------------------
# scan_pchdir
# ---------------------------------------------------------------------------


def test_scan_pchdir_empty(tmp_path):
    assert cache_report.scan_pchdir(str(tmp_path)) == []


def test_scan_pchdir_finds_cmd_hash_dirs_with_manifest(tmp_path):
    cmd_hash = "aabbccddeeff0011"
    header_realpath = "/abs/path/foo.h"
    d = _make_pch_entry(tmp_path, cmd_hash, header_realpath, gch_size=50)

    entries = cache_report.scan_pchdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.cmd_hash_dir == str(d)
    assert e.cmd_hash == cmd_hash
    assert e.header_realpath == header_realpath
    # Bytes = gch (50) + manifest.json on-disk size.
    expected_size = 50 + (d / "manifest.json").stat().st_size
    assert e.size_bytes == expected_size


def test_scan_pchdir_skips_non_cmd_hash_entries(tmp_path):
    # Legacy / clutter entries that should be silently ignored.
    (tmp_path / "legacy_dir").mkdir()
    (tmp_path / "legacy_dir" / "foo.gch").write_bytes(b"junk")
    # A 2-char dir (looks like an objdir bucket, not a cmd_hash).
    (tmp_path / "aa").mkdir()
    (tmp_path / "aa" / "stuff.gch").write_bytes(b"junk")
    # Stray top-level file.
    (tmp_path / "stray.txt").write_bytes(b"hi")
    # One legitimate cmd_hash dir.
    _make_pch_entry(tmp_path, "ffeeddccbbaa9988", "/h/kept.h")

    entries = cache_report.scan_pchdir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].cmd_hash == "ffeeddccbbaa9988"


def test_scan_pchdir_handles_missing_manifest(tmp_path):
    cmd_hash = "0123456789abcdef"
    d = pathlib.Path(tmp_path) / cmd_hash
    d.mkdir()
    (d / "header.gch").write_bytes(b"y" * 30)
    # NO manifest.json.

    entries = cache_report.scan_pchdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.cmd_hash == cmd_hash
    # Orphans are tagged with their cmd_hash so unrelated lost manifests
    # don't get grouped together as duplicates.
    assert e.header_realpath == f"<unknown:{cmd_hash}>"
    assert e.size_bytes == 30


def test_scan_pchdir_handles_corrupt_manifest(tmp_path):
    cmd_hash = "fedcba9876543210"
    d = pathlib.Path(tmp_path) / cmd_hash
    d.mkdir()
    (d / "header.gch").write_bytes(b"z" * 40)
    (d / "manifest.json").write_text("{not: valid: json")

    entries = cache_report.scan_pchdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.header_realpath == f"<unknown:{cmd_hash}>"
    assert e.size_bytes == 40 + (d / "manifest.json").stat().st_size


# ---------------------------------------------------------------------------
# group_pch_by_header
# ---------------------------------------------------------------------------


def test_group_pch_by_header_groups_by_realpath(tmp_path):
    _make_pch_entry(tmp_path, "1111111111111111", "/h/A.h")
    _make_pch_entry(tmp_path, "2222222222222222", "/h/A.h")
    _make_pch_entry(tmp_path, "3333333333333333", "/h/B.h")

    entries = cache_report.scan_pchdir(str(tmp_path))
    groups = cache_report.group_pch_by_header(entries)
    assert set(groups.keys()) == {"/h/A.h", "/h/B.h"}
    assert len(groups["/h/A.h"]) == 2
    assert len(groups["/h/B.h"]) == 1


# ---------------------------------------------------------------------------
# pch_report
# ---------------------------------------------------------------------------


def test_pch_report_no_duplication(tmp_path):
    for i in range(5):
        cmd_hash = f"{i:016x}"
        _make_pch_entry(tmp_path, cmd_hash, f"/h/header{i}.h")

    rep = cache_report.pch_report(str(tmp_path))
    assert rep.pchdir == str(tmp_path)
    assert rep.total_entries == 5
    assert rep.unique_headers_count == 5
    assert rep.duplicated_groups == []
    assert rep.wasted_bytes == 0


def test_pch_report_with_duplication_computes_waste(tmp_path):
    # Pad manifests so each cmd_hash dir totals exactly 100 bytes.
    # Manifest JSON for /h/A.h:
    a_payload = json.dumps({"header_realpath": "/h/A.h"})
    a_pad = 50 - len(a_payload)
    b_payload = json.dumps({"header_realpath": "/h/B.h"})
    b_pad = 50 - len(b_payload)
    assert a_pad >= 0 and b_pad >= 0, "test fixture too small; raise the totals"

    _make_pch_entry(tmp_path, "1111111111111111", "/h/A.h", gch_size=50, manifest_pad=a_pad)
    _make_pch_entry(tmp_path, "2222222222222222", "/h/A.h", gch_size=50, manifest_pad=a_pad)
    _make_pch_entry(tmp_path, "3333333333333333", "/h/B.h", gch_size=50, manifest_pad=b_pad)
    _make_pch_entry(tmp_path, "4444444444444444", "/h/B.h", gch_size=50, manifest_pad=b_pad)

    rep = cache_report.pch_report(str(tmp_path))
    assert rep.total_entries == 4
    assert rep.total_bytes == 400
    assert rep.unique_headers_count == 2
    assert len(rep.duplicated_groups) == 2
    # Per group: sum=200, min=100 -> wasted=100. Two groups -> 200.
    assert rep.wasted_bytes == 200


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_pchdir_only(tmp_path, capsys):
    rc = cache_report.main([f"--cas-pchdir={tmp_path}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PCH cache report for" in out
    assert "Object cache report for" not in out


def test_cli_objdir_only(tmp_path, capsys):
    rc = cache_report.main([f"--cas-objdir={tmp_path}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Object cache report for" in out
    assert "PCH cache report for" not in out


def test_cli_both_flags(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    objdir.mkdir()
    pchdir.mkdir()
    rc = cache_report.main([f"--cas-objdir={objdir}", f"--cas-pchdir={pchdir}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Object cache report for" in out
    assert "PCH cache report for" in out


def test_cli_no_args_scans_variant_defaults(monkeypatch, tmp_path, capsys):
    """Post-apptools migration: a no-args invocation no longer errors.

    Instead it scans whichever variant-default CAS dirs exist on disk
    (peer behaviour with ct-trim-cache). Run the test from an empty
    tmp_path so the variant-default paths resolve to non-existent
    directories — the report should run cleanly and produce no output.
    """
    monkeypatch.chdir(tmp_path)
    rc = cache_report.main([])
    assert rc == 0
    out = capsys.readouterr().out
    # Empty variant defaults -> nothing to scan -> empty text output.
    assert out == ""


def test_cli_json_includes_both_reports(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    objdir.mkdir()
    pchdir.mkdir()
    rc = cache_report.main([f"--cas-objdir={objdir}", f"--cas-pchdir={pchdir}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "cas-objdir-report" in data
    assert "cas-pchdir-report" in data
    assert data["cas-objdir-report"] is not None
    assert data["cas-pchdir-report"] is not None
