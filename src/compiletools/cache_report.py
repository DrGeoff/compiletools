"""Measure duplication in the object CAS, PCH CAS, PCM CAS, and linker-artefact CAS.

Walks a ``cas-objdir`` and groups entries by ``(file_hash, dep_hash)``.
Entries that share that pair but differ in ``macro_state_hash`` are
bit-identical duplicates spawned by command-line ``-D`` macro pollution
of the cache key.

Walks a ``cas-pchdir`` and groups ``<cmd_hash>/`` entries by their
manifest's ``header_realpath``. Two ``cmd_hash`` dirs that share a
header but differ in ``cmd_hash`` are PCH-cache duplicates from the
same kind of pollution.

Walks a ``cas-pcmdir`` and groups ``<cmd_hash>/`` entries by their
manifest's ``bucket_key`` (source realpath for named modules, verbatim
token for header units). Two ``cmd_hash`` dirs that share a bucket_key
but differ in ``cmd_hash`` are PCM-cache duplicates from compiler /
flag / environment pollution of the cache key.

Walks a ``cas-exedir`` and groups ``<basename>_<linkkey>.<ext>`` entries
by ``(source_realpath, suffix)`` (from the per-entry ``.manifest``
sidecar; falls back to basename for legacy entries). Two link-key
variants for the same source+suffix are linker-cache duplicates from
LDFLAGS / environment-variable pollution of the link key.

This module is standalone — it imports only stdlib plus a handful of
on-disk-format helpers from ``trim_cache`` (the single source of truth
for the cache layouts). No Hunter / MagicFlags / BuildContext
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

from compiletools.trim_cache import (
    _CAS_EXE_SUFFIXES,
    _OBJ_BUCKET_RE,
    _PCH_COMMAND_HASH_RE,
    _PCM_COMMAND_HASH_RE,
    _load_exe_manifest,
    _load_pch_manifest,
    _load_pcm_manifest,
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
# Data model — pcmdir
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PcmEntry:
    """One parsed entry from a PCM CAS."""

    cmd_hash_dir: str  # absolute path to <pcmdir>/<cmd_hash>
    cmd_hash: str  # 16 hex chars
    bucket_key: str  # from manifest "bucket_key", or "<unknown:<cmd_hash>>"
    stage: str  # from manifest "stage" — informational; not part of grouping
    size_bytes: int  # total bytes of files inside cmd_hash_dir


@dataclass(frozen=True)
class PcmDuplicateGroup:
    """A bucket_key that has multiple cmd_hash variants."""

    bucket_key: str
    variants: list[PcmEntry] = field(hash=False, compare=False)


@dataclass(frozen=True)
class PcmReport:
    """Structured summary of a cas-pcmdir scan."""

    pcmdir: str
    total_entries: int  # total cmd_hash dirs
    total_bytes: int
    unique_buckets_count: int  # distinct bucket_key values
    duplicated_groups: list[PcmDuplicateGroup] = field(hash=False, compare=False)
    wasted_bytes: int = 0


# ---------------------------------------------------------------------------
# Data model — exedir
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExeEntry:
    """One parsed entry from a cas-exedir."""

    path: str  # absolute path to the cas artefact
    basename: str  # filename basename (before the ``_<linkkey>`` separator)
    suffix: str  # one of ".exe", ".a", ".so"
    link_key: str  # the hash portion after the trailing underscore
    source_realpath: str  # from manifest "source_realpath", or "<unknown:<basename>:<suffix>>"
    size_bytes: int


@dataclass(frozen=True)
class ExeDuplicateGroup:
    """A ``(source_realpath, suffix)`` bucket with multiple link_key variants."""

    source_realpath: str
    suffix: str
    variants: list[ExeEntry] = field(hash=False, compare=False)


@dataclass(frozen=True)
class ExeReport:
    """Structured summary of a cas-exedir scan."""

    exedir: str
    total_entries: int
    total_bytes: int
    unique_buckets_count: int  # distinct (source_realpath, suffix) buckets
    duplicated_groups: list[ExeDuplicateGroup] = field(hash=False, compare=False)
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
    unparseable are still included with ``header_realpath="<unknown:<cmd_hash>>"``.
    Tagging with the cmd_hash keeps unrelated orphans in distinct groups
    rather than collapsing them all into a single ``<unknown>`` bucket
    (which would falsely flag them as duplicates of one lost header).
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
            header_realpath = f"<unknown:{cmd_hash}>"
        else:
            header_realpath = manifest.get("header_realpath") or f"<unknown:{cmd_hash}>"
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
# Scanning — pcmdir
# ---------------------------------------------------------------------------


def scan_pcmdir(pcmdir: str) -> list[PcmEntry]:
    """Walk a cas-pcmdir and return one ``PcmEntry`` per ``<cmd_hash>/`` dir.

    Skips top-level entries whose name doesn't match
    ``_PCM_COMMAND_HASH_RE`` (legacy dirs, accidental clutter, the
    per-makefile ``.module-mapper.txt``). Entries whose manifest is
    missing or unparseable are still included with
    ``bucket_key="<unknown:<cmd_hash>>"`` and ``stage=""`` — same
    anti-collapse trick as the PCH scanner uses, so unrelated orphans
    don't get falsely flagged as duplicates of one ghost bucket_key.
    """
    entries: list[PcmEntry] = []
    if not os.path.isdir(pcmdir):
        return entries

    try:
        with os.scandir(pcmdir) as top_iter:
            cmd_hash_dirs = [
                (e.name, e.path)
                for e in top_iter
                if _PCM_COMMAND_HASH_RE.match(e.name) and e.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return entries

    for cmd_hash, cmd_hash_dir in cmd_hash_dirs:
        manifest = _load_pcm_manifest(cmd_hash_dir)
        if manifest is None:
            bucket_key = f"<unknown:{cmd_hash}>"
            stage = ""
        else:
            bucket_key = manifest.get("bucket_key") or f"<unknown:{cmd_hash}>"
            stage_val = manifest.get("stage")
            stage = stage_val if isinstance(stage_val, str) else ""
        size_bytes = _dir_size_bytes(cmd_hash_dir)
        entries.append(
            PcmEntry(
                cmd_hash_dir=cmd_hash_dir,
                cmd_hash=cmd_hash,
                bucket_key=bucket_key,
                stage=stage,
                size_bytes=size_bytes,
            )
        )

    return entries


def group_pcm_by_bucket_key(entries: Iterable[PcmEntry]) -> dict[str, list[PcmEntry]]:
    """Group PCM entries by ``bucket_key``.

    Mirrors trim_cache's PCM bucketing: source realpath for named
    modules, ``<header>`` token for header units. Each group with >1
    entries is a duplication candidate.
    """
    groups: dict[str, list[PcmEntry]] = {}
    for e in entries:
        groups.setdefault(e.bucket_key, []).append(e)
    return groups


def pcm_report(pcmdir: str) -> PcmReport:
    """Produce a structured report about PCM duplication in ``pcmdir``."""
    entries = scan_pcmdir(pcmdir)
    groups = group_pcm_by_bucket_key(entries)

    total_entries = len(entries)
    total_bytes = sum(e.size_bytes for e in entries)
    unique_buckets_count = len(groups)

    duplicated: list[PcmDuplicateGroup] = []
    wasted_bytes = 0
    for bucket_key, variants in groups.items():
        if len(variants) <= 1:
            continue
        sizes = [v.size_bytes for v in variants]
        wasted_bytes += sum(sizes) - min(sizes)
        duplicated.append(
            PcmDuplicateGroup(
                bucket_key=bucket_key,
                variants=list(variants),
            )
        )

    return PcmReport(
        pcmdir=pcmdir,
        total_entries=total_entries,
        total_bytes=total_bytes,
        unique_buckets_count=unique_buckets_count,
        duplicated_groups=duplicated,
        wasted_bytes=wasted_bytes,
    )


def top_pcm_buckets_by_waste(rep: PcmReport, n: int = 10) -> list[tuple[str, int, int]]:
    """Top-N PCM bucket_keys by wasted bytes.

    Returns list of ``(bucket_key, variant_count, wasted_bytes)`` sorted
    by wasted bytes descending. Only bucket_keys with >0 wasted bytes
    appear.
    """
    items: list[tuple[str, int, int]] = []
    for grp in rep.duplicated_groups:
        sizes = [v.size_bytes for v in grp.variants]
        wasted = sum(sizes) - min(sizes)
        if wasted <= 0:
            continue
        items.append((grp.bucket_key, len(grp.variants), wasted))
    items.sort(key=lambda t: (-t[2], t[0]))
    return items[:n]


# ---------------------------------------------------------------------------
# Scanning — exedir
# ---------------------------------------------------------------------------


def scan_exedir(exedir: str) -> list[ExeEntry]:
    """Walk a cas-exedir and return one ``ExeEntry`` per ``<basename>_<linkkey><suffix>``.

    Layout: ``<exedir>/<linkkey[:2]>/<basename>_<linkkey>.<ext>`` with
    ``<ext>`` in ``_CAS_EXE_SUFFIXES``. The 2-hex shard dirs match the
    same shape as ``_OBJ_BUCKET_RE``; non-conforming top-level entries
    are silently skipped (mirrors trim_cache).

    Within each bucket, files that don't end in a recognised suffix
    (``.lock`` sidecars, ``.manifest`` sidecars, etc.) and files whose
    name doesn't split into ``<basename>_<linkkey>`` on the last
    underscore are silently skipped.

    Manifest-less or corrupt-manifest entries are tagged with
    ``<unknown:<basename>:<suffix>>`` so unrelated orphans stay in
    distinct buckets — exactly the same anti-collapse trick used for
    PCH orphans.
    """
    entries: list[ExeEntry] = []
    if not os.path.isdir(exedir):
        return entries

    try:
        with os.scandir(exedir) as top_iter:
            buckets = [e.path for e in top_iter if _OBJ_BUCKET_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return entries

    for bucket_path in buckets:
        try:
            with os.scandir(bucket_path) as bucket_iter:
                for leaf in bucket_iter:
                    if not leaf.is_file(follow_symlinks=False):
                        continue
                    matched_suffix = next(
                        (s for s in _CAS_EXE_SUFFIXES if leaf.name.endswith(s)),
                        None,
                    )
                    if matched_suffix is None:
                        continue
                    # ``<basename>_<linkkey><suffix>``: split on the LAST
                    # underscore so basenames containing underscores stay
                    # intact. Mirrors trim_cache.trim_exedir.
                    stem = leaf.name[: -len(matched_suffix)]
                    sep = stem.rfind("_")
                    if sep <= 0:
                        continue
                    basename = stem[:sep]
                    link_key = stem[sep + 1 :]
                    try:
                        size = leaf.stat().st_size
                    except OSError:
                        continue
                    manifest = _load_exe_manifest(leaf.path)
                    source_realpath: str = f"<unknown:{basename}:{matched_suffix}>"
                    if manifest is not None:
                        src = manifest.get("source_realpath")
                        if isinstance(src, str) and src:
                            source_realpath = src
                    entries.append(
                        ExeEntry(
                            path=leaf.path,
                            basename=basename,
                            suffix=matched_suffix,
                            link_key=link_key,
                            source_realpath=source_realpath,
                            size_bytes=size,
                        )
                    )
        except OSError:
            continue  # bucket disappeared mid-scan, best-effort

    return entries


def group_exe_by_source(entries: Iterable[ExeEntry]) -> dict[tuple[str, str], list[ExeEntry]]:
    """Group exe entries by ``(source_realpath, suffix)``.

    Each group with >1 entries is a duplication candidate. The suffix
    is part of the key so ``libfoo.a`` and ``libfoo.so`` (which legitimately
    coexist for the same source) are not flagged as duplicates of each
    other.
    """
    groups: dict[tuple[str, str], list[ExeEntry]] = {}
    for e in entries:
        groups.setdefault((e.source_realpath, e.suffix), []).append(e)
    return groups


def exe_report(exedir: str) -> ExeReport:
    """Produce a structured report about linker-artefact duplication in ``exedir``."""
    entries = scan_exedir(exedir)
    groups = group_exe_by_source(entries)

    total_entries = len(entries)
    total_bytes = sum(e.size_bytes for e in entries)
    unique_buckets_count = len(groups)

    duplicated: list[ExeDuplicateGroup] = []
    wasted_bytes = 0
    for (source, suffix), variants in groups.items():
        if len(variants) <= 1:
            continue
        sizes = [v.size_bytes for v in variants]
        wasted_bytes += sum(sizes) - min(sizes)
        duplicated.append(
            ExeDuplicateGroup(
                source_realpath=source,
                suffix=suffix,
                variants=list(variants),
            )
        )

    return ExeReport(
        exedir=exedir,
        total_entries=total_entries,
        total_bytes=total_bytes,
        unique_buckets_count=unique_buckets_count,
        duplicated_groups=duplicated,
        wasted_bytes=wasted_bytes,
    )


def top_exe_sources_by_waste(rep: ExeReport, n: int = 10) -> list[tuple[str, str, int, int]]:
    """Top-N (source_realpath, suffix) buckets by wasted bytes.

    Returns list of ``(source_realpath, suffix, variant_count, wasted_bytes)``
    sorted by wasted bytes descending. Only buckets with >0 wasted bytes
    appear.
    """
    items: list[tuple[str, str, int, int]] = []
    for grp in rep.duplicated_groups:
        sizes = [v.size_bytes for v in grp.variants]
        wasted = sum(sizes) - min(sizes)
        if wasted <= 0:
            continue
        items.append((grp.source_realpath, grp.suffix, len(grp.variants), wasted))
    items.sort(key=lambda t: (-t[3], t[0], t[1]))
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


def _render_pcm_text(rep: PcmReport, top_n: int) -> str:
    lines: list[str] = []
    header = f"PCM cache report for {rep.pcmdir}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(f"Total cmd_hash dirs:     {rep.total_entries}")
    lines.append(f"Total size:              {_format_bytes(rep.total_bytes)}")
    lines.append(f"Unique bucket_keys:      {rep.unique_buckets_count}")
    lines.append("")
    lines.append("Duplication summary")
    lines.append("-" * len("Duplication summary"))
    n_dup = len(rep.duplicated_groups)
    lines.append(f"bucket_keys with >1 cmd_hash variant: {n_dup}")

    if n_dup > 0:
        variant_counts = [len(g.variants) for g in rep.duplicated_groups]
        lo, hi = min(variant_counts), max(variant_counts)
        lines.append(f"Variants per duplicated bucket_key: {lo}-{hi} (max {hi})")
    else:
        lines.append("Variants per duplicated bucket_key: n/a")

    pct = (rep.wasted_bytes / rep.total_bytes * 100) if rep.total_bytes else 0.0
    lines.append(f"Wasted bytes (duplicates):  {_format_bytes(rep.wasted_bytes)} ({pct:.1f}%)")

    top = top_pcm_buckets_by_waste(rep, n=top_n)
    if top:
        lines.append("")
        sub = f"Top {top_n} most-duplicated module bucket_keys"
        lines.append(sub)
        lines.append("-" * len(sub))
        bk_width = max(len(t[0]) for t in top)
        for bucket_key, variants, wasted in top:
            lines.append(f"{bucket_key:<{bk_width}}  {variants} variants  {_format_bytes(wasted)} wasted")

    return "\n".join(lines) + "\n"


def _render_exe_text(rep: ExeReport, top_n: int) -> str:
    lines: list[str] = []
    header = f"Linker-artefact cache report for {rep.exedir}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(f"Total entries:           {rep.total_entries}")
    lines.append(f"Total size:              {_format_bytes(rep.total_bytes)}")
    lines.append(f"Unique (source, suffix) buckets: {rep.unique_buckets_count}")
    lines.append("")
    lines.append("Duplication summary")
    lines.append("-" * len("Duplication summary"))
    n_dup = len(rep.duplicated_groups)
    lines.append(f"Buckets with >1 link_key variant: {n_dup}")

    if n_dup > 0:
        variant_counts = [len(g.variants) for g in rep.duplicated_groups]
        lo, hi = min(variant_counts), max(variant_counts)
        lines.append(f"Variants per duplicated bucket: {lo}-{hi} (max {hi})")
    else:
        lines.append("Variants per duplicated bucket: n/a")

    pct = (rep.wasted_bytes / rep.total_bytes * 100) if rep.total_bytes else 0.0
    lines.append(f"Wasted bytes (duplicates):  {_format_bytes(rep.wasted_bytes)} ({pct:.1f}%)")

    top = top_exe_sources_by_waste(rep, n=top_n)
    if top:
        lines.append("")
        sub = f"Top {top_n} most-duplicated linker artefacts"
        lines.append(sub)
        lines.append("-" * len(sub))
        label_width = max(len(f"{s}{suffix}") for s, suffix, _v, _w in top)
        for source, suffix, variants, wasted in top:
            label = f"{source}{suffix}"
            lines.append(f"{label:<{label_width}}  {variants} variants  {_format_bytes(wasted)} wasted")

    return "\n".join(lines) + "\n"


def _cas_objdir_json_payload(rep: CacheReport, top_n: int) -> dict:
    """JSON payload for a cas-objdir scan.

    Top-level keys that mirror CLI flag names use kebab-case so users
    can grep the report with the same identifier as the CLI (e.g.
    ``jq '.["cas-objdir"]'`` matches ``--cas-objdir``). Internal field
    names (``total-entries`` etc.) follow the same convention for
    consistency within the document.
    """
    top = top_basenames_by_waste(rep, n=top_n)
    return {
        "cas-objdir": rep.objdir,
        "total-entries": rep.total_entries,
        "total-bytes": rep.total_bytes,
        "unique-src-deps-count": rep.unique_src_deps_count,
        "duplicated-groups-count": len(rep.duplicated_groups),
        "wasted-bytes": rep.wasted_bytes,
        "top-basenames": [
            {
                "basename": b.basename,
                "variants": b.variants,
                "wasted-bytes": b.wasted_bytes,
            }
            for b in top
        ],
    }


def _pch_json_payload(rep: PchReport, top_n: int) -> dict:
    """JSON payload for a cas-pchdir scan. Kebab-case keys; see
    :func:`_cas_objdir_json_payload` for the rationale.
    """
    top = top_pch_headers_by_waste(rep, n=top_n)
    return {
        "cas-pchdir": rep.pchdir,
        "total-entries": rep.total_entries,
        "total-bytes": rep.total_bytes,
        "unique-headers-count": rep.unique_headers_count,
        "duplicated-groups-count": len(rep.duplicated_groups),
        "wasted-bytes": rep.wasted_bytes,
        "top-headers": [
            {
                "header-realpath": header_path,
                "variants": variants,
                "wasted-bytes": wasted,
            }
            for header_path, variants, wasted in top
        ],
    }


def _pcm_json_payload(rep: PcmReport, top_n: int) -> dict:
    """JSON payload for a cas-pcmdir scan. Kebab-case keys; see
    :func:`_cas_objdir_json_payload` for the rationale.
    """
    top = top_pcm_buckets_by_waste(rep, n=top_n)
    return {
        "cas-pcmdir": rep.pcmdir,
        "total-entries": rep.total_entries,
        "total-bytes": rep.total_bytes,
        "unique-buckets-count": rep.unique_buckets_count,
        "duplicated-groups-count": len(rep.duplicated_groups),
        "wasted-bytes": rep.wasted_bytes,
        "top-buckets": [
            {
                "bucket-key": bucket_key,
                "variants": variants,
                "wasted-bytes": wasted,
            }
            for bucket_key, variants, wasted in top
        ],
    }


def _exe_json_payload(rep: ExeReport, top_n: int) -> dict:
    """JSON payload for a cas-exedir scan. Kebab-case keys; see
    :func:`_cas_objdir_json_payload` for the rationale.
    """
    top = top_exe_sources_by_waste(rep, n=top_n)
    return {
        "cas-exedir": rep.exedir,
        "total-entries": rep.total_entries,
        "total-bytes": rep.total_bytes,
        "unique-buckets-count": rep.unique_buckets_count,
        "duplicated-groups-count": len(rep.duplicated_groups),
        "wasted-bytes": rep.wasted_bytes,
        "top-sources": [
            {
                "source-realpath": source,
                "suffix": suffix,
                "variants": variants,
                "wasted-bytes": wasted,
            }
            for source, suffix, variants, wasted in top
        ],
    }


def _render_cas_objdir_only_json(rep: CacheReport, top_n: int) -> str:
    """Single-objdir flat JSON output (vs. the wrapped combined schema)."""
    return json.dumps(_cas_objdir_json_payload(rep, top_n), indent=2) + "\n"


def _render_combined_json(
    obj_rep: CacheReport | None,
    pch_rep_obj: PchReport | None,
    pcm_rep_obj: PcmReport | None,
    exe_rep_obj: ExeReport | None,
    top_n: int,
) -> str:
    payload = {
        "cas-objdir-report": _cas_objdir_json_payload(obj_rep, top_n) if obj_rep is not None else None,
        "cas-pchdir-report": _pch_json_payload(pch_rep_obj, top_n) if pch_rep_obj is not None else None,
        "cas-pcmdir-report": _pcm_json_payload(pcm_rep_obj, top_n) if pcm_rep_obj is not None else None,
        "cas-exedir-report": _exe_json_payload(exe_rep_obj, top_n) if exe_rep_obj is not None else None,
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct-cache-report",
        description=(
            "Report duplication in cas-objdir, cas-pchdir, cas-pcmdir, and/or "
            "cas-exedir caches. Two cache entries that share the underlying "
            "source/header/module but differ in a hash component are bit-identical "
            "duplicates spawned by command-line ``-D`` macro pollution of the "
            "cache key (objdir, pchdir, pcmdir) or LDFLAGS/environment-variable "
            "pollution of the link key (exedir)."
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
        "--cas-pcmdir",
        default=None,
        help="Path to the cas-pcmdir (C++20 modules cache) to scan. Optional.",
    )
    parser.add_argument(
        "--cas-exedir",
        default=None,
        help="Path to the cas-exedir (linker-artefact cache) to scan. Optional.",
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
    pcmdir = args.cas_pcmdir
    exedir = args.cas_exedir

    if objdir is None and pchdir is None and pcmdir is None and exedir is None:
        parser.error(
            "at least one of --cas-objdir, --cas-pchdir, --cas-pcmdir, or --cas-exedir is required"
        )

    obj_rep = report(objdir) if objdir is not None else None
    pch_rep_obj = pch_report(pchdir) if pchdir is not None else None
    pcm_rep_obj = pcm_report(pcmdir) if pcmdir is not None else None
    exe_rep_obj = exe_report(exedir) if exedir is not None else None

    # JSON schema selection: preserve the legacy flat objdir-only schema
    # when ONLY --cas-objdir was supplied (back-compat for existing
    # tooling that consumes that shape). Any combination involving any
    # other cache uses the combined schema.
    if args.json:
        if pchdir is None and pcmdir is None and exedir is None and obj_rep is not None:
            sys.stdout.write(_render_cas_objdir_only_json(obj_rep, args.top))
        else:
            sys.stdout.write(
                _render_combined_json(obj_rep, pch_rep_obj, pcm_rep_obj, exe_rep_obj, args.top)
            )
    else:
        chunks: list[str] = []
        if obj_rep is not None:
            chunks.append(_render_cas_objdir_text(obj_rep, args.top))
        if pch_rep_obj is not None:
            chunks.append(_render_pch_text(pch_rep_obj, args.top))
        if pcm_rep_obj is not None:
            chunks.append(_render_pcm_text(pcm_rep_obj, args.top))
        if exe_rep_obj is not None:
            chunks.append(_render_exe_text(exe_rep_obj, args.top))
        sys.stdout.write("\n".join(chunks))

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
