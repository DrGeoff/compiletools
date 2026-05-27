"""Tests for the linker-artefact (cas-exedir) reporting in
``compiletools.cache_report``.
"""

from __future__ import annotations

import json
import pathlib

from compiletools import cache_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exe_entry(
    exe_root,
    basename,
    link_key,
    suffix=".exe",
    size=100,
    source_realpath=None,
    *,
    bad_manifest=False,
):
    """Create a fake cas-exedir entry under ``exe_root/<linkkey[:2]>/``.

    Returns the path to the artefact file. Writes a ``.manifest`` sidecar
    iff ``source_realpath`` is not None; ``bad_manifest=True`` writes
    corrupt JSON instead of a valid payload. Mirrors the
    ``write_manifest``/``bad_manifest`` shape of ``_make_pch_entry`` and
    ``_make_pcm_entry`` in the sibling test files.
    """
    bucket = pathlib.Path(exe_root) / link_key[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    artefact = bucket / f"{basename}_{link_key}{suffix}"
    artefact.write_bytes(b"x" * size)
    if source_realpath is not None:
        manifest = bucket / f"{basename}_{link_key}{suffix}.manifest"
        if bad_manifest:
            manifest.write_text("{not: json")
        else:
            manifest.write_text(json.dumps({"source_realpath": source_realpath}))
    return artefact


# ---------------------------------------------------------------------------
# scan_exedir
# ---------------------------------------------------------------------------


def test_scan_exedir_empty(tmp_path):
    assert cache_report.scan_exedir(str(tmp_path)) == []


def test_scan_exedir_missing_root(tmp_path):
    assert cache_report.scan_exedir(str(tmp_path / "does-not-exist")) == []


def test_scan_exedir_finds_entry_with_manifest(tmp_path):
    link_key = "aabbccdd11223344" + "0" * 48  # 64 hex chars total
    artefact = _make_exe_entry(
        tmp_path,
        basename="myexe",
        link_key=link_key,
        suffix=".exe",
        size=100,
        source_realpath="/abs/path/main.cpp",
    )

    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.path == str(artefact)
    assert e.basename == "myexe"
    assert e.suffix == ".exe"
    assert e.link_key == link_key
    assert e.source_realpath == "/abs/path/main.cpp"
    assert e.size_bytes == 100


def test_scan_exedir_skips_non_bucket_top_level(tmp_path):
    # Stray top-level file.
    (tmp_path / "stray.txt").write_bytes(b"hi")
    # Non-2-hex top-level dir.
    (tmp_path / "weird").mkdir()
    (tmp_path / "weird" / "thing.exe").write_bytes(b"ignored")
    # One legitimate entry.
    _make_exe_entry(
        tmp_path,
        basename="kept",
        link_key="ff" + "0" * 62,
        source_realpath="/h/kept.cpp",
    )

    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].basename == "kept"


def test_scan_exedir_skips_lock_and_manifest_sidecars(tmp_path):
    link_key = "11" + "0" * 62
    bucket = tmp_path / "11"
    bucket.mkdir()
    # Lock sidecars and manifest file should never become entries.
    (bucket / f"main_{link_key}.exe.lock").write_bytes(b"")
    (bucket / f"main_{link_key}.exe.lock.excl").write_bytes(b"")
    (bucket / f"main_{link_key}.exe.manifest").write_text("{}")
    # No actual .exe present -> 0 entries.

    entries = cache_report.scan_exedir(str(tmp_path))
    assert entries == []


def test_scan_exedir_handles_missing_manifest(tmp_path):
    link_key = "22" + "0" * 62
    _make_exe_entry(
        tmp_path,
        basename="legacy",
        link_key=link_key,
        suffix=".exe",
        size=42,
        source_realpath=None,
    )

    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    # Orphans tagged with basename+suffix so unrelated lost manifests
    # don't get grouped together as duplicates.
    assert e.source_realpath == "<unknown:legacy:.exe>"
    assert e.size_bytes == 42


def test_scan_exedir_handles_corrupt_manifest(tmp_path):
    link_key = "33" + "0" * 62
    _make_exe_entry(
        tmp_path,
        basename="broken",
        link_key=link_key,
        suffix=".so",
        size=20,
        source_realpath="/ignored",
        bad_manifest=True,
    )

    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].source_realpath == "<unknown:broken:.so>"


