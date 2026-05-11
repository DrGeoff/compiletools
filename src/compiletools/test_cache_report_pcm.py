"""Tests for the PCM-cache duplication reporting in
``compiletools.cache_report``.
"""

from __future__ import annotations

import json
import pathlib

from compiletools import cache_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm_entry(
    pcm_root,
    cmd_hash,
    bucket_key,
    *,
    stage="clang_module_interface",
    pcm_size=50,
    manifest_pad=0,
    write_manifest=True,
    bad_manifest=False,
):
    """Create a fake PCM cmd_hash directory.

    Layout matches trim_cache: ``<pcm_root>/<cmd_hash>/<name>.pcm`` plus
    ``manifest.json`` carrying ``bucket_key``, ``stage``, and
    ``transitive_hashes``. ``manifest_pad`` adds whitespace bytes so
    callers can pin total dir size for waste-math tests.
    """
    d = pathlib.Path(pcm_root) / cmd_hash
    d.mkdir(exist_ok=True)
    (d / "module.pcm").write_bytes(b"x" * pcm_size)
    if write_manifest:
        if bad_manifest:
            (d / "manifest.json").write_text("{not: valid")
        else:
            payload = json.dumps(
                {
                    "bucket_key": bucket_key,
                    "stage": stage,
                    "transitive_hashes": [],
                }
            )
            if manifest_pad > 0:
                payload = payload + (" " * manifest_pad)
            (d / "manifest.json").write_text(payload)
    return d


# ---------------------------------------------------------------------------
# scan_pcmdir
# ---------------------------------------------------------------------------


def test_scan_pcmdir_empty(tmp_path):
    assert cache_report.scan_pcmdir(str(tmp_path)) == []


def test_scan_pcmdir_missing_root(tmp_path):
    assert cache_report.scan_pcmdir(str(tmp_path / "does-not-exist")) == []


def test_scan_pcmdir_finds_cmd_hash_dirs_with_manifest(tmp_path):
    cmd_hash = "aabbccddeeff0011"
    bucket_key = "/abs/path/foo.cppm"
    d = _make_pcm_entry(tmp_path, cmd_hash, bucket_key, pcm_size=50)

    entries = cache_report.scan_pcmdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.cmd_hash_dir == str(d)
    assert e.cmd_hash == cmd_hash
    assert e.bucket_key == bucket_key
    assert e.stage == "clang_module_interface"
    expected_size = 50 + (d / "manifest.json").stat().st_size
    assert e.size_bytes == expected_size


def test_scan_pcmdir_skips_non_cmd_hash_entries(tmp_path):
    # Real PCM caches contain a ``.module-mapper.txt`` and may have
    # other clutter. Only 16-hex-char dirs should be scanned.
    (tmp_path / ".module-mapper.txt").write_text("std /tmp/std.pcm\n")
    (tmp_path / "legacy_dir").mkdir()
    (tmp_path / "aa").mkdir()  # 2-char (looks like obj bucket, not pcm cmd_hash)
    _make_pcm_entry(tmp_path, "ffeeddccbbaa9988", "/m/kept.cppm")

    entries = cache_report.scan_pcmdir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].cmd_hash == "ffeeddccbbaa9988"


def test_scan_pcmdir_handles_missing_manifest(tmp_path):
    cmd_hash = "0123456789abcdef"
    _make_pcm_entry(tmp_path, cmd_hash, "ignored", pcm_size=30, write_manifest=False)

    entries = cache_report.scan_pcmdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    # Orphans tagged with cmd_hash so unrelated lost manifests don't
    # collapse into one fake duplicate group.
    assert e.bucket_key == f"<unknown:{cmd_hash}>"
    assert e.stage == ""
    assert e.size_bytes == 30


