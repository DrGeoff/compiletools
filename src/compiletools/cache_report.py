"""Measure duplication in shared object and PCH caches.

Walks a ``cas-objdir`` and groups entries by ``(file_hash, dep_hash)``.
Entries that share that pair but differ in ``macro_state_hash`` are
bit-identical duplicates spawned by command-line ``-D`` macro pollution
of the cache key.

Walks a ``cas-pchdir`` and groups ``<cmd_hash>/`` entries by their
manifest's ``header_realpath``. Two ``cmd_hash`` dirs that share a
header but differ in ``cmd_hash`` are PCH-cache duplicates from the
same kind of pollution.

This module is standalone — it imports only stdlib plus
``parse_object_filename`` / ``_PCH_COMMAND_HASH_RE`` / ``_load_pch_manifest``
from ``trim_cache`` (the single source of truth for the on-disk formats).
No Hunter / MagicFlags / BuildContext dependencies, so it stays cheap to
import and easy to test.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

from compiletools.trim_cache import (
    _OBJ_BUCKET_RE,
    _PCH_COMMAND_HASH_RE,
    _load_pch_manifest,
    parse_object_filename,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model — objdir
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectFileEntry:
    """One parsed entry from an object CAS."""

    path: str
    basename: str
    file_hash: str  # 12 hex chars
    dep_hash: str  # 14 hex chars
    macro_state_hash: str  # 16 hex chars
    size_bytes: int


@dataclass(frozen=True)
class DuplicateGroup:
    """A (file_hash, dep_hash) tuple that has multiple macro_state_hash variants."""

    file_hash: str
    dep_hash: str
    basename: str
    variants: list[ObjectFileEntry] = field(hash=False, compare=False)


@dataclass(frozen=True)
class BasenameWaste:
    """Aggregate duplication waste for a single basename."""

    basename: str
    variants: int  # total number of duplicate entries (sum of group sizes for this basename)
    wasted_bytes: int


@dataclass(frozen=True)
class CacheReport:
    """Structured summary of a cas-objdir scan."""

    objdir: str
    total_entries: int
    total_bytes: int
    unique_src_deps_count: int
    duplicated_groups: list[DuplicateGroup] = field(hash=False, compare=False)
    wasted_bytes: int = 0


# ---------------------------------------------------------------------------
# Data model — pchdir
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PchEntry:
    """One parsed entry from a PCH CAS."""

    cmd_hash_dir: str  # absolute path to <pchdir>/<cmd_hash>
    cmd_hash: str  # 16 hex chars
    header_realpath: str  # from manifest "header_realpath", or "<unknown>"
    size_bytes: int  # total bytes of files inside cmd_hash_dir


@dataclass(frozen=True)
class PchDuplicateGroup:
    """A header_realpath that has multiple cmd_hash variants."""

    header_realpath: str
    variants: list[PchEntry] = field(hash=False, compare=False)


@dataclass(frozen=True)
class PchReport:
    """Structured summary of a cas-pchdir scan."""

    pchdir: str
    total_entries: int  # total cmd_hash dirs
    total_bytes: int
    unique_headers_count: int  # distinct header_realpath values
    duplicated_groups: list[PchDuplicateGroup] = field(hash=False, compare=False)
    wasted_bytes: int = 0


# ---------------------------------------------------------------------------
# Scanning — objdir
# ---------------------------------------------------------------------------


def scan_objdir(objdir: str) -> list[ObjectFileEntry]:
    """Walk ``objdir`` and return one ``ObjectFileEntry`` per parseable object.

    Top-level entries that don't match the bucket-dir convention
    (2 hex chars) are silently skipped — this includes ``TraceStore/``,
    ``diagnostics/`` dirs, stray top-level files, etc.

    Within each bucket, files that don't end in ``.o`` or whose names
    don't match the content-addressable object format are silently
    skipped (consistent with ``trim_cache``).
    """
    entries: list[ObjectFileEntry] = []
    if not os.path.isdir(objdir):
        return entries

    try:
        with os.scandir(objdir) as top_iter:
            buckets = [e.path for e in top_iter if _OBJ_BUCKET_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return entries

    for bucket_path in buckets:
        try:
            with os.scandir(bucket_path) as bucket_iter:
                for entry in bucket_iter:
                    name = entry.name
                    if not name.endswith(".o"):
                        continue
                    parsed = parse_object_filename(name)
                    if parsed is None:
                        continue
                    basename, file_hash, dep_hash, macro_state_hash = parsed
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    entries.append(
                        ObjectFileEntry(
                            path=entry.path,
                            basename=basename,
                            file_hash=file_hash,
                            dep_hash=dep_hash,
                            macro_state_hash=macro_state_hash,
                            size_bytes=size,
                        )
                    )
        except OSError:
            continue  # bucket disappeared mid-scan, best-effort

    return entries


def group_by_src_deps(
    entries: Iterable[ObjectFileEntry],
) -> dict[tuple[str, str], list[ObjectFileEntry]]:
    """Group entries by ``(file_hash, dep_hash)``.

    Each group represents one ``(source, transitive-deps)`` tuple.
    Multiple entries in a group means there are macro_state_hash variants
    for the same source+deps — i.e., duplicates from key pollution.
    """
    groups: dict[tuple[str, str], list[ObjectFileEntry]] = {}
    for e in entries:
        groups.setdefault((e.file_hash, e.dep_hash), []).append(e)
    return groups


def report(objdir: str) -> CacheReport:
    """Produce a structured report about cache duplication in ``objdir``."""
    entries = scan_objdir(objdir)
    groups = group_by_src_deps(entries)

    total_entries = len(entries)
    total_bytes = sum(e.size_bytes for e in entries)
    unique_src_deps_count = len(groups)

    duplicated: list[DuplicateGroup] = []
    wasted_bytes = 0
    for (fh, dh), variants in groups.items():
        if len(variants) <= 1:
            continue
        # Verify the basename invariant: all entries in a (file_hash, dep_hash)
        # group should share a basename (same source file). If they don't,
        # log a warning and use the first.
        basenames = {v.basename for v in variants}
        if len(basenames) > 1:
            logger.warning(
                "Cache group (%s, %s) has multiple basenames: %s",
                fh,
                dh,
                sorted(basenames),
            )
        rep_basename = variants[0].basename
        sizes = [v.size_bytes for v in variants]
        wasted_bytes += sum(sizes) - min(sizes)
        duplicated.append(
            DuplicateGroup(
                file_hash=fh,
                dep_hash=dh,
                basename=rep_basename,
                variants=list(variants),
            )
        )

    return CacheReport(
        objdir=objdir,
        total_entries=total_entries,
        total_bytes=total_bytes,
        unique_src_deps_count=unique_src_deps_count,
        duplicated_groups=duplicated,
        wasted_bytes=wasted_bytes,
    )


def top_basenames_by_waste(rep: CacheReport, n: int = 10) -> list[BasenameWaste]:
    """Aggregate per-basename waste across all duplicated groups, sorted desc.

    Returns at most ``n`` entries. A basename is included only if it has
    nonzero wasted bytes (i.e., it appears in at least one group with >1
    variant). ``variants`` is the maximum variant-count seen across this
    basename's groups (so a basename that has one 4-variant group and
    one 2-variant group reports ``variants=4``).
    """
    by_basename: dict[str, dict] = {}
    for grp in rep.duplicated_groups:
        sizes = [v.size_bytes for v in grp.variants]
        wasted = sum(sizes) - min(sizes)
        slot = by_basename.setdefault(grp.basename, {"wasted": 0, "max_variants": 0})
        slot["wasted"] += wasted
        if len(grp.variants) > slot["max_variants"]:
            slot["max_variants"] = len(grp.variants)

    items = [
        BasenameWaste(basename=bn, variants=info["max_variants"], wasted_bytes=info["wasted"])
        for bn, info in by_basename.items()
        if info["wasted"] > 0
    ]
    items.sort(key=lambda b: (-b.wasted_bytes, b.basename))
    return items[:n]


# ---------------------------------------------------------------------------
# Scanning — pchdir
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: str) -> int:
    """Sum sizes of files (not subdirs) directly inside ``path``.

    Best-effort: any file that disappears or can't be stat'd mid-scan is
    skipped. Subdirectories are not recursed — PCH cmd_hash dirs are flat.
    """
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def scan_pchdir(pchdir: str) -> list[PchEntry]:
    """Walk a cas-pchdir and return one ``PchEntry`` per ``<cmd_hash>/`` dir.

    Skips top-level entries whose name doesn't match ``_PCH_COMMAND_HASH_RE``
    (legacy dirs, accidental clutter). Entries whose manifest is missing or
    unparseable are still included with ``header_realpath="<unknown>"`` so
    the report still reflects on-disk reality.
    """
    entries: list[PchEntry] = []
    if not os.path.isdir(pchdir):
        return entries

    try:
        with os.scandir(pchdir) as top_iter:
            cmd_hash_dirs = [
                (e.name, e.path)
                for e in top_iter
                if _PCH_COMMAND_HASH_RE.match(e.name) and e.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return entries

    for cmd_hash, cmd_hash_dir in cmd_hash_dirs:
        manifest = _load_pch_manifest(cmd_hash_dir)
        if manifest is None:
            header_realpath = "<unknown>"
        else:
            header_realpath = manifest.get("header_realpath", "<unknown>") or "<unknown>"
        size_bytes = _dir_size_bytes(cmd_hash_dir)
        entries.append(
            PchEntry(
                cmd_hash_dir=cmd_hash_dir,
                cmd_hash=cmd_hash,
                header_realpath=header_realpath,
                size_bytes=size_bytes,
            )
        )

    return entries


def group_pch_by_header(entries: Iterable[PchEntry]) -> dict[str, list[PchEntry]]:
    """Group PCH entries by ``header_realpath``.

    Each group with >1 entries is a duplication candidate.
    """
    groups: dict[str, list[PchEntry]] = {}
    for e in entries:
        groups.setdefault(e.header_realpath, []).append(e)
    return groups


def pch_report(pchdir: str) -> PchReport:
    """Produce a structured report about PCH duplication in ``pchdir``."""
    entries = scan_pchdir(pchdir)
    groups = group_pch_by_header(entries)

    total_entries = len(entries)
    total_bytes = sum(e.size_bytes for e in entries)
    unique_headers_count = len(groups)

    duplicated: list[PchDuplicateGroup] = []
    wasted_bytes = 0
    for header, variants in groups.items():
        if len(variants) <= 1:
            continue
        sizes = [v.size_bytes for v in variants]
        wasted_bytes += sum(sizes) - min(sizes)
        duplicated.append(
            PchDuplicateGroup(
                header_realpath=header,
                variants=list(variants),
            )
        )

    return PchReport(
        pchdir=pchdir,
        total_entries=total_entries,
        total_bytes=total_bytes,
        unique_headers_count=unique_headers_count,
        duplicated_groups=duplicated,
        wasted_bytes=wasted_bytes,
    )


def top_pch_headers_by_waste(rep: PchReport, n: int = 10) -> list[tuple[str, int, int]]:
    """Top-N headers by wasted bytes.

    Returns list of ``(header_realpath, variant_count, wasted_bytes)``,
    sorted by wasted bytes descending. Only headers with >0 wasted bytes
    appear.
    """
    items: list[tuple[str, int, int]] = []
    for grp in rep.duplicated_groups:
        sizes = [v.size_bytes for v in grp.variants]
        wasted = sum(sizes) - min(sizes)
        if wasted <= 0:
            continue
        items.append((grp.header_realpath, len(grp.variants), wasted))
    items.sort(key=lambda t: (-t[2], t[0]))
    return items[:n]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_bytes(n: int) -> str:
    """Render a byte count in B / KB / MB / GB.

    Bytes are reported as integers; everything else uses 2-decimal precision.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.2f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _render_cas_objdir_text(rep: CacheReport, top_n: int) -> str:
    lines: list[str] = []
    header = f"Object cache report for {rep.objdir}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(f"Total entries:           {rep.total_entries}")
    lines.append(f"Total size:              {_format_bytes(rep.total_bytes)}")
    lines.append(f"Unique (src, deps) tuples: {rep.unique_src_deps_count}")
    lines.append("")
    lines.append("Duplication summary")
    lines.append("-" * len("Duplication summary"))
    n_dup = len(rep.duplicated_groups)
    lines.append(f"Groups with >1 macro_state variant: {n_dup}")

    if n_dup > 0:
        variant_counts = [len(g.variants) for g in rep.duplicated_groups]
        lo, hi = min(variant_counts), max(variant_counts)
        lines.append(f"Variants per duplicated group: {lo}-{hi} (max {hi})")
    else:
        lines.append("Variants per duplicated group: n/a")

    pct = (rep.wasted_bytes / rep.total_bytes * 100) if rep.total_bytes else 0.0
    lines.append(f"Wasted bytes (duplicates):  {_format_bytes(rep.wasted_bytes)} ({pct:.1f}%)")

    top = top_basenames_by_waste(rep, n=top_n)
    if top:
        lines.append("")
        sub = f"Top {top_n} most-duplicated sources"
        lines.append(sub)
        lines.append("-" * len(sub))
        bn_width = max(len(b.basename) for b in top)
        for b in top:
            lines.append(f"{b.basename:<{bn_width}}  {b.variants} variants  {_format_bytes(b.wasted_bytes)} wasted")

    return "\n".join(lines) + "\n"