def test_scan_exedir_handles_basename_with_underscore(tmp_path):
    # The split is on the LAST underscore so basenames like ``my_tool`` survive.
    link_key = "44" + "0" * 62
    _make_exe_entry(
        tmp_path,
        basename="my_tool_v2",
        link_key=link_key,
        suffix=".exe",
        source_realpath="/src/my_tool_v2.cpp",
    )
    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 1
    assert entries[0].basename == "my_tool_v2"
    assert entries[0].link_key == link_key


def test_scan_exedir_recognises_all_suffixes(tmp_path):
    for i, suffix in enumerate((".exe", ".a", ".so")):
        link_key = f"{i:02x}" + "0" * 62
        _make_exe_entry(
            tmp_path,
            basename=f"art{i}",
            link_key=link_key,
            suffix=suffix,
            source_realpath=f"/src/art{i}.cpp",
        )
    entries = cache_report.scan_exedir(str(tmp_path))
    assert len(entries) == 3
    suffixes = sorted(e.suffix for e in entries)
    assert suffixes == [".a", ".exe", ".so"]


# ---------------------------------------------------------------------------
# group_exe_by_source
# ---------------------------------------------------------------------------


def test_group_exe_by_source_separates_suffixes(tmp_path):
    # libfoo.a and libfoo.so legitimately coexist for the same source
    # — they must NOT collapse into one bucket.
    src = "/src/libfoo.cpp"
    _make_exe_entry(tmp_path, "libfoo", "11" + "0" * 62, ".a", source_realpath=src)
    _make_exe_entry(tmp_path, "libfoo", "22" + "0" * 62, ".so", source_realpath=src)

    entries = cache_report.scan_exedir(str(tmp_path))
    groups = cache_report.group_exe_by_source(entries)
    assert set(groups.keys()) == {(src, ".a"), (src, ".so")}
    assert len(groups[(src, ".a")]) == 1
    assert len(groups[(src, ".so")]) == 1


def test_group_exe_by_source_collects_link_key_variants(tmp_path):
    src = "/src/main.cpp"
    _make_exe_entry(tmp_path, "main", "11" + "0" * 62, ".exe", source_realpath=src)
    _make_exe_entry(tmp_path, "main", "22" + "0" * 62, ".exe", source_realpath=src)
    _make_exe_entry(tmp_path, "main", "33" + "0" * 62, ".exe", source_realpath=src)

    entries = cache_report.scan_exedir(str(tmp_path))
    groups = cache_report.group_exe_by_source(entries)
    assert list(groups.keys()) == [(src, ".exe")]
    assert len(groups[(src, ".exe")]) == 3


# ---------------------------------------------------------------------------
# exe_report
# ---------------------------------------------------------------------------


def test_exe_report_no_duplication(tmp_path):
    for i in range(4):
        link_key = f"{i:02x}" + "0" * 62
        _make_exe_entry(
            tmp_path,
            basename=f"exe{i}",
            link_key=link_key,
            suffix=".exe",
            size=100,
            source_realpath=f"/src/exe{i}.cpp",
        )

    rep = cache_report.exe_report(str(tmp_path))
    assert rep.exedir == str(tmp_path)
    assert rep.total_entries == 4
    assert rep.total_bytes == 400
    assert rep.unique_buckets_count == 4
    assert rep.duplicated_groups == []
    assert rep.wasted_bytes == 0


def test_exe_report_with_duplication_computes_waste(tmp_path):
    src_a = "/src/a.cpp"
    src_b = "/src/b.cpp"
    # Two link_key variants of src_a (each 100 B) and two of src_b (each 100 B).
    _make_exe_entry(tmp_path, "a", "11" + "0" * 62, ".exe", size=100, source_realpath=src_a)
    _make_exe_entry(tmp_path, "a", "22" + "0" * 62, ".exe", size=100, source_realpath=src_a)
    _make_exe_entry(tmp_path, "b", "33" + "0" * 62, ".exe", size=100, source_realpath=src_b)
    _make_exe_entry(tmp_path, "b", "44" + "0" * 62, ".exe", size=100, source_realpath=src_b)

    rep = cache_report.exe_report(str(tmp_path))
    assert rep.total_entries == 4
    assert rep.total_bytes == 400
    assert rep.unique_buckets_count == 2
    assert len(rep.duplicated_groups) == 2
    # Per group: sum=200, min=100 -> wasted=100. Two groups -> 200.
    assert rep.wasted_bytes == 200