def test_scan_pcmdir_handles_corrupt_manifest(tmp_path):
    cmd_hash = "fedcba9876543210"  # pragma: allowlist secret
    d = _make_pcm_entry(tmp_path, cmd_hash, "ignored", pcm_size=40, bad_manifest=True)

    entries = cache_report.scan_pcmdir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.bucket_key == f"<unknown:{cmd_hash}>"
    assert e.size_bytes == 40 + (d / "manifest.json").stat().st_size


def test_scan_pcmdir_recognises_header_unit_bucket_keys(tmp_path):
    # Header units use the verbatim token as bucket_key, e.g. ``<vector>``.
    _make_pcm_entry(tmp_path, "1111111111111111", "<vector>", stage="clang_header_unit")
    entries = cache_report.scan_pcmdir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].bucket_key == "<vector>"
    assert entries[0].stage == "clang_header_unit"


# ---------------------------------------------------------------------------
# group_pcm_by_bucket_key
# ---------------------------------------------------------------------------


def test_group_pcm_by_bucket_key_groups_by_bucket(tmp_path):
    _make_pcm_entry(tmp_path, "1111111111111111", "/m/A.cppm")
    _make_pcm_entry(tmp_path, "2222222222222222", "/m/A.cppm")
    _make_pcm_entry(tmp_path, "3333333333333333", "/m/B.cppm")

    entries = cache_report.scan_pcmdir(str(tmp_path))
    groups = cache_report.group_pcm_by_bucket_key(entries)
    assert set(groups.keys()) == {"/m/A.cppm", "/m/B.cppm"}
    assert len(groups["/m/A.cppm"]) == 2
    assert len(groups["/m/B.cppm"]) == 1


# ---------------------------------------------------------------------------
# pcm_report
# ---------------------------------------------------------------------------


def test_pcm_report_no_duplication(tmp_path):
    for i in range(5):
        cmd_hash = f"{i:016x}"
        _make_pcm_entry(tmp_path, cmd_hash, f"/m/mod{i}.cppm")

    rep = cache_report.pcm_report(str(tmp_path))
    assert rep.pcmdir == str(tmp_path)
    assert rep.total_entries == 5
    assert rep.unique_buckets_count == 5
    assert rep.duplicated_groups == []
    assert rep.wasted_bytes == 0


def test_pcm_report_with_duplication_computes_waste(tmp_path):
    # Pad manifests so each cmd_hash dir totals exactly 200 bytes.
    # PCM manifests are larger than PCH manifests because they carry
    # ``stage`` and ``transitive_hashes`` in addition to the bucket key.
    pcm_size = 100
    target_dir_bytes = 200
    a_payload = json.dumps({"bucket_key": "/m/A.cppm", "stage": "clang_module_interface", "transitive_hashes": []})
    a_pad = (target_dir_bytes - pcm_size) - len(a_payload)
    b_payload = json.dumps({"bucket_key": "/m/B.cppm", "stage": "clang_module_interface", "transitive_hashes": []})
    b_pad = (target_dir_bytes - pcm_size) - len(b_payload)
    assert a_pad >= 0 and b_pad >= 0, "test fixture too small; raise target_dir_bytes"

    _make_pcm_entry(tmp_path, "1111111111111111", "/m/A.cppm", pcm_size=pcm_size, manifest_pad=a_pad)
    _make_pcm_entry(tmp_path, "2222222222222222", "/m/A.cppm", pcm_size=pcm_size, manifest_pad=a_pad)
    _make_pcm_entry(tmp_path, "3333333333333333", "/m/B.cppm", pcm_size=pcm_size, manifest_pad=b_pad)
    _make_pcm_entry(tmp_path, "4444444444444444", "/m/B.cppm", pcm_size=pcm_size, manifest_pad=b_pad)

    rep = cache_report.pcm_report(str(tmp_path))
    assert rep.total_entries == 4
    assert rep.total_bytes == 4 * target_dir_bytes
    assert rep.unique_buckets_count == 2
    assert len(rep.duplicated_groups) == 2
    # Per group: sum=2*target_dir_bytes, min=target_dir_bytes -> wasted=target_dir_bytes.
    # Two groups -> 2 * target_dir_bytes.
    assert rep.wasted_bytes == 2 * target_dir_bytes