def _render_pch_text(rep: PchReport, top_n: int) -> str:
    lines: list[str] = []
    header = f"PCH cache report for {rep.pchdir}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(f"Total cmd_hash dirs:     {rep.total_entries}")
    lines.append(f"Total size:              {_format_bytes(rep.total_bytes)}")
    lines.append(f"Unique headers:          {rep.unique_headers_count}")
    lines.append("")
    lines.append("Duplication summary")
    lines.append("-" * len("Duplication summary"))
    n_dup = len(rep.duplicated_groups)
    lines.append(f"Headers with >1 cmd_hash variant: {n_dup}")

    if n_dup > 0:
        variant_counts = [len(g.variants) for g in rep.duplicated_groups]
        lo, hi = min(variant_counts), max(variant_counts)
        lines.append(f"Variants per duplicated header: {lo}-{hi} (max {hi})")
    else:
        lines.append("Variants per duplicated header: n/a")

    pct = (rep.wasted_bytes / rep.total_bytes * 100) if rep.total_bytes else 0.0
    lines.append(f"Wasted bytes (duplicates):  {_format_bytes(rep.wasted_bytes)} ({pct:.1f}%)")

    top = top_pch_headers_by_waste(rep, n=top_n)
    if top:
        lines.append("")
        sub = f"Top {top_n} most-duplicated headers"
        lines.append(sub)
        lines.append("-" * len(sub))
        hdr_width = max(len(t[0]) for t in top)
        for header_path, variants, wasted in top:
            lines.append(f"{header_path:<{hdr_width}}  {variants} variants  {_format_bytes(wasted)} wasted")

    return "\n".join(lines) + "\n"


