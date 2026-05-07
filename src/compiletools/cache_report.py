"""Measure duplication in a shared object cache.

Walks a ``shared-objdir`` and groups entries by ``(file_hash, dep_hash)``.
Entries that share that pair but differ in ``macro_state_hash`` are
bit-identical duplicates spawned by command-line ``-D`` macro pollution
of the cache key. This tool reports how many such duplicates exist so
operators can quantify the impact of the cache-key scoping fix.

This module is standalone — it imports only stdlib plus
``parse_object_filename`` from ``trim_cache`` (the single source of truth
for the object-filename format). No Hunter / MagicFlags / BuildContext
dependencies, so it stays cheap to import and easy to test.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

from compiletools.trim_cache import _OBJ_BUCKET_RE, parse_object_filename

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectFileEntry:
    """One parsed entry from a shared object directory."""

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
    """Structured summary of a shared-objdir scan."""

    objdir: str
    total_entries: int
    total_bytes: int
    unique_src_deps_count: int
    duplicated_groups: list[DuplicateGroup] = field(hash=False, compare=False)
    wasted_bytes: int = 0


# ---------------------------------------------------------------------------
# Scanning
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


def _render_text(rep: CacheReport, top_n: int) -> str:
    lines: list[str] = []
    lines.append(f"Cache report for {rep.objdir}")
    lines.append("=" * (len(lines[0])))
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
        header = f"Top {top_n} most-duplicated sources"
        lines.append(header)
        lines.append("-" * len(header))
        bn_width = max(len(b.basename) for b in top)
        for b in top:
            lines.append(f"{b.basename:<{bn_width}}  {b.variants} variants  {_format_bytes(b.wasted_bytes)} wasted")

    return "\n".join(lines) + "\n"


def _render_json(rep: CacheReport, top_n: int) -> str:
    top = top_basenames_by_waste(rep, n=top_n)
    payload = {
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
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct-cache-report",
        description=(
            "Walk a shared-objdir and report how many cached objects are "
            "bit-identical duplicates differing only in macro_state_hash."
        ),
    )
    parser.add_argument("objdir", help="Path to the shared-objdir to scan.")
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show the N most-duplicated source basenames (default: 10).",
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

    rep = report(args.objdir)
    if args.json:
        sys.stdout.write(_render_json(rep, args.top))
    else:
        sys.stdout.write(_render_text(rep, args.top))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