def test_pcm_report_orphans_dont_collapse(tmp_path):
    # Two manifest-less cmd_hash dirs must NOT be flagged as duplicates
    # of each other.
    _make_pcm_entry(tmp_path, "1111111111111111", "ignored", write_manifest=False)
    _make_pcm_entry(tmp_path, "2222222222222222", "ignored", write_manifest=False)

    rep = cache_report.pcm_report(str(tmp_path))
    assert rep.unique_buckets_count == 2
    assert rep.duplicated_groups == []


def test_top_pcm_buckets_by_waste_orders_descending(tmp_path):
    # Wasted-bytes for a 2-variant group = sum-min = one full entry size
    # (pcm + manifest). Manifest sizes within a bucket are identical,
    # so across-bucket comparisons need to factor manifest length too.
    _make_pcm_entry(tmp_path, "1111111111111111", "/m/big.cppm", pcm_size=1000)
    _make_pcm_entry(tmp_path, "2222222222222222", "/m/big.cppm", pcm_size=1000)
    _make_pcm_entry(tmp_path, "3333333333333333", "/m/small.cppm", pcm_size=50)
    _make_pcm_entry(tmp_path, "4444444444444444", "/m/small.cppm", pcm_size=50)

    rep = cache_report.pcm_report(str(tmp_path))
    top = cache_report.top_pcm_buckets_by_waste(rep, n=10)
    assert [t[0] for t in top] == ["/m/big.cppm", "/m/small.cppm"]
    assert top[0][2] > top[1][2]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_pcmdir_only_text(tmp_path, capsys):
    rc = cache_report.main([f"--cas-pcmdir={tmp_path}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PCM cache report for" in out
    assert "Object cache report for" not in out
    assert "PCH cache report for" not in out
    assert "Linker-artefact cache report for" not in out


def test_cli_all_four_flags_text(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    pcmdir = tmp_path / "pcm"
    exedir = tmp_path / "exe"
    for d in (objdir, pchdir, pcmdir, exedir):
        d.mkdir()
    rc = cache_report.main(
        [
            f"--cas-objdir={objdir}",
            f"--cas-pchdir={pchdir}",
            f"--cas-pcmdir={pcmdir}",
            f"--cas-exedir={exedir}",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Object cache report for" in out
    assert "PCH cache report for" in out
    assert "PCM cache report for" in out
    assert "Linker-artefact cache report for" in out


def test_cli_pcmdir_only_json_uses_combined_schema(tmp_path, capsys):
    rc = cache_report.main([f"--cas-pcmdir={tmp_path}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    # Combined schema includes all four keys, three of which are None.
    assert data["cas-objdir-report"] is None
    assert data["cas-pchdir-report"] is None
    assert data["cas-pcmdir-report"] is not None
    assert data["cas-exedir-report"] is None


def test_cli_combined_json_includes_all_four(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    pcmdir = tmp_path / "pcm"
    exedir = tmp_path / "exe"
    for d in (objdir, pchdir, pcmdir, exedir):
        d.mkdir()
    rc = cache_report.main(
        [
            f"--cas-objdir={objdir}",
            f"--cas-pchdir={pchdir}",
            f"--cas-pcmdir={pcmdir}",
            f"--cas-exedir={exedir}",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["cas-objdir-report"] is not None
    assert data["cas-pchdir-report"] is not None
    assert data["cas-pcmdir-report"] is not None
    assert data["cas-exedir-report"] is not None


def test_cli_objdir_only_json_still_flat(tmp_path, capsys):
    # Back-compat: --cas-objdir alone still emits the legacy flat shape
    # even though pcmdir reporting was added.
    rc = cache_report.main([f"--cas-objdir={tmp_path}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "cas-objdir" in data
    assert "cas-objdir-report" not in data