def _cas_objdir_json_payload(rep: CacheReport, top_n: int) -> dict:
    top = top_basenames_by_waste(rep, n=top_n)
    return {
        "objdir": rep.objdir,
        "total_entries": rep.total_entries,
        "total_bytes": rep.total_bytes,
        "unique_src_deps_count": rep.unique_src_deps_count,
        "duplicated_groups_count": len(rep.duplicated_groups),
        "wasted_bytes": rep.wasted_bytes,
        "top_basenames": [
            {
                "basename": b.basename,
                "variants": b.variants,
                "wasted_bytes": b.wasted_bytes,
            }
            for b in top
        ],
    }


def _pch_json_payload(rep: PchReport, top_n: int) -> dict:
    top = top_pch_headers_by_waste(rep, n=top_n)
    return {
        "pchdir": rep.pchdir,
        "total_entries": rep.total_entries,
        "total_bytes": rep.total_bytes,
        "unique_headers_count": rep.unique_headers_count,
        "duplicated_groups_count": len(rep.duplicated_groups),
        "wasted_bytes": rep.wasted_bytes,
        "top_headers": [
            {
                "header_realpath": header_path,
                "variants": variants,
                "wasted_bytes": wasted,
            }
            for header_path, variants, wasted in top
        ],
    }


def _render_cas_objdir_only_json(rep: CacheReport, top_n: int) -> str:
    """Backward-compatible single-objdir JSON output (flat schema)."""
    return json.dumps(_cas_objdir_json_payload(rep, top_n), indent=2) + "\n"