def test_exe_report_orphans_dont_collapse(tmp_path):
    # Two manifest-less entries with different basenames must NOT be
    # treated as duplicates of each other (they get distinct
    # ``<unknown:...>`` source_realpath tags).
    _make_exe_entry(tmp_path, "alpha", "11" + "0" * 62, ".exe", source_realpath=None)
    _make_exe_entry(tmp_path, "beta", "22" + "0" * 62, ".exe", source_realpath=None)

    rep = cache_report.exe_report(str(tmp_path))
    assert rep.unique_buckets_count == 2
    assert rep.duplicated_groups == []


def test_top_exe_sources_by_waste_orders_descending(tmp_path):
    src_big = "/src/big.cpp"
    src_small = "/src/small.cpp"
    # big: 2 variants of 1000 B each -> wasted 1000
    _make_exe_entry(tmp_path, "big", "11" + "0" * 62, ".exe", size=1000, source_realpath=src_big)
    _make_exe_entry(tmp_path, "big", "22" + "0" * 62, ".exe", size=1000, source_realpath=src_big)
    # small: 2 variants of 50 B each -> wasted 50
    _make_exe_entry(tmp_path, "small", "33" + "0" * 62, ".exe", size=50, source_realpath=src_small)
    _make_exe_entry(tmp_path, "small", "44" + "0" * 62, ".exe", size=50, source_realpath=src_small)

    rep = cache_report.exe_report(str(tmp_path))
    top = cache_report.top_exe_sources_by_waste(rep, n=10)
    assert [(t[0], t[1], t[3]) for t in top] == [
        (src_big, ".exe", 1000),
        (src_small, ".exe", 50),
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exedir_only_text(tmp_path, capsys):
    rc = cache_report.main([f"--cas-exedir={tmp_path}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Linker-artefact cache report for" in out
    assert "Object cache report for" not in out
    assert "PCH cache report for" not in out


def test_cli_all_three_flags_text(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    exedir = tmp_path / "exe"
    objdir.mkdir()
    pchdir.mkdir()
    exedir.mkdir()
    rc = cache_report.main(
        [
            f"--cas-objdir={objdir}",
            f"--cas-pchdir={pchdir}",
            f"--cas-exedir={exedir}",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Object cache report for" in out
    assert "PCH cache report for" in out
    assert "Linker-artefact cache report for" in out


def test_cli_exedir_only_json_uses_combined_schema(tmp_path, capsys):
    rc = cache_report.main([f"--cas-exedir={tmp_path}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "cas-objdir-report" in data
    assert "cas-pchdir-report" in data
    assert "cas-exedir-report" in data
    assert data["cas-objdir-report"] is None
    assert data["cas-pchdir-report"] is None
    assert data["cas-exedir-report"] is not None


def test_cli_objdir_only_json_stays_flat(tmp_path, capsys):
    # Back-compat: --cas-objdir alone keeps the legacy flat schema.
    rc = cache_report.main([f"--cas-objdir={tmp_path}", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "cas-objdir" in data  # flat shape
    assert "cas-objdir-report" not in data


def test_cli_combined_json_includes_all_three(tmp_path, capsys):
    objdir = tmp_path / "obj"
    pchdir = tmp_path / "pch"
    exedir = tmp_path / "exe"
    objdir.mkdir()
    pchdir.mkdir()
    exedir.mkdir()
    rc = cache_report.main(
        [
            f"--cas-objdir={objdir}",
            f"--cas-pchdir={pchdir}",
            f"--cas-exedir={exedir}",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["cas-objdir-report"] is not None
    assert data["cas-pchdir-report"] is not None
    assert data["cas-exedir-report"] is not None