def _render_combined_json(
    obj_rep: CacheReport | None,
    pch_rep_obj: PchReport | None,
    top_n: int,
) -> str:
    payload = {
        "objdir_report": _cas_objdir_json_payload(obj_rep, top_n) if obj_rep is not None else None,
        "pch_report": _pch_json_payload(pch_rep_obj, top_n) if pch_rep_obj is not None else None,
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct-cache-report",
        description=(
            "Report duplication in cas-objdir and/or cas-pchdir caches. "
            "Two cache entries that share the underlying source/header but "
            "differ in a hash component are bit-identical duplicates spawned "
            "by command-line ``-D`` macro pollution of the cache key."
        ),
    )
    parser.add_argument(
        "--cas-objdir",
        default=None,
        help="Path to the cas-objdir to scan. Optional.",
    )
    parser.add_argument(
        "--cas-pchdir",
        default=None,
        help="Path to the cas-pchdir to scan. Optional.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show the N most-duplicated entries per cache (default: 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    objdir = args.cas_objdir
    pchdir = args.cas_pchdir

    if objdir is None and pchdir is None:
        parser.error("at least one of --cas-objdir or --cas-pchdir is required")

    obj_rep = report(objdir) if objdir is not None else None
    pch_rep_obj = pch_report(pchdir) if pchdir is not None else None

    # JSON schema selection: if the user passed only --cas-objdir, emit the
    # flat objdir schema. As soon as --cas-pchdir is in play we switch to
    # the combined schema with both keys.
    if args.json:
        if pchdir is None and obj_rep is not None:
            sys.stdout.write(_render_cas_objdir_only_json(obj_rep, args.top))
        else:
            sys.stdout.write(_render_combined_json(obj_rep, pch_rep_obj, args.top))
    else:
        chunks: list[str] = []
        if obj_rep is not None:
            chunks.append(_render_cas_objdir_text(obj_rep, args.top))
        if pch_rep_obj is not None:
            chunks.append(_render_pch_text(pch_rep_obj, args.top))
        sys.stdout.write("\n".join(chunks))

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
