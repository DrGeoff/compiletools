"""Cache trimming utility for the object CAS, PCH CAS, PCM CAS, and the
linker-artefact CAS (cas-exedir).

Scans cas-objdir, cas-pchdir, cas-pcmdir, and cas-exedir for stale entries
and removes them, keeping entries that match the current git state and
preserving a configurable number of recent non-current entries per source
file or per module/header/linker-artefact bucket.

Three size-control behaviours layer on top of that per-bucket policy
(``--keep-count`` + ``--max-age``):

* ``--max-size`` (``enforce_budget``): a per-pool byte budget. After the
  per-bucket policy runs, additional NON-protected entries are evicted
  oldest-first until the pool is under the budget. Peer-safe -- current
  objects and hard-linked (published) artefacts are never evicted, so the
  budget may be left unmet (reported as ``budget_unmet_bytes``) rather than
  violating safety. It MAY evict non-current entries below ``--keep-count``
  (they are rebuildable -- the point of an explicit budget).
* Orphan-temp reclamation (``reclaim_orphan_temps``): removes producer temp
  files the artefact scanners ignore (``*.compiletools.tmp[.<pid>]`` and
  ``*.publish.tmp``) once older than ``_ORPHAN_TEMP_MIN_AGE_SECONDS`` (so a
  removal cannot race an in-flight build), lock-safe.
* Retry-once (``retry_failed``): a removal that cannot take its lock on the
  first pass is queued and retried once just before the summary; only a
  SECOND failure counts as ``failed`` (left in place -- an intentional leak a
  peer is presumably still using).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import compiletools.filesystem_utils

# Object filename format: {basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o
# Anchored from the END (the three hash fields have fixed widths) so the
# basename can contain anything — including embedded substrings that look
# like our hash fields. Per-trailing-component matching avoids the regex
# backtracking quirks of a greedy ``(.+)`` first group.
_OBJ_FILENAME_RE = re.compile(
    r"^(?P<basename>.+)_(?P<file>[0-9a-f]{12})_(?P<dep>[0-9a-f]{14})_(?P<macro>[0-9a-f]{16})\.o$"
)

# Object bucket directories are exactly 2 lowercase hex chars (the leading
# 2 chars of the per-source ``file_hash``). Top-level entries that don't
# match — ``TraceStore/`` dirs, anything else — are invisible to the
# scanner.  Diagnostics artifacts (timing JSON and ``slurm-ct-*.out`` logs)
# default to ``<bindir>/diagnostics/<invocation>/`` and are not in objdir;
# if a user overrides ``--diagnostics-dir`` to point back at ``--cas-objdir``
# they remain invisible here too, by design.
_OBJ_BUCKET_RE = re.compile(r"^[0-9a-f]{2}$")

# PCH command hash directories are exactly 16 lowercase hex chars.
_PCH_COMMAND_HASH_RE = re.compile(r"^[0-9a-f]{16}$")

# PCM command_hash directories use the same shape as PCH: 16 hex chars
# of sha256 truncation, single hash per cache entry. The PCM cache key
# folds source content + transitive header content + compiler + flags
# into one ``command_hash``; safety against accidental collision is
# provided by the compiler's BMI verification at consume time
# (see _pcm_command_hash docstring), so the additional entropy of an
# object-cache-style 3-axis path layout isn't needed here.
_PCM_COMMAND_HASH_RE = re.compile(r"^[0-9a-f]{16}$")

# Suffixes recognised inside cas-exedir buckets. Keep in lockstep with
# ``namer.cas_*_pathname`` and ``build_backend._build_publish_rule``.
# Anything not on this list (e.g. ``.lock`` sidecars, ``.lock.excl``)
# is silently skipped, which is what we want — peer-build lock files
# must never be enumerated as trim or report candidates.
_CAS_EXE_SUFFIXES: tuple[str, ...] = (".exe", ".a", ".so")

# Matchers that identify orphaned producer temp files inside CAS bucket / cmd_hash
# dirs. Two sources:
#   • ``build_backend`` PCH/PCM precompile temp: ``<artefact>.compiletools.tmp.<pid>``
#     (build_backend.py emits ``f"{artefact_path}.compiletools.tmp.{unique}"`` where
#     ``unique`` is a PID/random integer suffix; if the build crashes or is killed
#     between the compiler write and the ``mv -f``, the temp is orphaned in the
#     cas-pchdir / cas-pcmdir cmd_hash dir).  The pid suffix is optional in the
#     regex so bare ``.compiletools.tmp`` (no trailing digits) is also matched.
#   • ``cas_publish`` atomic-replace temp: ``<base><rand>.publish.tmp``
#     (``tempfile.mkstemp(suffix=".publish.tmp")`` — names end exactly in
#     ``.publish.tmp``, so a plain ``endswith`` is sufficient and correct).
# NOT included: locking.py compile/link temps ``<target>.{pid}.{rand}.tmp``, which
# end with a plain ``.tmp`` suffix shared with many unrelated temporaries;  the
# one-day age floor already makes accidental false-positives safe, but those temps
# are cleaned up by ``_temp_under_lock`` on normal or crashed exit, so orphaning
# them requires a SIGKILL mid-write — rare enough that the broad ``.tmp`` suffix
# is not worth the risk of accidentally matching unrelated files.
_COMPILETOOLS_TMP_RE = re.compile(r"\.compiletools\.tmp(\.\d+)?$")
_PUBLISH_TMP_SUFFIX: str = ".publish.tmp"

# A temp file untouched for this many seconds cannot be an in-flight write — no
# build invocation legitimately holds a temp open for more than a day. Removing a
# file older than this age cannot race a live producer.
_ORPHAN_TEMP_MIN_AGE_SECONDS: int = 86400  # 1 day


# Size-suffix multipliers for --max-size (1024-based / binary). Case-insensitive
# and an optional trailing 'B' is tolerated (e.g. "500MB" == "500M").
_SIZE_SUFFIX_MULTIPLIERS: dict[str, int] = {
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}


def _parse_size(s: str) -> int:
    """Parse a human-readable size string into a byte count.

    Accepts a plain integer (bytes) or an integer/decimal magnitude with a
    1024-based binary suffix ``K``/``M``/``G``/``T`` (case-insensitive), with an
    optional trailing ``B`` (so ``"10G"``, ``"500MB"``, ``"2g"``, ``"1024"`` all
    work). Whitespace around the value is ignored.

    Args:
        s: The size string (e.g. ``"10G"``, ``"512M"``, ``"1024"``).

    Returns:
        The size in bytes as an ``int``.

    Raises:
        ValueError: when *s* is not a recognised size (junk text, an empty
            value, or a negative magnitude).
    """
    if s is None:
        raise ValueError("size value is required")
    text = s.strip()
    if not text:
        raise ValueError("size value is empty")

    # Strip a single optional trailing 'B' (bytes marker) unless the whole
    # token is just "B" (which is junk: no magnitude).
    body = text
    if len(body) > 1 and body[-1] in ("B", "b"):
        body = body[:-1]

    multiplier = 1
    if body and body[-1].upper() in _SIZE_SUFFIX_MULTIPLIERS:
        multiplier = _SIZE_SUFFIX_MULTIPLIERS[body[-1].upper()]
        body = body[:-1]

    body = body.strip()
    if not body:
        raise ValueError(f"invalid size {s!r}: no numeric magnitude")
    # Fast-path for plain integers: int() is exact for arbitrarily large values,
    # whereas float() loses precision above 2^53 (e.g. 2^53+1 rounds to 2^53).
    if body.isdigit():
        return int(body) * multiplier
    try:
        magnitude = float(body)
    except ValueError:
        raise ValueError(
            f"invalid size {s!r}: expected an integer optionally followed by K/M/G/T (e.g. '10G', '512M', '1024')"
        ) from None
    if magnitude < 0:
        raise ValueError(f"invalid size {s!r}: must not be negative")
    return int(magnitude * multiplier)


def _load_exe_manifest(cas_path: str) -> dict | None:
    """Read a cas-exedir entry's sidecar manifest at ``<cas_path>.manifest``.

    Returns ``None`` for legacy entries (no manifest), unreadable, or
    corrupt files. Callers must treat ``None`` as "fall back to
    basename bucketing" so reporting/trimming keep working during the
    manifest-rollout window.
    """
    try:
        with open(cas_path + ".manifest") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_cmd_hash_manifest(cmd_hash_dir: str) -> dict | None:
    """Read a cmd_hash dir's sidecar manifest (PCH or PCM).

    Returns ``None`` for legacy (manifest-less) entries or unreadable /
    corrupt files. Callers must treat ``None`` as "fall back to global
    ranking" so the trim path keeps working during the manifest-rollout
    window.
    """
    path = os.path.join(cmd_hash_dir, "manifest.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# Back-compat aliases for the (now-deduplicated) PCH/PCM manifest loaders.
# Re-exported by cache_report.py.
_load_pch_manifest = _load_cmd_hash_manifest
_load_pcm_manifest = _load_cmd_hash_manifest


def _entry_mtime_size(entry):
    """Return ``(st_mtime, st_size)`` for a ``DirEntry``, or ``None`` if it
    vanished / is unreadable mid-scan.

    Centralizes the per-entry ``stat()`` — the single most expensive operation
    in a cache scan on a high-latency filesystem (one metadata round-trip per
    call). Routing every scan through this one seam keeps the ``OSError``
    handling uniform and gives the parallel fan-out a clean unit to spy on.
    """
    try:
        st = entry.stat()
    except OSError:
        return None
    return st.st_mtime, st.st_size


def _map_scan(units, scan_one, workers):
    """Apply ``scan_one`` to each unit, fanning out across ``workers`` threads
    when ``workers > 1``; otherwise run serially.

    Results are returned in input order. ``scan_one`` runs in a worker thread
    and MUST return a self-contained partial result — all merging into shared
    structures happens in the calling thread, so no cross-thread locking is
    needed. Per-entry ``stat()`` releases the GIL, so the threads overlap the
    metadata round-trip latency that dominates the scan on GPFS/NFS/Lustre.
    On local-disk (and unknown) filesystems the caller passes ``workers == 1``
    and this is a plain serial loop — byte-for-byte the historical behavior.
    """
    units = list(units)
    if workers <= 1 or len(units) <= 1:
        return [scan_one(u) for u in units]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(scan_one, units))


def _scan_one_object_bucket(bucket_path, current_hashes):
    """Scan one 2-hex object bucket (runs in a worker thread).

    Returns ``(current_counts, noncurrent, scanned)`` where:
      * ``current_counts`` maps basename → count of current-hash objects.
        Current objects are kept regardless of mtime/size, so they are NOT
        statted — this elision removes the bulk of the metadata round-trips
        in a healthy cache (most entries are current).
      * ``noncurrent`` maps basename → list of ``(path, mtime, size)``; these
        are statted because they must be ranked by mtime and sized for the
        bytes-freed tally.
      * ``scanned`` counts every parseable ``.o`` (current + non-current),
        matching the historical ``total_scanned`` semantics.

    ``.lockdir`` siblings are skipped (lock subsystem's, not ours). A bucket
    that vanishes / is unreadable mid-scan contributes nothing — best-effort.
    """
    current_counts = {}
    noncurrent = {}
    scanned = 0
    try:
        with os.scandir(bucket_path) as bucket_entries:
            for entry in bucket_entries:
                if entry.name.endswith(".lockdir"):
                    continue
                if not entry.name.endswith(".o"):
                    continue
                parsed = parse_object_filename(entry.name)
                if parsed is None:
                    continue
                scanned += 1
                basename, file_hash, _dep_hash, _macro_hash = parsed
                if file_hash in current_hashes:
                    current_counts[basename] = current_counts.get(basename, 0) + 1
                    continue
                ms = _entry_mtime_size(entry)
                if ms is None:
                    continue
                noncurrent.setdefault(basename, []).append((entry.path, ms[0], ms[1]))
    except OSError:
        pass  # bucket vanished or unreadable; skip and move on
    return current_counts, noncurrent, scanned


def _scan_one_cmd_hash_dir(entry, leaf_suffixes):
    """Scan one PCH/PCM ``<cmd_hash>/`` directory (runs in a worker thread).

    ``leaf_suffixes`` is the tuple of artefact extensions to total
    (``(".gch",)`` for PCH, ``(".pcm", ".gcm")`` for PCM). Returns
    ``(name, path, mtime, total_size, leaves)`` or ``None`` when the dir has
    no matching leaves or its stat fails (skipped, matching legacy behavior).
    """
    leaves = []
    total_size = 0
    try:
        with os.scandir(entry.path) as leaf_entries:
            for leaf in leaf_entries:
                if leaf.name.endswith(leaf_suffixes) and leaf.is_file():
                    leaves.append(leaf.name)
                    ms = _entry_mtime_size(leaf)
                    if ms is not None:
                        total_size += ms[1]
    except OSError:
        return None
    if not leaves:
        return None
    ms = _entry_mtime_size(entry)
    if ms is None:
        return None
    return entry.name, entry.path, ms[0], total_size, leaves


def _scan_one_exe_bucket(bucket_entry):
    """Scan one cas-exedir bucket (runs in a worker thread).

    Returns a list of ``(path, bucket_key, mtime, size, nlink)``. Every entry
    needs mtime + size + nlink, so (unlike the object scan) there is no stat
    to elide — a single ``stat()`` per artefact is taken. ``bucket_key`` is
    ``(source_realpath, suffix)`` from the sidecar manifest when present, else
    ``(basename, suffix)`` (legacy). Non-artefact files (lock sidecars, etc.)
    and unreadable entries are skipped.
    """
    recs = []
    try:
        inner = list(os.scandir(bucket_entry.path))
    except OSError:
        return recs
    for leaf in inner:
        if not leaf.is_file():
            continue
        matched_suffix = next((s for s in _CAS_EXE_SUFFIXES if leaf.name.endswith(s)), None)
        if matched_suffix is None:
            continue
        # ``<basename>_<key><suffix>``: split on the LAST underscore so
        # basenames containing underscores stay intact.
        stem = leaf.name[: -len(matched_suffix)]
        sep = stem.rfind("_")
        if sep <= 0:
            continue
        basename = stem[:sep]
        try:
            st = leaf.stat()
        except OSError:
            continue
        bucket_id = basename
        manifest = _load_exe_manifest(leaf.path)
        if manifest is not None:
            src = manifest.get("source_realpath")
            if isinstance(src, str) and src:
                bucket_id = src
        recs.append((leaf.path, (bucket_id, matched_suffix), st.st_mtime, st.st_size, st.st_nlink))
    return recs


def parse_object_filename(filename):
    """Parse a content-addressable object filename into its components.

    Args:
        filename: Object filename (not path), e.g.
            ``my_module_aabbccddeeff_11223344556677_0011223344556677.o``

    Returns:
        Tuple of (basename, file_hash_12, dep_hash_14, macro_state_hash_16)
        or None if the filename does not match the expected format.
    """
    m = _OBJ_FILENAME_RE.match(filename)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return None


def build_current_hash_set(context):
    """Build the set of 12-char file hash prefixes for all git-tracked files.

    The 12-char (48-bit) prefix is deliberately loose: the failure mode
    of a collision is over-retention (treating a non-current entry as
    current and keeping it), never under-deletion. With ~80k tracked
    files the birthday probability of a collision is non-trivial but
    benign.

    Args:
        context: BuildContext with loaded file hashes.

    Returns:
        set of 12-character hex strings.
    """
    from compiletools.global_hash_registry import get_tracked_files

    tracked = get_tracked_files(context)
    return {sha[:12] for sha in tracked.values()}


class CacheTrimmer:
    """Trims stale entries from cas-objdir and cas-pchdir caches."""

    def __init__(self, args):
        self.dry_run = getattr(args, "dry_run", False)
        self.verbose = getattr(args, "verbose", 1)
        self.keep_count = getattr(args, "keep_count", 1)
        max_age_days = getattr(args, "max_age", None)
        self.max_age_seconds = max_age_days * 86400 if max_age_days is not None else None
        # --max-size: an optional per-pool TOTAL size budget in bytes, already
        # parsed (via _parse_size) by main() into args.max_size_bytes. None means
        # "no budget" (the historical behaviour — keep_count/max_age only).
        self.max_size_bytes = getattr(args, "max_size_bytes", None)
        # Scan parallelism is sourced from --parallel / -j (jobs.py), which
        # already honours CPU affinity, cgroups, and slurm allocations. A
        # caller that never plumbed it (or passed 0/None) stays serial.
        self.parallel = getattr(args, "parallel", 1) or 1
        # When --json is set, human/progress text goes to stderr so stdout
        # stays pure JSON. Default: stdout (non-JSON human mode).
        self._human = sys.stderr if getattr(args, "json", False) else sys.stdout
        # Retry list: entries that failed their first removal attempt are queued
        # here and retried once in retry_failed() after all caches have been
        # trimmed. Each entry is a dict with keys: "path" (str), "is_dir"
        # (bool), "size" (int), "stats" (the per-cache stats dict, by reference
        # so retry mutations land in the same dict main() will print),
        # "removed_key" (str — "removed" for objdir/exedir, "dirs_removed" for
        # pchdir/pcmdir), "unlink_kwargs" (dict, e.g.
        # {"skip_if_nlink_above": 1} for exedir), and "cleanup_sidecars" (bool,
        # True for exedir entries — instructs retry_failed() to also remove the
        # .manifest/.result sidecars on success; omitted or False for
        # objdir/pchdir/pcmdir entries that have no sidecars to clean up).
        self._retry: list[dict] = []

    def _workers_for(self, path):
        """Worker-thread count for scanning ``path``.

        ``--parallel`` on a filesystem where parallel ``stat()`` overlaps
        metadata latency (GPFS/NFS/Lustre/CIFS/...), else ``1`` (serial) — on
        local-disk and unknown filesystems threads only add overhead. The
        filesystem detection + policy live in ``filesystem_utils`` (shared
        with the locking-strategy selector), so trim and lock agree on FS
        classification.
        """
        if self.parallel <= 1:
            return 1
        fstype = compiletools.filesystem_utils.get_filesystem_type(path)
        if compiletools.filesystem_utils.should_parallelize_scan(fstype):
            return self.parallel
        return 1

    # ------------------------------------------------------------------
    # Object directory trimming
    # ------------------------------------------------------------------

    def trim_objdir(self, objdir, current_hashes):
        """Trim stale object files from an object CAS.

        Note on ``max_age``: "aged" means "old since written" (mtime), NOT
        "old since last accessed" (atime). A heavily-used cache entry from
        months ago will still be evicted because we cannot rely on atime
        (most production filesystems mount with ``noatime``).

        Args:
            objdir: Path to the object CAS.
            current_hashes: Set of 12-char hex strings representing current
                file hashes (from build_current_hash_set).

        Returns:
            dict with statistics: total_scanned, basenames_found,
            current_kept, noncurrent_kept, removed, failed, bytes_freed.
        """
        stats = {
            "total_scanned": 0,
            "basenames_found": 0,
            "current_kept": 0,
            "noncurrent_kept": 0,
            "removed": 0,
            "failed": 0,
            "bytes_freed": 0,
            "orphan_temps_removed": 0,
            "orphan_temp_bytes_freed": 0,
            "budget_removed": 0,
            "budget_bytes_freed": 0,
            "budget_unmet_bytes": 0,
        }

        if not os.path.isdir(objdir):
            return stats

        try:
            groups = self._scan_object_files(objdir, current_hashes, stats)
        except OSError as exc:
            print(f"Error scanning {objdir}: {exc}", file=sys.stderr)
            return stats

        now = time.time()
        for _basename, group in groups.items():
            self._process_basename_group(group["current"], group["noncurrent"], now, stats)

        return stats

    def _scan_object_files(self, objdir, current_hashes, stats):
        """Scan ``objdir`` for parseable ``.o`` entries, grouped by basename.

        Walks two levels: ``<objdir>/<bucket>/*.o`` where ``<bucket>`` is
        a 2-hex shard derived from the per-source ``file_hash[:2]``.
        Top-level entries that aren't 2-hex bucket dirs (Slurm ``.out``
        logs, ``TraceStore/`` dirs, stray pre-sharding ``.o`` leftovers)
        are skipped — they are outside the sharded cache's world.

        Each bucket is scanned by ``_scan_one_object_bucket`` — fanned out
        across ``self._workers_for(objdir)`` threads on a high-latency
        filesystem, serial on local disk. Buckets are the natural unit of
        parallelism (≤256 of them) and ``stat()`` releases the GIL, so the
        per-file metadata round-trips overlap. Current-hash objects are not
        statted at all (they are kept regardless of mtime); only non-current
        objects pay a ``stat()``.

        Within each bucket, ``.lockdir`` entries are skipped (they sit
        next to their ``.o`` and are managed by the lock subsystem, not
        the trimmer).

        Mutates ``stats`` in place: accumulates ``total_scanned`` across
        buckets and sets ``basenames_found`` to the final number of distinct
        basenames discovered.

        Returns a dict mapping basename to ``{"current": int, "noncurrent":
        [(path, mtime, size), ...]}``. May raise ``OSError`` if the initial
        top-level ``os.scandir`` call fails. Per-bucket ``OSError`` (e.g. a
        bucket dir vanishes mid-scan) is swallowed inside the worker and that
        bucket's contribution is just skipped — best-effort scan.
        """
        with os.scandir(objdir) as entries:
            bucket_paths = [
                entry.path
                for entry in entries
                if _OBJ_BUCKET_RE.match(entry.name) and entry.is_dir(follow_symlinks=False)
            ]

        workers = self._workers_for(objdir)
        results = _map_scan(bucket_paths, lambda b: _scan_one_object_bucket(b, current_hashes), workers)

        # Merge partial results in the calling thread (no cross-thread locking).
        groups = {}  # basename -> {"current": int, "noncurrent": [(path, mtime, size)]}
        for current_counts, noncurrent, scanned in results:
            stats["total_scanned"] += scanned
            for basename, count in current_counts.items():
                groups.setdefault(basename, {"current": 0, "noncurrent": []})["current"] += count
            for basename, items in noncurrent.items():
                groups.setdefault(basename, {"current": 0, "noncurrent": []})["noncurrent"].extend(items)

        stats["basenames_found"] = len(groups)
        return groups

    def _process_basename_group(self, current_count, noncurrent, now, stats):
        """Apply the keep/remove policy to one basename's object files.

        ``current_count`` is the number of current-hash objects (kept
        unconditionally; not statted during the scan). ``noncurrent`` is a
        list of ``(path, mtime, size)`` for the non-current objects. Mutates
        ``stats`` in place additively (``current_kept``, ``noncurrent_kept``,
        ``removed``, ``failed``, ``bytes_freed``) and performs the per-file
        ``print`` / ``_safe_locked_unlink`` side effects.
        """
        stats["current_kept"] += current_count

        # Sort non-current by mtime descending (newest first)
        noncurrent = sorted(noncurrent, key=lambda x: x[1], reverse=True)

        to_keep = noncurrent[: self.keep_count]
        candidates = noncurrent[self.keep_count :]

        # Safety: always keep at least 1 file per basename total.
        # Only fires when keep_count=0 AND no current entry exists; if
        # the basename has zero non-current entries it stays absent (no
        # file to retain — nothing is silently lost, there is nothing
        # to keep).
        if current_count == 0 and not to_keep and candidates:
            to_keep.append(candidates.pop(0))

        # Apply max_age filter: only remove candidates older than max_age
        if self.max_age_seconds is not None:
            cutoff = now - self.max_age_seconds
            to_remove = [f for f in candidates if f[1] < cutoff]
        else:
            to_remove = candidates

        stats["noncurrent_kept"] += len(to_keep) + (len(candidates) - len(to_remove))

        for path, _mt, size in to_remove:
            if self.verbose >= 1:
                action = "Would remove" if self.dry_run else "Removing"
                print(f"  {action}: {path} ({_format_size(size)})", file=self._human)
            if not self.dry_run:
                if _safe_locked_unlink(path):
                    stats["removed"] += 1
                    stats["bytes_freed"] += size
                else:
                    if self.verbose >= 1:
                        print(f"  Failed to remove {path} (will retry)", file=self._human)
                    self._retry.append(
                        {
                            "path": path,
                            "is_dir": False,
                            "size": size,
                            "stats": stats,
                            "removed_key": "removed",
                            "unlink_kwargs": {},
                        }
                    )
            else:
                stats["removed"] += 1
                stats["bytes_freed"] += size

    # ------------------------------------------------------------------
    # PCH directory trimming
    # ------------------------------------------------------------------

    def trim_pchdir(self, pchdir):
        """Trim stale precompiled header directories from the PCH CAS.

        Each ``<pchdir>/<cmd_hash>/`` directory is one unique compile
        configuration (compiler + flags + header realpath). The trim
        policy treats each cmd_hash dir as an independent unit:

        * Sort all cmd_hash dirs by mtime, newest first.
        * Keep the newest ``keep_count`` overall.
        * If ``max_age_seconds`` is set, also keep anything younger
          than that even beyond ``keep_count``.

        Bucketing-by-header-basename was tried in v8.0.2 but caused
        cache thrash: two unrelated projects both using ``stdafx.h``
        evicted each other at the default ``keep_count=1``.
        Per-realpath bucketing is now used instead — each
        ``<pchdir>/<cmd_hash>/`` writes a sidecar ``manifest.json``
        recording the immediate header's realpath, and ``keep_count``
        is enforced per realpath bucket so cross-variant builds of the
        same header coexist. Legacy entries without a manifest fall
        back to the previous global ranking.

        Note on ``max_age``: "aged" means "old since written" (the
        cmd_hash dir's mtime), NOT "old since last accessed". A
        heavily-used PCH cmd_hash dir from months ago is still evicted
        if it falls outside ``keep_count`` and ``max_age``. atime is
        unreliable on noatime-mounted filesystems.

        Note on cache-key composition: the cmd_hash captures the
        immediate header's realpath. Transitive-header content hashes
        are stored in the sidecar manifest and consulted during trim —
        entries whose transitive content has changed are pre-evicted so
        the user does not pay the slow ``cc1`` PCH-stamp rejection at
        consume time.

        Args:
            pchdir: Path to the PCH CAS.

        Returns:
            dict with statistics: total_dirs_scanned, headers_found,
            dirs_kept, dirs_removed, failed, bytes_freed.
        """
        stats = {
            "total_dirs_scanned": 0,
            "headers_found": 0,
            "dirs_kept": 0,
            "dirs_removed": 0,
            "failed": 0,
            "bytes_freed": 0,
            "orphan_temps_removed": 0,
            "orphan_temp_bytes_freed": 0,
            "budget_removed": 0,
            "budget_bytes_freed": 0,
            "budget_unmet_bytes": 0,
        }

        if not os.path.isdir(pchdir):
            return stats

        # Phase 1: scan command_hash directories
        # dir_info: {command_hash: (path, mtime, total_size, [header_basenames])}
        dir_info = {}
        unique_headers: set[str] = set()

        try:
            entries = list(os.scandir(pchdir))
        except OSError as exc:
            print(f"Error scanning {pchdir}: {exc}", file=sys.stderr)
            return stats

        cmd_dirs = [e for e in entries if e.is_dir() and _PCH_COMMAND_HASH_RE.match(e.name)]
        stats["total_dirs_scanned"] = len(cmd_dirs)

        # Each cmd_hash dir (stat its mtime + sum its .gch sizes) is an
        # independent unit of metadata work — fan out across threads on a
        # high-latency filesystem, serial on local disk. ``.gch`` headers
        # whose ``header_base`` strips the 4-char ".gch" extension.
        workers = self._workers_for(pchdir)
        results = _map_scan(cmd_dirs, lambda e: _scan_one_cmd_hash_dir(e, (".gch",)), workers)

        for result in results:
            if result is None:
                continue
            name, path, dir_mtime, total_size, gch_names = result
            headers = [n[:-4] for n in gch_names]  # strip .gch
            dir_info[name] = (path, dir_mtime, total_size, headers)
            unique_headers.update(headers)

        stats["headers_found"] = len(unique_headers)
        now = time.time()

        # Phase 2: bucket cmd_hash dirs by header_realpath (from sidecar
        # manifest) and apply ``keep_count`` per bucket. Legacy entries
        # without a manifest fall into the ``__legacy__`` bucket and use
        # the previous global-ranking semantics so the rollout is
        # backwards-compatible.
        buckets: dict[str, list[str]] = {}
        for cmd_hash, (path, _mtime, _size, _headers) in dir_info.items():
            manifest = _load_pch_manifest(path)
            realpath = manifest.get("header_realpath") if manifest else None
            key = realpath or "__legacy__"
            buckets.setdefault(key, []).append(cmd_hash)

        needed_dirs: set[str] = set()
        for _bucket_key, hashes in buckets.items():
            sorted_hashes = sorted(hashes, key=lambda ch: dir_info[ch][1], reverse=True)
            needed_dirs.update(sorted_hashes[: self.keep_count])

        if self.max_age_seconds is not None:
            cutoff = now - self.max_age_seconds
            for cmd_hash, (_path, mtime, _size, _headers) in dir_info.items():
                if mtime >= cutoff:
                    needed_dirs.add(cmd_hash)

        # Phase 2b: pre-evict entries whose transitive headers have
        # changed since the .gch was built. Best-effort — manifest
        # absence or unreadable headers leave the entry alone.
        # Hashes use the same git-blob-SHA1 algorithm as
        # ``global_hash_registry._compute_external_file_hash`` so
        # comparisons are meaningful without taking on a BuildContext
        # dependency in the trim CLI.
        for cmd_hash in list(needed_dirs):
            path = dir_info[cmd_hash][0]
            manifest = _load_pch_manifest(path)
            if not manifest:
                continue
            for h_realpath, expected_hash in manifest.get("transitive_hashes", {}).items():
                try:
                    with open(h_realpath, "rb") as fh:
                        content = fh.read()
                except OSError:
                    continue  # best-effort: missing or unreadable
                current = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
                if current != expected_hash:
                    if self.verbose >= 1:
                        print(f"  Pre-evicting {path} (transitive {h_realpath} changed)", file=self._human)
                    needed_dirs.discard(cmd_hash)
                    break

        # Phase 3: remove directories not needed
        for cmd_hash, (path, _mtime, total_size, _headers) in dir_info.items():
            if cmd_hash in needed_dirs:
                stats["dirs_kept"] += 1
                continue

            if self.verbose >= 1:
                action = "Would remove" if self.dry_run else "Removing"
                print(f"  {action}: {path} ({_format_size(total_size)})", file=self._human)

            if not self.dry_run:
                # Lock each .gch file before removing the cmd_hash
                # dir. If a build is currently generating one of the .gch
                # files, we block until it releases — never deleting a
                # file a peer is mid-write to. Best-effort: filesystems
                # that don't support our lock strategies fall through to
                # plain rmtree (the lock acquisition is wrapped to ignore
                # errors so we don't fail trims on unlocked filesystems).
                if _safe_locked_rmtree(path):
                    stats["dirs_removed"] += 1
                    stats["bytes_freed"] += total_size
                else:
                    if self.verbose >= 1:
                        print(f"  Failed to remove {path} (will retry)", file=self._human)
                    self._retry.append(
                        {
                            "path": path,
                            "is_dir": True,
                            "size": total_size,
                            "stats": stats,
                            "removed_key": "dirs_removed",
                            "unlink_kwargs": {},
                        }
                    )
            else:
                stats["dirs_removed"] += 1
                stats["bytes_freed"] += total_size

        return stats

    # ------------------------------------------------------------------
    # PCM directory trimming
    # ------------------------------------------------------------------

    def trim_pcmdir(self, pcmdir):
        """Trim stale precompiled C++20 module artefacts from the PCM CAS.

        Layout: ``<pcmdir>/<command_hash>/<name>.{pcm,gcm}`` plus a
        sidecar ``manifest.json`` per command_hash directory. Mirrors
        ``trim_pchdir``: each command_hash dir is one unique compile
        configuration; the manifest carries ``bucket_key`` (source
        realpath for named modules, verbatim token for header units),
        ``stage``, and ``transitive_hashes``.

        The single-hash + manifest design here is **deliberately
        simpler** than the object cache's 3-hash path -- see
        ``_pcm_command_hash`` for why PCM doesn't need the object
        cache's per-axis path isolation. The compiler verifies BMIs
        at consume time, so the worst case of a hypothetical 64-bit
        collision is a slow re-precompile, never a miscompile.

        Trim policy:

        - **bucketing** (mirrors pchdir): per-bucket ``keep_count``
          using ``bucket_key`` from the manifest. Cross-variant builds
          of the same source coexist at ``keep_count=1``.
        - **transitive-staleness pre-eviction** (mirrors pchdir): if
          a recorded transitive header's content hash no longer
          matches its on-disk content, the entry is pre-evicted.
        - **max_age**: keeps anything younger than the cutoff
          regardless of bucket position.

        Args:
            pcmdir: Path to the PCM CAS.

        Returns:
            dict with statistics: total_dirs_scanned, buckets_found,
            dirs_kept, dirs_removed, failed, bytes_freed.
        """
        stats = {
            "total_dirs_scanned": 0,
            "buckets_found": 0,
            "dirs_kept": 0,
            "dirs_removed": 0,
            "failed": 0,
            "bytes_freed": 0,
            "orphan_temps_removed": 0,
            "orphan_temp_bytes_freed": 0,
            "budget_removed": 0,
            "budget_bytes_freed": 0,
            "budget_unmet_bytes": 0,
        }

        if not os.path.isdir(pcmdir):
            return stats

        # Phase 1: scan command_hash directories.
        # dir_info: {cmd_hash: (path, mtime, total_size, [leaf_filenames])}
        dir_info: dict[str, tuple] = {}

        try:
            entries = list(os.scandir(pcmdir))
        except OSError as exc:
            print(f"Error scanning {pcmdir}: {exc}", file=sys.stderr)
            return stats

        cmd_dirs = [e for e in entries if e.is_dir() and _PCM_COMMAND_HASH_RE.match(e.name)]
        stats["total_dirs_scanned"] = len(cmd_dirs)

        # Same fan-out as pchdir: each cmd_hash dir is an independent unit of
        # metadata work, parallelised on high-latency filesystems.
        workers = self._workers_for(pcmdir)
        results = _map_scan(cmd_dirs, lambda e: _scan_one_cmd_hash_dir(e, (".pcm", ".gcm")), workers)

        for result in results:
            if result is None:
                continue
            name, path, dir_mtime, total_size, leaves = result
            dir_info[name] = (path, dir_mtime, total_size, leaves)

        # Phase 2: bucket by manifest's bucket_key. Header-unit entries
        # use the token (``<vector>``); named-module entries use the
        # source realpath. Legacy / manifest-less entries fall into
        # ``__legacy__`` for global ranking so older builds keep working.
        buckets: dict[str, list[str]] = {}
        for cmd_hash, (path, _mtime, _size, _leaves) in dir_info.items():
            manifest = _load_pcm_manifest(path)
            bucket_key = (manifest.get("bucket_key") if manifest else None) or "__legacy__"
            buckets.setdefault(bucket_key, []).append(cmd_hash)
        stats["buckets_found"] = sum(1 for k in buckets if k != "__legacy__")

        needed_dirs: set[str] = set()
        for _bucket_key, hashes in buckets.items():
            sorted_hashes = sorted(hashes, key=lambda ch: dir_info[ch][1], reverse=True)
            needed_dirs.update(sorted_hashes[: self.keep_count])

        now = time.time()
        if self.max_age_seconds is not None:
            cutoff = now - self.max_age_seconds
            for cmd_hash, (_path, mtime, _size, _leaves) in dir_info.items():
                if mtime >= cutoff:
                    needed_dirs.add(cmd_hash)

        # Phase 2b: pre-evict entries whose transitive headers have
        # changed. Same git-blob SHA1 algorithm as
        # ``global_hash_registry._compute_external_file_hash``.
        for cmd_hash in list(needed_dirs):
            path = dir_info[cmd_hash][0]
            manifest = _load_pcm_manifest(path)
            if not manifest:
                continue
            for h_realpath, expected_hash in manifest.get("transitive_hashes", {}).items():
                try:
                    with open(h_realpath, "rb") as fh:
                        content = fh.read()
                except OSError:
                    continue
                current = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
                if current != expected_hash:
                    if self.verbose >= 1:
                        print(f"  Pre-evicting {path} (transitive {h_realpath} changed)", file=self._human)
                    needed_dirs.discard(cmd_hash)
                    break

        # Phase 3: remove dirs not in the keep set.
        for cmd_hash, (path, _mtime, total_size, _leaves) in dir_info.items():
            if cmd_hash in needed_dirs:
                stats["dirs_kept"] += 1
                continue

            if self.verbose >= 1:
                action = "Would remove" if self.dry_run else "Removing"
                print(f"  {action}: {path} ({_format_size(total_size)})", file=self._human)

            if not self.dry_run:
                if _safe_locked_rmtree(path):
                    stats["dirs_removed"] += 1
                    stats["bytes_freed"] += total_size
                else:
                    if self.verbose >= 1:
                        print(f"  Failed to remove {path} (will retry)", file=self._human)
                    self._retry.append(
                        {
                            "path": path,
                            "is_dir": True,
                            "size": total_size,
                            "stats": stats,
                            "removed_key": "dirs_removed",
                            "unlink_kwargs": {},
                        }
                    )
            else:
                stats["dirs_removed"] += 1
                stats["bytes_freed"] += total_size

        return stats

    # ------------------------------------------------------------------
    # Executable cache (cas-exedir) trimming
    # ------------------------------------------------------------------

    def trim_exedir(self, exedir):
        """Trim stale entries from the content-addressable linker-artefact
        cache (executables, static libraries, shared libraries — all
        share the cas-exedir root).

        Layout: ``<exedir>/<key[:2]>/<basename>_<key>.<ext>`` with
        ``<ext>`` ∈ ``{.exe, .a, .so}``. Flatter than the PCH/PCM
        caches (no per-entry sidecar manifest; the key itself is the
        cache identity).

        Trim policy:

        * **Bucket** by ``(basename, suffix)`` (the part of the
          filename before ``_<key>``, plus its suffix). One
          executable's distinct link configurations live in the same
          bucket — and ``libfoo.a`` and ``libfoo.so`` bucket
          separately because the suffix differs. The newest
          ``keep_count`` per bucket survive bucket-rank eviction.
        * **max_age**: anything younger than the cutoff is kept
          regardless of bucket position (mirrors objdir / pchdir /
          pcmdir).
        * **Hard-link safety**: skip files with ``st_nlink > 1``.
          The ``symlink`` rule publishes ``bin/<variant>/<name>`` as a
          hard link to the cas entry; a second reference means a
          user-facing artefact is still pointing at this inode and
          deleting it would force a relink on the next build for no
          actual savings (the inode would survive anyway via the
          remaining link). Symlinked-fallback bin paths show
          ``st_nlink == 1`` on the cas entry and are NOT protected — the
          user can rebuild if a symlink dangles.
        * **Lock-aware delete**: ``_safe_locked_unlink`` acquires
          ``<path>.lock`` via the same ``FileLock`` strategy used by
          the link/ar wrapper, so a trim that lands mid-link blocks
          until the link releases the lock instead of clobbering a
          partially-renamed artefact.

        Sidecar lock files (``*.lock`` / ``*.lock.excl``) are filtered
        at the suffix-match step — they don't end in any of the
        ``_CAS_EXE_SUFFIXES`` so they never become candidates.

        Args:
            exedir: Path to the linker-artefact CAS.

        Returns:
            dict with statistics: total_scanned, basenames_found,
            kept, removed, failed, bytes_freed.
        """
        stats = {
            "total_scanned": 0,
            "basenames_found": 0,
            "kept": 0,
            "removed": 0,
            "failed": 0,
            "bytes_freed": 0,
            "orphan_temps_removed": 0,
            "orphan_temp_bytes_freed": 0,
            "budget_removed": 0,
            "budget_bytes_freed": 0,
            "budget_unmet_bytes": 0,
        }

        if not os.path.isdir(exedir):
            return stats

        # entry_info: {full_path: (bucket_key, mtime, size, st_nlink)}
        # bucket_key prefers ``(source_realpath, suffix)`` from the
        # sidecar manifest written at link time (C4 — disambiguates
        # distinct executables that happen to share a basename like
        # ``main``). Falls back to ``(basename, suffix)`` for legacy
        # entries that pre-date the sidecar contract (existing caches
        # don't suddenly behave differently after upgrading).
        entry_info: dict[str, tuple] = {}
        with os.scandir(exedir) as exe_entries:
            bucket_dirs = [e for e in exe_entries if e.is_dir()]

        # Each top-level bucket is an independent unit of metadata work
        # (stat + sidecar-manifest read per artefact); fan out on a
        # high-latency filesystem, serial on local disk.
        workers = self._workers_for(exedir)
        results = _map_scan(bucket_dirs, _scan_one_exe_bucket, workers)

        for recs in results:
            for path, bucket_key, mtime, size, nlink in recs:
                entry_info[path] = (bucket_key, mtime, size, nlink)
                stats["total_scanned"] += 1

        if not entry_info:
            return stats

        # Bucket by executable basename, then sort each bucket newest-first
        # and keep the top keep_count.
        buckets: dict[str, list[str]] = {}
        for path, (basename, _mtime, _size, _nlink) in entry_info.items():
            buckets.setdefault(basename, []).append(path)
        stats["basenames_found"] = len(buckets)

        keep_paths: set[str] = set()
        for paths in buckets.values():
            paths.sort(key=lambda p: entry_info[p][1], reverse=True)
            keep_paths.update(paths[: self.keep_count])

        # max-age: keep anything younger than the cutoff regardless of rank.
        now = time.time()
        if self.max_age_seconds is not None:
            cutoff = now - self.max_age_seconds
            for path, (_basename, mtime, _size, _nlink) in entry_info.items():
                if mtime >= cutoff:
                    keep_paths.add(path)

        # Hard-link safety: anything with another reference is in use.
        for path, (_basename, _mtime, _size, nlink) in entry_info.items():
            if nlink > 1:
                keep_paths.add(path)

        for path, (_basename, _mtime, size, _nlink) in entry_info.items():
            if path in keep_paths:
                stats["kept"] += 1
                continue
            if self.dry_run:
                if self.verbose >= 1:
                    print(f"  Would remove: {path}", file=self._human)
                stats["removed"] += 1
                stats["bytes_freed"] += size
                continue
            # I4: re-stat under the lock and skip if a peer publish
            # elevated nlink between the initial scan and this unlink.
            if _safe_locked_unlink(path, skip_if_nlink_above=1):
                stats["removed"] += 1
                stats["bytes_freed"] += size
                # Sidecar files are best-effort cleanup — don't count
                # towards bytes_freed (small, ignore failure). The
                # ``.result`` sidecar is the per-CAS-entry test-success
                # marker touched by the in-build test rules in CAS-only mode.
                for sidecar_suffix in (".manifest", ".result"):
                    try:
                        os.remove(path + sidecar_suffix)
                    except OSError:
                        pass
            else:
                if self.verbose >= 1:
                    print(f"  Failed to remove {path} (will retry)", file=self._human)
                self._retry.append(
                    {
                        "path": path,
                        "is_dir": False,
                        "size": size,
                        "stats": stats,
                        "removed_key": "removed",
                        "unlink_kwargs": {"skip_if_nlink_above": 1},
                        "cleanup_sidecars": True,
                    }
                )

        return stats

    # ------------------------------------------------------------------
    # Retry pass
    # ------------------------------------------------------------------

    def retry_failed(self):
        """Retry every queued removal exactly once and clear the retry list.

        Called by ``main()`` after ALL four trim passes and BEFORE the summary
        is printed, so reported numbers reflect the final post-retry state.

        On retry success: increment ``removed`` (or the entry's ``removed_key``)
        and ``bytes_freed`` in the per-cache stats dict (stored by reference in
        each retry entry).  When the entry carries an optional ``"bytes_key"``
        (e.g. ``"orphan_temp_bytes_freed"`` for orphan-temp retries), that
        per-category counter is also incremented so it stays consistent with the
        direct-success path.  For exedir entries (``"cleanup_sidecars": True``)
        also best-effort-remove the ``.manifest``/``.result`` sidecars.

        On retry failure: increment ``failed`` — the path is intentionally left
        in place (a peer build is holding it; it will be retried on the next
        trim run).

        At ``verbose >= 1`` a one-line note per retry outcome is written to
        ``self._human``.
        """
        for entry in self._retry:
            path = entry["path"]
            is_dir = entry["is_dir"]
            size = entry["size"]
            stats = entry["stats"]
            removed_key = entry["removed_key"]
            unlink_kwargs = entry["unlink_kwargs"]

            if is_dir:
                success = _safe_locked_rmtree(path)
            else:
                success = _safe_locked_unlink(path, **unlink_kwargs)

            if success:
                stats[removed_key] += 1
                stats["bytes_freed"] += size
                # Credit the per-category byte counter when provided (e.g.
                # orphan_temp_bytes_freed for orphan-temp retries).  Other
                # retry sites (objdir, pchdir, pcmdir, exedir) omit bytes_key.
                if entry.get("bytes_key"):
                    stats[entry["bytes_key"]] += size
                # Reconcile budget_unmet_bytes: a failed budget removal was
                # conservatively left in the unmet tally.  On retry success the
                # bytes are gone, so reduce the unmet counter by the entry's
                # size (floor at 0 to guard against double-crediting).
                if entry.get("unmet_key"):
                    stats[entry["unmet_key"]] = max(0, stats[entry["unmet_key"]] - size)
                if self.verbose >= 1:
                    print(f"  Retry succeeded: {path}", file=self._human)
                # Best-effort sidecar cleanup for exedir entries (flagged
                # explicitly via "cleanup_sidecars": True in the retry entry).
                if entry.get("cleanup_sidecars"):
                    for sidecar_suffix in (".manifest", ".result"):
                        try:
                            os.remove(path + sidecar_suffix)
                        except OSError:
                            pass
            else:
                stats["failed"] += 1
                if self.verbose >= 1:
                    print(f"  Retry failed (leaving in place): {path}", file=self._human)

        self._retry.clear()

    # ------------------------------------------------------------------
    # Orphan temp reclamation
    # ------------------------------------------------------------------

    def reclaim_orphan_temps(self, cache_root: str, stats: dict) -> None:
        """Remove orphaned producer temp files from a CAS directory.

        Walks one level into each immediate subdirectory of ``cache_root`` (the
        bucket / cmd_hash dirs). For every file matched by ``_COMPILETOOLS_TMP_RE``
        or ending with ``_PUBLISH_TMP_SUFFIX``, AND whose mtime is older than
        ``now - _ORPHAN_TEMP_MIN_AGE_SECONDS``, the file is removed via
        ``_safe_locked_unlink`` (so a temp that is somehow still locked by a peer
        is left in place and queued on ``self._retry`` for a single retry after
        all four caches finish).

        Safety properties:

        * **Age floor** (``_ORPHAN_TEMP_MIN_AGE_SECONDS = 86400``): a temp
          untouched for one day cannot be an in-flight write — no build stays
          alive across a day.  Removing it cannot race a live producer.
        * **Lock-aware unlink**: ``_safe_locked_unlink`` acquires the build lock
          before unlinking, so a temp that a peer has re-acquired mid-trim is
          left safely in place.
        * **One-level descent only**: never recurses beyond the immediate
          subdirectory layer (bucket / cmd_hash dir), so this path cannot
          accidentally walk into unrelated directory trees.

        Skips ``*.lock`` / ``*.lock.excl`` / ``*.lockdir`` entries (lock sidecar
        files managed by the locking subsystem, not build outputs).

        Honours ``dry_run``: in dry-run mode the counts are accumulated as
        would-be removals but no unlink is performed and ``self._retry`` is never
        populated.

        Args:
            cache_root: Path to one CAS root directory (e.g. ``args.cas_objdir``).
                If not an existing directory the function is a no-op.
            stats: The per-cache stats dict to accumulate into. Must contain
                ``"orphan_temps_removed"`` and ``"orphan_temp_bytes_freed"`` keys
                (initialised by each ``trim_*`` method).
        """
        if not os.path.isdir(cache_root):
            return

        now = time.time()
        cutoff = now - _ORPHAN_TEMP_MIN_AGE_SECONDS

        try:
            with os.scandir(cache_root) as top_it:
                subdirs = [e.path for e in top_it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return  # cache_root became unreadable mid-scan; best-effort

        for subdir_path in subdirs:
            try:
                with os.scandir(subdir_path) as inner_it:
                    entries = list(inner_it)
            except OSError:
                continue  # subdir vanished mid-scan; best-effort

            for entry in entries:
                name = entry.name
                # Skip lock sidecar files managed by the locking subsystem.
                if name.endswith((".lock", ".lock.excl", ".lockdir")):
                    continue
                # _PUBLISH_TMP_SUFFIX arm: cas_publish writes *.publish.tmp into
                # dirname(user_path) = the published bin/<variant>/ dir, which
                # ct-trim-cache does NOT scan (it only owns the cas-*dir roots).
                # This arm is therefore defensive — it reclaims nothing in
                # production but will catch any future layout where a publish
                # temp lands inside a CAS bucket dir.
                if not (_COMPILETOOLS_TMP_RE.search(name) or name.endswith(_PUBLISH_TMP_SUFFIX)):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue  # file vanished mid-scan; best-effort
                if st.st_mtime >= cutoff:
                    continue  # too fresh — might be an in-flight write
                size = st.st_size
                path = entry.path
                if self.verbose >= 1:
                    action = "Would remove orphan temp" if self.dry_run else "Removing orphan temp"
                    print(f"  {action}: {path} ({_format_size(size)})", file=self._human)
                if self.dry_run:
                    stats["orphan_temps_removed"] += 1
                    stats["orphan_temp_bytes_freed"] += size
                    stats["bytes_freed"] += size
                else:
                    if _safe_locked_unlink(path):
                        stats["orphan_temps_removed"] += 1
                        stats["orphan_temp_bytes_freed"] += size
                        stats["bytes_freed"] += size
                    else:
                        if self.verbose >= 1:
                            print(f"  Failed to remove orphan temp {path} (will retry)", file=self._human)
                        self._retry.append(
                            {
                                "path": path,
                                "is_dir": False,
                                "size": size,
                                "stats": stats,
                                "removed_key": "orphan_temps_removed",
                                "bytes_key": "orphan_temp_bytes_freed",
                                "unlink_kwargs": {},
                            }
                        )

    # ------------------------------------------------------------------
    # Per-pool size budget (--max-size)
    # ------------------------------------------------------------------

    def enforce_budget(self, cache_dir, stats, *, kind, current_hashes=None):
        """Evict non-protected (rebuildable) units oldest-first until the pool's
        total on-disk size is at or below ``self.max_size_bytes``.

        Runs AFTER the normal variant-driven trim and orphan-temp reclaim, so it
        RE-SCANS whatever survived those passes. No-op when ``--max-size`` was
        not supplied (``self.max_size_bytes is None``) or ``cache_dir`` is not an
        existing directory.

        Floor semantics: the default ``--keep-count`` (objects/exes) and
        ``keep >= 1`` (pch/pcm cmd_hash dirs) floors are UNCHANGED for the normal
        trim path. ONLY this explicit ``--max-size`` path may evict below those
        floors, and ONLY for non-protected (rebuildable) units — that is the
        deliberate purpose of a size budget on a space-constrained pool.

        PEER SAFETY: a *protected* unit is NEVER evicted, regardless of the
        budget. Protection is kind-specific:

        * ``obj`` — an object whose ``file_hash`` is in ``current_hashes`` (a
          current object for the invoking checkout).
        * ``exe`` — an artefact with ``st_nlink > 1`` (a published / hard-linked
          reference is still live).
        * ``pch`` / ``pcm`` — no per-unit protection signal exists at this layer,
          so all cmd_hash dirs are eviction candidates (``protected=False``);
          the compiler re-precompiles on the next build.

        If protected units alone exceed the budget, the overflow is reported via
        ``stats["budget_unmet_bytes"]`` and the budget is left UNMET rather than
        violated.

        Units and their (path, mtime, size, protected) records by kind:

        * ``obj`` — each parseable ``.o`` in a 2-hex bucket.
        * ``exe`` — each artefact (``_CAS_EXE_SUFFIXES``) in a ``key[:2]`` bucket.
        * ``pch`` / ``pcm`` — each ``cmd_hash`` directory as a whole unit (newest
          leaf mtime, total dir size).

        Args:
            cache_dir: The resolved CAS directory for this pool.
            stats: The per-cache stats dict (mutated in place: ``budget_removed``,
                ``budget_bytes_freed``, ``budget_unmet_bytes``, ``bytes_freed``).
            kind: One of ``"obj"`` / ``"pch"`` / ``"pcm"`` / ``"exe"``.
            current_hashes: Set of current 12-char file-hash prefixes; only used
                (and only required) for ``kind == "obj"`` to mark protected
                objects. Ignored for the other kinds.
        """
        if self.max_size_bytes is None:
            return
        if not os.path.isdir(cache_dir):
            return

        if kind == "obj":
            units = self._budget_scan_obj(cache_dir, current_hashes or set())
        elif kind == "exe":
            units = self._budget_scan_exe(cache_dir)
        elif kind in ("pch", "pcm"):
            units = self._budget_scan_cmd_hash_dirs(cache_dir, kind)
        else:  # pragma: no cover - guarded by the caller's fixed kind set
            raise ValueError(f"enforce_budget: unknown kind {kind!r}")

        # total includes BOTH protected and non-protected units — the budget is
        # a statement about the whole pool, and protected bytes count against it
        # (they are simply not eligible for eviction).
        total = sum(size for _path, _mtime, size, _protected in units)
        if total <= self.max_size_bytes:
            stats["budget_unmet_bytes"] = 0
            return

        # Evict non-protected units oldest-first (mtime ascending).
        candidates = sorted(
            (u for u in units if not u[3]),
            key=lambda u: u[1],
        )
        for path, _mtime, size, _protected in candidates:
            if total <= self.max_size_bytes:
                break
            is_dir = kind in ("pch", "pcm")
            cleanup_sidecars = kind == "exe"
            unlink_kwargs = {"skip_if_nlink_above": 1} if kind == "exe" else {}

            if self.verbose >= 1:
                action = "Would remove (budget)" if self.dry_run else "Removing (budget)"
                print(f"  {action}: {path} ({_format_size(size)})", file=self._human)

            if self.dry_run:
                # Count would-be removals and subtract from the running total so
                # the unmet calc reflects what a real run would reclaim, but
                # touch nothing on disk and never populate the retry list.
                stats["budget_removed"] += 1
                stats["budget_bytes_freed"] += size
                total -= size
                continue

            removed = _safe_locked_rmtree(path) if is_dir else _safe_locked_unlink(path, **unlink_kwargs)
            if removed:
                total -= size
                stats["budget_removed"] += 1
                stats["budget_bytes_freed"] += size
                stats["bytes_freed"] += size
                if cleanup_sidecars:
                    for sidecar_suffix in (".manifest", ".result"):
                        try:
                            os.remove(path + sidecar_suffix)
                        except OSError:
                            pass
            else:
                # Failed removal — queue for the single post-pass retry exactly
                # like every other site. Do NOT decrement total: for the unmet
                # calc we conservatively assume it stays (it may be reclaimed on
                # retry, but we must not under-report the overflow).
                if self.verbose >= 1:
                    print(f"  Failed to remove {path} (will retry)", file=self._human)
                retry_entry = {
                    "path": path,
                    "is_dir": is_dir,
                    "size": size,
                    "stats": stats,
                    "removed_key": "budget_removed",
                    "bytes_key": "budget_bytes_freed",
                    # On retry success, budget_unmet_bytes must be decremented by
                    # this entry's size — a successfully-retried budget eviction
                    # was conservatively left in the unmet tally at queue time.
                    "unmet_key": "budget_unmet_bytes",
                    "unlink_kwargs": unlink_kwargs,
                }
                if cleanup_sidecars:
                    retry_entry["cleanup_sidecars"] = True
                self._retry.append(retry_entry)

        stats["budget_unmet_bytes"] = max(0, total - self.max_size_bytes)

    def _budget_scan_obj(self, objdir, current_hashes):
        """Re-scan ``objdir`` for the budget pass: one record per parseable ``.o``.

        Returns ``[(path, mtime, size, protected), ...]`` where ``protected`` is
        ``True`` when the object's ``file_hash`` is in ``current_hashes``. Skips
        ``.lockdir`` / lock sidecars and orphan temps (matching the trim scan).
        """
        units: list[tuple[str, float, int, bool]] = []
        try:
            with os.scandir(objdir) as top_it:
                bucket_paths = [
                    e.path for e in top_it if _OBJ_BUCKET_RE.match(e.name) and e.is_dir(follow_symlinks=False)
                ]
        except OSError:
            return units
        for bucket_path in bucket_paths:
            try:
                inner = list(os.scandir(bucket_path))
            except OSError:
                continue
            for entry in inner:
                name = entry.name
                if not name.endswith(".o"):
                    continue
                if _COMPILETOOLS_TMP_RE.search(name) or name.endswith(_PUBLISH_TMP_SUFFIX):
                    continue
                parsed = parse_object_filename(name)
                if parsed is None:
                    continue
                _basename, file_hash, _dep_hash, _macro_hash = parsed
                ms = _entry_mtime_size(entry)
                if ms is None:
                    continue
                units.append((entry.path, ms[0], ms[1], file_hash in current_hashes))
        return units

    def _budget_scan_exe(self, exedir):
        """Re-scan ``exedir`` for the budget pass: one record per CAS artefact.

        Returns ``[(path, mtime, size, protected), ...]`` where ``protected`` is
        ``True`` when ``st_nlink > 1`` (a published / hard-linked reference is
        still live). Non-artefact files (lock sidecars, ``.manifest``/``.result``)
        are skipped via the suffix match.
        """
        units: list[tuple[str, float, int, bool]] = []
        try:
            with os.scandir(exedir) as top_it:
                bucket_paths = [e.path for e in top_it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return units
        for bucket_path in bucket_paths:
            try:
                inner = list(os.scandir(bucket_path))
            except OSError:
                continue
            for entry in inner:
                if not entry.is_file():
                    continue
                if not entry.name.endswith(_CAS_EXE_SUFFIXES):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                units.append((entry.path, st.st_mtime, st.st_size, st.st_nlink > 1))
        return units

    def _budget_scan_cmd_hash_dirs(self, cache_dir, kind):
        """Re-scan a PCH/PCM pool for the budget pass: one record per cmd_hash dir.

        Returns ``[(dir_path, newest_mtime, total_dir_size, False), ...]`` — each
        cmd_hash dir is one unit and none are protected at this layer (the
        compiler re-precompiles on the next build). ``newest_mtime`` is the max
        leaf mtime (falling back to the dir's own mtime when it has no leaves).
        """
        hash_re = _PCH_COMMAND_HASH_RE if kind == "pch" else _PCM_COMMAND_HASH_RE
        leaf_suffixes = (".gch",) if kind == "pch" else (".pcm", ".gcm")
        units: list[tuple[str, float, int, bool]] = []
        try:
            with os.scandir(cache_dir) as top_it:
                cmd_dirs = [e for e in top_it if e.is_dir() and hash_re.match(e.name)]
        except OSError:
            return units
        for entry in cmd_dirs:
            total_size = 0
            newest = None
            try:
                with os.scandir(entry.path) as leaf_it:
                    for leaf in leaf_it:
                        if not leaf.name.endswith(leaf_suffixes) or not leaf.is_file():
                            continue
                        ms = _entry_mtime_size(leaf)
                        if ms is None:
                            continue
                        total_size += ms[1]
                        if newest is None or ms[0] > newest:
                            newest = ms[0]
            except OSError:
                continue
            if newest is None:
                ms = _entry_mtime_size(entry)
                newest = ms[0] if ms is not None else 0.0
            units.append((entry.path, newest, total_size, False))
        return units

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_budget_lines(self, cache_stats):
        """Print the ``--max-size`` budget lines for one cache (no-op when no
        budget eviction occurred and the budget was met).

        Always surfaces a nonzero ``budget_unmet_bytes`` — the operator needs to
        know when protected (current / hard-linked) entries alone exceed the
        budget, since that is reported but never violated.
        """
        if cache_stats["budget_removed"]:
            budget_str = f"    Budget evicted:  {cache_stats['budget_removed']}"
            if cache_stats["budget_bytes_freed"]:
                budget_str += f" ({_format_size(cache_stats['budget_bytes_freed'])} freed)"
            print(budget_str, file=self._human)
        if cache_stats["budget_unmet_bytes"]:
            print(
                f"    Budget unmet:    {_format_size(cache_stats['budget_unmet_bytes'])} over "
                "(protected/current entries alone exceed --max-size)",
                file=self._human,
            )

    def print_summary(self, objdir_stats=None, pchdir_stats=None, pcmdir_stats=None, exedir_stats=None):
        """Print a formatted summary of trimming results."""
        total_freed = 0
        print(file=self._human)
        print("=" * 60, file=self._human)
        print("Cache trim complete", file=self._human)

        if objdir_stats is not None:
            total_freed += objdir_stats["bytes_freed"]
            print("  Object files:", file=self._human)
            print(f"    Total scanned:   {objdir_stats['total_scanned']}", file=self._human)
            print(f"    Basenames found: {objdir_stats['basenames_found']}", file=self._human)
            print(f"    Current (kept):  {objdir_stats['current_kept']}", file=self._human)
            print(f"    Non-current kept:{objdir_stats['noncurrent_kept']}", file=self._human)
            removed_str = f"    Removed:         {objdir_stats['removed']}"
            if objdir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(objdir_stats['bytes_freed'])} freed)"
            print(removed_str, file=self._human)
            if objdir_stats["failed"]:
                print(f"    Failed:          {objdir_stats['failed']}", file=self._human)
            if objdir_stats["orphan_temps_removed"]:
                orphan_str = f"    Orphan temps:    {objdir_stats['orphan_temps_removed']}"
                if objdir_stats["orphan_temp_bytes_freed"]:
                    orphan_str += f" ({_format_size(objdir_stats['orphan_temp_bytes_freed'])} freed)"
                print(orphan_str, file=self._human)
            self._print_budget_lines(objdir_stats)

        if pchdir_stats is not None:
            total_freed += pchdir_stats["bytes_freed"]
            print("  PCH directories:", file=self._human)
            print(f"    Total scanned:   {pchdir_stats['total_dirs_scanned']}", file=self._human)
            print(f"    Headers found:   {pchdir_stats['headers_found']}", file=self._human)
            print(f"    Kept:            {pchdir_stats['dirs_kept']}", file=self._human)
            removed_str = f"    Removed:         {pchdir_stats['dirs_removed']}"
            if pchdir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(pchdir_stats['bytes_freed'])} freed)"
            print(removed_str, file=self._human)
            if pchdir_stats["failed"]:
                print(f"    Failed:          {pchdir_stats['failed']}", file=self._human)
            if pchdir_stats["orphan_temps_removed"]:
                orphan_str = f"    Orphan temps:    {pchdir_stats['orphan_temps_removed']}"
                if pchdir_stats["orphan_temp_bytes_freed"]:
                    orphan_str += f" ({_format_size(pchdir_stats['orphan_temp_bytes_freed'])} freed)"
                print(orphan_str, file=self._human)
            self._print_budget_lines(pchdir_stats)

        if pcmdir_stats is not None:
            total_freed += pcmdir_stats["bytes_freed"]
            print("  PCM directories:", file=self._human)
            print(f"    Total scanned:   {pcmdir_stats['total_dirs_scanned']}", file=self._human)
            print(f"    Buckets found:   {pcmdir_stats['buckets_found']}", file=self._human)
            print(f"    Kept:            {pcmdir_stats['dirs_kept']}", file=self._human)
            removed_str = f"    Removed:         {pcmdir_stats['dirs_removed']}"
            if pcmdir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(pcmdir_stats['bytes_freed'])} freed)"
            print(removed_str, file=self._human)
            if pcmdir_stats["failed"]:
                print(f"    Failed:          {pcmdir_stats['failed']}", file=self._human)
            if pcmdir_stats["orphan_temps_removed"]:
                orphan_str = f"    Orphan temps:    {pcmdir_stats['orphan_temps_removed']}"
                if pcmdir_stats["orphan_temp_bytes_freed"]:
                    orphan_str += f" ({_format_size(pcmdir_stats['orphan_temp_bytes_freed'])} freed)"
                print(orphan_str, file=self._human)
            self._print_budget_lines(pcmdir_stats)

        if exedir_stats is not None:
            total_freed += exedir_stats["bytes_freed"]
            print("  Executable cache:", file=self._human)
            print(f"    Total scanned:   {exedir_stats['total_scanned']}", file=self._human)
            print(f"    Basenames found: {exedir_stats['basenames_found']}", file=self._human)
            print(f"    Kept:            {exedir_stats['kept']}", file=self._human)
            removed_str = f"    Removed:         {exedir_stats['removed']}"
            if exedir_stats["bytes_freed"]:
                removed_str += f" ({_format_size(exedir_stats['bytes_freed'])} freed)"
            print(removed_str, file=self._human)
            if exedir_stats["failed"]:
                print(f"    Failed:          {exedir_stats['failed']}", file=self._human)
            if exedir_stats["orphan_temps_removed"]:
                orphan_str = f"    Orphan temps:    {exedir_stats['orphan_temps_removed']}"
                if exedir_stats["orphan_temp_bytes_freed"]:
                    orphan_str += f" ({_format_size(exedir_stats['orphan_temp_bytes_freed'])} freed)"
                print(orphan_str, file=self._human)
            self._print_budget_lines(exedir_stats)

        # The summary line aggregates whatever was actually scanned.
        scanned = sum(s is not None for s in (objdir_stats, pchdir_stats, pcmdir_stats, exedir_stats))
        if scanned >= 2:
            print(f"  Total space freed: {_format_size(total_freed)}", file=self._human)
        print("=" * 60, file=self._human)

    def summary_json(self, objdir_stats=None, pchdir_stats=None, pcmdir_stats=None, exedir_stats=None):
        """Return a dict with raw integer counts/bytes per cache that ran.

        Top-level ``schema`` (always ``1``) and ``mode`` (always
        ``"trim"``) identify the payload shape for machine consumers.
        Keys ``objdir`` / ``pchdir`` / ``pcmdir`` / ``exedir`` are omitted
        (present as ``None``) for caches that did not run. Top-level
        ``total_bytes_freed`` is always present (even when only a single
        cache ran) and equals the sum of ``bytes_freed`` across all caches
        that ran. Note: ``print_summary`` only prints a "Total space freed"
        line when two or more caches ran; ``summary_json`` always includes
        ``total_bytes_freed`` because machine consumers benefit from a
        stable key regardless of how many caches were trimmed.
        """
        total_bytes_freed = 0
        result: dict = {
            "schema": 1,
            "mode": "trim",
            "objdir": None,
            "pchdir": None,
            "pcmdir": None,
            "exedir": None,
        }

        if objdir_stats is not None:
            result["objdir"] = dict(objdir_stats)
            total_bytes_freed += objdir_stats["bytes_freed"]

        if pchdir_stats is not None:
            result["pchdir"] = dict(pchdir_stats)
            total_bytes_freed += pchdir_stats["bytes_freed"]

        if pcmdir_stats is not None:
            result["pcmdir"] = dict(pcmdir_stats)
            total_bytes_freed += pcmdir_stats["bytes_freed"]

        if exedir_stats is not None:
            result["exedir"] = dict(exedir_stats)
            total_bytes_freed += exedir_stats["bytes_freed"]

        result["total_bytes_freed"] = total_bytes_freed
        return result


def _safe_locked_unlink(path, *, skip_if_nlink_above=None):
    """Unlink path after acquiring the build lock for it.

    Used by ct-trim-cache to avoid deleting an .o file that a concurrent
    build is currently writing. Returns True on success, False on failure.

    If the lock subsystem cannot acquire a lock (filesystem unsupported,
    permissions, etc.), this function REFUSES to delete the file —
    deleting unlocked would defeat the entire purpose of the wrapper
    and could clobber an in-flight write from a peer build. The caller
    sees False and reports the file as failed; a future trim run can
    retry once the underlying lock issue is resolved.

    ``skip_if_nlink_above`` (I4): if not None, re-stat the path under
    the lock and skip the unlink if ``st_nlink`` exceeds the threshold.
    Closes a TOCTOU window where a peer publish-as-hardlink lands
    between the trim's initial scan (nlink read=1) and this per-entry
    unlink (nlink would now be 2, but the in-memory snapshot is stale).
    Without this re-check, we'd evict an entry that just gained a
    published reference and force a relink on the next build. Returns
    False (entry still considered live) when the threshold trips.
    """
    from types import SimpleNamespace

    from compiletools.locking import FileLock

    lock_args = SimpleNamespace(
        file_locking=True,
        verbose=0,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.1,
        sleep_interval_flock_fallback=0.1,
    )
    try:
        with FileLock(path, lock_args):
            if skip_if_nlink_above is not None:
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    return True  # peer already removed
                except OSError:
                    return False
                if st.st_nlink > skip_if_nlink_above:
                    # I4: peer publish elevated nlink mid-trim. Treat as
                    # still-in-use; do not unlink.
                    return False
            try:
                os.remove(path)
                return True
            except FileNotFoundError:
                return True  # peer already removed
            except OSError:
                return False
    except OSError as exc:
        # Lock acquisition failed — refuse to delete unlocked. Deleting
        # without a lock could clobber a peer build that is mid-write.
        print(
            f"  Refusing to remove {path}: lock unavailable ({exc})",
            file=sys.stderr,
        )
        return False


def _safe_locked_rmtree(dir_path):
    """Remove dir_path after acquiring a build lock on each contained file.

    Used by ct-trim-cache to avoid deleting files that a concurrent build
    is currently writing. Returns True on success, False on failure.

    Lock-unavailable safety: if any file's lock cannot be acquired, this
    function REFUSES to remove the directory and returns False. Deleting
    unlocked would defeat the wrapper's purpose and risk clobbering a
    peer build's in-flight write.

    TOCTOU safety: after locks are acquired, the directory is re-scanned.
    If any new file appeared between the initial scan and lock window
    (a peer build creating a fresh .gch), we abort the removal so the
    new file is not deleted unlocked. Caller sees False and the dir is
    naturally retried on the next trim pass.
    """
    from types import SimpleNamespace

    lock_args = SimpleNamespace(
        file_locking=True,
        verbose=0,
        lock_cross_host_timeout=300,
        lock_warn_interval=30,
        lock_creation_grace_period=2,
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.1,
        sleep_interval_flock_fallback=0.1,
    )

    # Lock-metadata sidecars (FlockLock/FcntlLock/CIFSLock create
    # ``<target>.lock`` and ``.lock.excl`` files alongside build artifacts).
    # They aren't build outputs, so we neither lock them (locking a .lock
    # file would create .lock.lock) nor count them as peer activity in the
    # TOCTOU re-scan — the .lock files we acquire below would otherwise
    # show up there as "new files" and abort the removal.
    def _is_build_artifact(name):
        return not (name.endswith(".lock") or name.endswith(".lock.excl"))

    files_to_lock = []
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file() and _is_build_artifact(entry.name):
                files_to_lock.append(entry.path)
    except OSError:
        # Dir vanished — nothing to do
        return True

    from compiletools.locking import FileLock

    def _release_quiet(fl):
        # Match the original best-effort release semantics: a stray OSError
        # on lock release during cleanup must not fail the trim (the rmtree
        # may already have succeeded).
        with contextlib.suppress(OSError):
            fl.__exit__(None, None, None)

    with contextlib.ExitStack() as stack:
        for path in files_to_lock:
            try:
                fl = FileLock(path, lock_args)
                fl.__enter__()
            except OSError as exc:
                # Lock acquisition failed — refuse to delete unlocked.
                # Deleting without a lock could clobber a peer build
                # that is mid-write to one of these files.
                print(
                    f"  Refusing to remove {dir_path}: lock unavailable for {path} ({exc})",
                    file=sys.stderr,
                )
                return False
            stack.callback(_release_quiet, fl)

        # Re-scan inside the lock window to catch files that appeared
        # between the initial scan and lock acquisition. A peer build
        # creating a fresh .gch in this dir is unlocked from our side,
        # so removing it would be unsafe — abort instead.
        try:
            current_files = {
                entry.path for entry in os.scandir(dir_path) if entry.is_file() and _is_build_artifact(entry.name)
            }
        except OSError:
            # Dir vanished between scan and re-scan; nothing more to do
            return True

        locked_set = set(files_to_lock)
        new_files = current_files - locked_set
        if new_files:
            print(
                f"  Refusing to remove {dir_path}: {len(new_files)} new file(s) "
                f"appeared after scan (peer build active)",
                file=sys.stderr,
            )
            return False

        try:
            shutil.rmtree(dir_path)
            return True
        except OSError:
            return False


def warn_if_suspicious_cas_dir(path, kind, variant, *, verbose, stream=None):
    """Warn when a CAS directory is missing or has no entries in a suspicious way.

    Fires at ``verbose >= 0`` (i.e. by default), written to *stream* (stderr
    by default) so it never pollutes ``--json`` stdout.

    Logic:
    - ``verbose < 0``: return silently (quiet mode).
    - Path is not an existing directory: print a warning naming *variant*.  If
      the parent directory contains sibling subdirectories whose names differ
      from the missing dir's basename, append a hint listing a few of them and
      naming *variant* to suggest the user may have meant a different
      ``--variant`` or bare pool path.
    - Path is an existing directory (caller guarantees scanned-count is 0) AND
      the parent has sibling variant subdirectories: print a "did you mean"
      warning naming *variant*.
    - Path exists, no siblings: stay silent (a legitimately empty cache must
      not generate noise).

    Args:
        path: The variant-suffixed CAS directory path (absolute).
        kind: Human-readable label for the cache type (e.g. ``"objdir"``).
        variant: The effective variant string, interpolated into warning text.
        verbose: Current verbosity level.
        stream: Output stream for warnings (default: ``sys.stderr`` resolved at
            call time, not at import time, so pytest capsys patching works).
    """
    if stream is None:
        stream = sys.stderr
    if verbose < 0:
        return

    parent = os.path.dirname(path)
    basename = os.path.basename(path)

    def _sibling_names():
        """Return names of sibling subdirectories in parent (up to 5)."""
        try:
            return sorted(e.name for e in os.scandir(parent) if e.is_dir() and e.name != basename)[:5]
        except OSError:
            return []

    # NOT wrappedos / NOT cached: post-build diagnostic existence check. This
    # runs after the trim pass to explain a zero-scan result; the cas dir's
    # existence may differ from any earlier cached answer, so use the uncached
    # os.path.isdir directly (same documented skip case as the trim_* methods).
    if not os.path.isdir(path):
        print(f"warning: {kind} cache dir not found: {path} (variant '{variant}')", file=stream)
        siblings = _sibling_names()
        if siblings:
            sib_str = ", ".join(siblings)
            print(
                f"  sibling variant dirs present ({sib_str}); '{variant}' may be the wrong"
                f" --variant for this pool, or pass a bare pool path",
                file=stream,
            )
    else:
        # Caller guarantees scanned-count == 0 when this is invoked.
        siblings = _sibling_names()
        if siblings:
            sib_str = ", ".join(siblings)
            print(
                f"warning: {kind} cache dir {path} has no entries to trim but sibling variant "
                f"dirs exist ({sib_str}); '{variant}' may be the wrong --variant",
                file=stream,
            )


def warn_if_wrong_checkout(objdir, objdir_stats, max_age, *, verbose, stream=None):
    """Warn when a no-age-limit object trim of a network pool found zero current objects.

    Zero current objects across a non-empty scan, without ``--max-age``, on a
    network filesystem is a strong signal that the trim was invoked from the
    wrong checkout (or against a foreign shared pool). Object currency is
    checkout-relative: ``build_current_hash_set`` reads *this* checkout's git
    HEAD, so objects built from another branch or by another user will all look
    non-current here and will be evicted down to ``--keep-count`` per basename.

    On a shared, multi-branch or multi-user pool, prefer ``--max-age`` as the
    primary eviction control — it keeps objects regardless of which checkout
    considers them current, removing only entries that have not been rebuilt
    recently enough.

    Fires at ``verbose >= 0`` (default), written to *stream* (``sys.stderr`` by
    default) so it never pollutes ``--json`` stdout.

    Condition (all must hold):
    - ``max_age is None`` — no age-limit guard already in effect.
    - ``objdir_stats["total_scanned"] > 0`` — the scan was non-empty (a
      legitimately empty pool is already handled by
      ``warn_if_suspicious_cas_dir``).
    - ``objdir_stats["current_kept"] == 0`` — not a single object is current
      for this checkout.
    - ``objdir`` is on a network/cluster filesystem (``should_parallelize_scan``
      returns ``True`` for the detected filesystem type).

    Args:
        objdir: Path to the object CAS directory that was scanned.
        objdir_stats: Stats dict returned by ``CacheTrimmer.trim_objdir``.
        max_age: Value of ``args.max_age`` (``None`` when ``--max-age`` was not
            supplied).
        verbose: Current verbosity level.
        stream: Output stream for the warning (default: ``sys.stderr`` resolved
            at call time, so pytest ``capsys`` patching works).
    """
    if stream is None:
        stream = sys.stderr
    if verbose < 0:
        return
    if max_age is not None:
        return
    if objdir_stats["total_scanned"] == 0:
        return
    if objdir_stats["current_kept"] > 0:
        return
    fstype = compiletools.filesystem_utils.get_filesystem_type(objdir)
    if not compiletools.filesystem_utils.should_parallelize_scan(fstype):
        return
    print(
        f"warning: object trim of {objdir} found 0 current objects across "
        f"{objdir_stats['total_scanned']} scanned — object currency is relative "
        f"to the invoking checkout's git HEAD. On a shared multi-branch pool this "
        f"can over-evict objects built by other checkouts. Consider using "
        f"--max-age to limit eviction by age instead of by currency.",
        file=stream,
    )


# ----------------------------------------------------------------------------
# Unresolvable-cell discovery (read-only orphan finder; --list-unresolvable)
# ----------------------------------------------------------------------------
#
# A CAS is laid out ``<pool>/<variant>/<inner>``. ``resolve_cas_directory_
# arguments`` appends ``/<variant>`` to each ``--cas-*dir``, so the resolved
# path ends in ``/<variant>`` and a cache "cell" is one ``<pool>/<variant>/``
# directory. When a variant's axis conf is removed from the checkout, its cell
# becomes UNREACHABLE by the normal (variant-resolving) trim path — those bytes
# can never be reclaimed by the variant-driven trimmer.
#
# SAFETY: "unresolvable from this checkout" is NOT a durable orphan signal on a
# shared pool — a cell unresolvable here may be another checkout's or branch's
# LIVE cache. This discovery path is strictly READ-ONLY; it exists so an
# operator can SEE candidates (with age, so a dead variant can be told apart
# from someone else's live one). The destructive purge is a separate, later
# concern. The classification here must stay conservative: a child that is not
# a recognisable cell of the requested kind is labelled UNKNOWN and is never a
# purge candidate.


def _active_cache_sections(args):
    """Return the list of ``(section_key, kind, cas_dir, active)`` tuples for
    the two pool-level modes (``list_unresolvable_cells`` and
    ``purge_unresolvable_cells``).

    Each cache runs unless any OTHER ``--cas-*-only`` flag is set. This is the
    single source of truth for pool-mode cache selection; both functions
    delegate here so they can never drift from each other.

    ``section_key`` is one of ``"objdir"`` / ``"pchdir"`` / ``"pcmdir"`` /
    ``"exedir"``.  ``kind`` is the matching ``enumerate_cells`` kind string
    (``"obj"`` / ``"pch"`` / ``"pcm"`` / ``"exe"``).  ``cas_dir`` is the
    resolved CAS directory (may be ``None`` when not configured).  ``active``
    is ``True`` when this cache should run under the current ``--cas-*-only``
    scope.
    """
    objdir_only = getattr(args, "cas_objdir_only", False)
    pchdir_only = getattr(args, "cas_pchdir_only", False)
    pcmdir_only = getattr(args, "cas_pcmdir_only", False)
    exedir_only = getattr(args, "cas_exedir_only", False)
    return [
        ("objdir", "obj", getattr(args, "cas_objdir", None), not (pchdir_only or pcmdir_only or exedir_only)),
        ("pchdir", "pch", getattr(args, "cas_pchdir", None), not (objdir_only or pcmdir_only or exedir_only)),
        ("pcmdir", "pcm", getattr(args, "cas_pcmdir", None), not (objdir_only or pchdir_only or exedir_only)),
        ("exedir", "exe", getattr(args, "cas_exedir", None), not (objdir_only or pchdir_only or pcmdir_only)),
    ]


# Known non-cell child names that may legitimately sit beside variant cells in
# a pool root. Skipped during cell enumeration.
_NON_CELL_POOL_CHILDREN: frozenset[str] = frozenset({"TraceStore", "diagnostics"})

# Cell classification labels.
_CELL_RESOLVABLE = "RESOLVABLE"
_CELL_UNRESOLVABLE = "UNRESOLVABLE"
_CELL_UNKNOWN = "UNKNOWN"


def cell_pool_root(resolved_cas_dir, variant):
    """Return the trusted pool root for a variant-suffixed cas directory.

    A resolved ``--cas-*dir`` is ``<pool>/<variant>``; the pool root is its
    parent. We only trust that climb when the resolved path's basename is
    EXACTLY ``variant`` — proof that ``resolve_cas_directory_arguments`` really
    appended the ``/<variant>`` suffix and that ``os.path.dirname`` therefore
    lands on the pool and not above it.

    Two cases break that assumption and are refused with ``ValueError`` rather
    than risking a walk above the pool:

    * **empty / falsy ``variant``** — there is no suffix to have been appended.
    * **``basename != variant``** — this is the ``_ensure_variant_suffix``
      no-op case: the user pointed ``--cas-*dir`` at a bare pool path whose
      basename already equalled the variant, so the suffix was never appended
      and the given path IS the cell, not ``<pool>/<variant>``. Climbing one
      level would land above the pool.

    Args:
        resolved_cas_dir: A post-``resolve_cas_directory_arguments`` cas dir.
        variant: The effective ``args.variant``.

    Returns:
        The pool-root path (``os.path.dirname`` of the cas dir).

    Raises:
        ValueError: when the path's shape cannot be trusted for a pool walk.
    """
    if not variant:
        raise ValueError(
            f"cannot derive a trusted pool root from {resolved_cas_dir!r}: "
            "variant is empty, so no '/<variant>' suffix is present to climb above"
        )
    normalised = resolved_cas_dir.rstrip(os.sep) or resolved_cas_dir
    if os.path.basename(normalised) != variant:
        raise ValueError(
            f"cannot derive a trusted pool root from {resolved_cas_dir!r}: its basename "
            f"is not the variant {variant!r} (the cas dir was given as a bare pool path, "
            "or already pointed at a cell) — refusing to climb above the pool"
        )
    return os.path.dirname(normalised)


def _cell_size_and_newest_mtime(cell_path):
    """Return ``(total_bytes, newest_mtime)`` for everything under ``cell_path``.

    Single ``os.walk``. ``total_bytes`` sums every file's size; ``newest_mtime``
    is the max file mtime (``None`` when the cell contains no files). Files that
    vanish or become unreadable mid-walk are skipped (best-effort), consistent
    with the rest of the trimmer's scan semantics.
    """
    total = 0
    newest = None
    for dirpath, _dirs, files in os.walk(cell_path):
        for name in files:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue  # vanished/unreadable mid-walk — best-effort
            total += st.st_size
            if newest is None or st.st_mtime > newest:
                newest = st.st_mtime
    return total, newest


def _has_immediate_subdir_matching(cell_path, regex):
    """True if ``cell_path`` has at least one immediate subdir whose name
    matches ``regex``. Best-effort: an unreadable cell returns False."""
    try:
        with os.scandir(cell_path) as it:
            for entry in it:
                if regex.match(entry.name) and entry.is_dir(follow_symlinks=False):
                    return True
    except OSError:
        pass
    return False


def _exe_cell_shape_ok(cell_path):
    """True if ``cell_path`` looks like a cas-exedir cell.

    Stricter than the obj/pch/pcm bucket-name check: an exe cell must have at
    least one 2-hex bucket that CONTAINS a file ending in one of
    ``_CAS_EXE_SUFFIXES``. A bare 2-hex bucket of non-artefact files is NOT a
    cell — this keeps a stray top-level bucket (or a mislabelled dir) from
    being mistaken for an exe cell. Best-effort on unreadable dirs.
    """
    try:
        with os.scandir(cell_path) as it:
            buckets = [e.path for e in it if _OBJ_BUCKET_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return False
    for bucket in buckets:
        try:
            with os.scandir(bucket) as inner:
                for leaf in inner:
                    if leaf.name.endswith(_CAS_EXE_SUFFIXES) and leaf.is_file():
                        return True
        except OSError:
            continue
    return False


# Per-kind cell-shape predicate. Each answers "does this child look like a real
# cell of THIS kind?" — the obj/exe kinds key off 2-hex buckets, pch/pcm off
# 16-hex command-hash dirs (exe additionally requires an artefact inside).
_CELL_SHAPE_PREDICATES = {
    "obj": lambda p: _has_immediate_subdir_matching(p, _OBJ_BUCKET_RE),
    "pch": lambda p: _has_immediate_subdir_matching(p, _PCH_COMMAND_HASH_RE),
    "pcm": lambda p: _has_immediate_subdir_matching(p, _PCM_COMMAND_HASH_RE),
    "exe": _exe_cell_shape_ok,
}


def _variant_resolvable(name):
    """True if ``name`` resolves against the checkout's conf hierarchy.

    Uses ``configutils.resolve_variant(name)`` with ``argv=None`` so the
    classification reads the conf files directly (no BuildContext / git state
    needed). Catches ONLY ``VariantResolutionError`` as "unresolvable"; any
    other exception is deliberately allowed to propagate — a cell must never be
    silently misclassified because of an unexpected error.
    """
    import compiletools.configutils

    try:
        compiletools.configutils.resolve_variant(name)
        return True
    except compiletools.configutils.VariantResolutionError:
        return False


def enumerate_cells(pool, kind):
    """Enumerate and classify candidate cells under a pool root.

    Scans the pool's IMMEDIATE children. Conservatively skips children that are
    not cells:

    * non-directories;
    * dotfiles (names starting with ``.``);
    * known non-cell names (``TraceStore``, ``diagnostics``);
    * a 2-hex name (``_OBJ_BUCKET_RE``) — a 2-hex direct child of the POOL is a
      stray top-level bucket, NOT a cell (critical: prevents treating a stray
      bucket as an orphan cell).

    Each remaining child is classified:

    * ``resolvable`` — via ``resolve_variant(child_name)`` (only
      ``VariantResolutionError`` counts as unresolvable).
    * ``cell_shape_ok`` — KIND-SPECIFIC: the child must look like a real cell
      of ``kind`` (obj/pch/pcm: a matching command-hash/bucket subdir; exe: a
      2-hex bucket containing a CAS exe artefact).
    * ``total_bytes`` / ``newest_mtime`` — one ``os.walk`` per cell.

    Derived ``label``:

    * ``RESOLVABLE``   — resolvable (regardless of shape);
    * ``UNRESOLVABLE`` — not resolvable AND cell_shape_ok (a real, orphaned
      cell of this kind — the only purge-candidate class);
    * ``UNKNOWN``      — not resolvable AND NOT cell_shape_ok (reported for
      visibility but NEVER a purge candidate).

    Args:
        pool: Pool-root path (from ``cell_pool_root``).
        kind: One of ``"obj"``, ``"pch"``, ``"pcm"``, ``"exe"``.

    Returns:
        A list of per-cell record dicts with keys ``name``, ``path``,
        ``resolvable``, ``cell_shape_ok``, ``total_bytes``, ``newest_mtime``,
        ``label``.
    """
    shape_ok = _CELL_SHAPE_PREDICATES[kind]  # KeyError on unknown kind is intentional
    records = []
    try:
        with os.scandir(pool) as _it:
            children = sorted(_it, key=lambda e: e.name)
    except OSError:
        return records

    for child in children:
        name = child.name
        if not child.is_dir(follow_symlinks=False):
            continue
        if name.startswith("."):
            continue
        if name in _NON_CELL_POOL_CHILDREN:
            continue
        # A 2-hex direct child of the POOL is a stray top-level bucket, never a
        # cell — variant names are never 2 hex chars.
        if _OBJ_BUCKET_RE.match(name):
            continue

        resolvable = _variant_resolvable(name)
        cell_shape_ok = bool(shape_ok(child.path))
        total_bytes, newest_mtime = _cell_size_and_newest_mtime(child.path)

        if resolvable:
            label = _CELL_RESOLVABLE
        elif cell_shape_ok:
            label = _CELL_UNRESOLVABLE
        else:
            label = _CELL_UNKNOWN

        records.append(
            {
                "name": name,
                "path": child.path,
                "resolvable": resolvable,
                "cell_shape_ok": cell_shape_ok,
                "total_bytes": total_bytes,
                "newest_mtime": newest_mtime,
                "label": label,
            }
        )
    return records


def list_unresolvable_cells(args, stream=None):
    """Run the read-only ``--list-unresolvable`` discovery across the caches.

    For each active cache (honouring the ``--cas-*-only`` selection, same as the
    trim path), derive its ``(pool, kind)`` via ``cell_pool_root`` and
    ``enumerate_cells`` it. Identical ``(pool, kind)`` pairs are enumerated once
    and the same record list is reused. A cache whose pool root cannot be
    trusted (``cell_pool_root`` raises ``ValueError``) emits a diagnostic to
    *stream* (stderr by default) and is skipped — the listing continues across
    the other caches rather than aborting the whole run.

    This function NEVER mutates the filesystem.

    Args:
        args: Parsed args namespace (needs ``cas_objdir`` / ``cas_pchdir`` /
            ``cas_pcmdir`` / ``cas_exedir``, ``variant``, and the four
            ``cas_*_only`` flags).
        stream: Diagnostic stream (default ``sys.stderr`` resolved at call time
            so pytest ``capsys`` patching works).

    Returns:
        A dict with keys ``objdir`` / ``pchdir`` / ``pcmdir`` / ``exedir``;
        each is ``None`` when that cache was not run, else
        ``{"pool": str, "cells": [<record>, ...]}``.
    """
    if stream is None:
        stream = sys.stderr

    caches = _active_cache_sections(args)

    variant = getattr(args, "variant", None)
    result: dict = {
        "schema": 1,
        "mode": "list-unresolvable",
        "objdir": None,
        "pchdir": None,
        "pcmdir": None,
        "exedir": None,
    }
    enumerated: dict[tuple[str, str], list] = {}  # (pool, kind) -> records

    for section, kind, cas_dir, active in caches:
        if not active or not cas_dir:
            continue
        try:
            pool = cell_pool_root(cas_dir, variant)
        except ValueError as exc:
            print(f"warning: cannot list unresolvable cells for {section}: {exc}", file=stream)
            continue
        key = (pool, kind)
        if key not in enumerated:
            enumerated[key] = enumerate_cells(pool, kind)
        cells = enumerated[key]
        unresolvable_bytes = sum(c["total_bytes"] for c in cells if c["label"] == _CELL_UNRESOLVABLE)
        unknown_bytes = sum(c["total_bytes"] for c in cells if c["label"] == _CELL_UNKNOWN)
        result[section] = {
            "pool": pool,
            "cells": cells,
            "unresolvable_bytes": unresolvable_bytes,
            "unknown_bytes": unknown_bytes,
        }

    return result


def _purge_one_cell(cell_path, *, dry_run):
    """Leaf-level lock-safe removal of one purgeable cell.

    **NEVER ``_safe_locked_rmtree`` the cell root.** ``_safe_locked_rmtree``
    only locks the cell's TOP-LEVEL files before rmtree'ing the whole subtree;
    a cell root's immediate children are DIRS (2-hex buckets / 16-hex cmd-hash
    dirs), so it would lock NOTHING and rmtree the artefacts unlocked —
    clobbering a peer build mid-write. So we descend ONE level and dispatch each
    immediate child:

    * dir  → ``_safe_locked_rmtree(child)`` (locks the artefacts INSIDE the
      bucket, then rmtree's it);
    * file → ``_safe_locked_unlink(child)``.

    Then ``os.rmdir(cell_path)``. If a child could not be removed (peer holds a
    lock → the helper returned False) the cell stays non-empty and ``os.rmdir``
    raises ``OSError`` (ENOTEMPTY); we LEAVE the cell for the next run and report
    DEFERRED. ENOTEMPTY from a peer creating a fresh bucket mid-purge lands here
    too — same deferred outcome, never a hard failure.

    Returns one of ``"purged"`` / ``"deferred"``. In ``dry_run`` mode nothing is
    touched and ``"purged"`` is returned (the candidate is reported as a
    would-be purge).

    Note on ``bytes_freed`` lower bound: when a cell is only PARTIALLY removed
    (a locked artefact or a peer-created bucket leaves the cell non-empty →
    ENOTEMPTY → DEFERRED), the bytes freed for already-removed children are NOT
    counted in the caller's ``bytes_freed`` tally — the cell is retried next
    run. ``bytes_freed`` is therefore a LOWER BOUND on actual reclamation when
    any cells are deferred.
    """
    if dry_run:
        return "purged"

    try:
        children = list(os.scandir(cell_path))
    except OSError:
        # Cell vanished mid-purge (a peer reclaimed it) — nothing to do.
        return "purged"

    all_removed = True
    for child in children:
        if child.is_dir(follow_symlinks=False):
            if not _safe_locked_rmtree(child.path):
                all_removed = False
        else:
            if not _safe_locked_unlink(child.path):
                all_removed = False

    try:
        os.rmdir(cell_path)
    except OSError:
        # Non-empty (a child was left because its lock could not be acquired, or
        # a peer created a fresh bucket between our scan and rmdir) — defer to
        # the next run rather than hard-failing.
        return "deferred"
    return "purged" if all_removed else "deferred"


def purge_unresolvable_cells(args, stream=None):
    """Run the DESTRUCTIVE ``--purge-unresolvable`` reclamation across the caches.

    Pool-level standalone mode (mirrors ``list_unresolvable_cells`` for cache
    selection and ``(pool, kind)`` enumeration): for each active cache, climb to
    its pool root via ``cell_pool_root`` and ``enumerate_cells`` it, then purge
    every cell that is BOTH:

    * ``label == "UNRESOLVABLE"`` — a real, orphaned cell of this kind. RESOLVABLE
      and UNKNOWN cells are NEVER purge candidates; a stray 2-hex pool bucket and
      ``TraceStore/`` are skipped by ``enumerate_cells`` itself.
    * **COLD** — ``newest_mtime is None`` OR ``newest_mtime < (now - max_age)``.
      A WARM unresolvable cell (newest file within ``max_age``) is SPARED and
      reported as ``cells_skipped_warm`` — it is most likely another live
      checkout's cache, not a dead variant.

    Removal is strictly leaf-level (see ``_purge_one_cell``); ``--dry-run``
    reports candidates and removes nothing.

    Caller contract: ``main`` HARD-ERRORS before calling this when
    ``args.max_age is None or args.max_age <= 0`` — there is no safe age
    cutoff without a strictly positive value (zero would classify every cell
    as COLD and defeat the WARM-cache safety gate). This function assumes
    ``args.max_age`` is set and > 0 (the CLI enforces this).

    This function honours a single ``--cas-*-only`` flag as a SCOPE filter (same
    selection list as ``list_unresolvable_cells``); it never mutates a cache the
    scope excludes.

    Args:
        args: Parsed args namespace (needs ``cas_objdir`` / ``cas_pchdir`` /
            ``cas_pcmdir`` / ``cas_exedir``, ``variant``, ``max_age``,
            ``dry_run``, and the four ``cas_*_only`` flags).
        stream: Diagnostic stream (default ``sys.stderr`` resolved at call time
            so pytest ``capsys`` patching works).

    Returns:
        A dict with ``schema`` plus keys ``objdir`` / ``pchdir`` / ``pcmdir`` /
        ``exedir`` (each ``None`` when that cache was not run, else a per-cache
        stats dict with ``cells_purged`` / ``cells_skipped_warm`` /
        ``cells_deferred`` / ``bytes_freed`` / ``pool``) and a top-level
        ``total_bytes_freed``.
    """
    if stream is None:
        stream = sys.stderr

    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", 1)
    human = sys.stderr if getattr(args, "json", False) else sys.stdout
    max_age_days = getattr(args, "max_age", None)
    max_age_seconds = max_age_days * 86400 if max_age_days is not None else None

    caches = _active_cache_sections(args)

    variant = getattr(args, "variant", None)
    result: dict = {
        "schema": 1,
        "mode": "purge-unresolvable",
        "objdir": None,
        "pchdir": None,
        "pcmdir": None,
        "exedir": None,
    }
    enumerated: dict[tuple[str, str], list] = {}  # (pool, kind) -> records
    now = time.time()
    cutoff = now - max_age_seconds if max_age_seconds is not None else None
    total_bytes_freed = 0

    for section, kind, cas_dir, active in caches:
        if not active or not cas_dir:
            continue
        try:
            pool = cell_pool_root(cas_dir, variant)
        except ValueError as exc:
            print(f"warning: cannot purge unresolvable cells for {section}: {exc}", file=stream)
            continue
        key = (pool, kind)
        if key not in enumerated:
            enumerated[key] = enumerate_cells(pool, kind)
        cells = enumerated[key]

        stats = {
            "pool": pool,
            "cells_purged": 0,
            "cells_skipped_warm": 0,
            "cells_deferred": 0,
            "bytes_freed": 0,
        }

        for cell in cells:
            if cell["label"] != _CELL_UNRESOLVABLE:
                continue  # never purge RESOLVABLE / UNKNOWN
            newest = cell["newest_mtime"]
            is_cold = newest is None or (cutoff is not None and newest < cutoff)
            if not is_cold:
                stats["cells_skipped_warm"] += 1
                if verbose >= 1:
                    print(
                        f"  Skipping warm unresolvable cell (peer-owned?): {cell['path']}"
                        f" (age {_format_age_days(newest, now)})",
                        file=human,
                    )
                continue

            if verbose >= 1:
                action = "Would purge" if dry_run else "Purging"
                print(f"  {action}: {cell['path']} ({_format_size(cell['total_bytes'])})", file=human)

            outcome = _purge_one_cell(cell["path"], dry_run=dry_run)
            if outcome == "purged":
                stats["cells_purged"] += 1
                stats["bytes_freed"] += cell["total_bytes"]
            else:  # deferred
                # bytes_freed is a LOWER BOUND: partially-removed children
                # are not counted; the cell is retried on the next run.
                stats["cells_deferred"] += 1
                if verbose >= 1:
                    print(
                        f"  Deferred (peer build active or lock unavailable): {cell['path']}",
                        file=human,
                    )

        total_bytes_freed += stats["bytes_freed"]
        result[section] = stats

    result["total_bytes_freed"] = total_bytes_freed
    return result


def print_purge_report(result, *, stream=None):
    """Print a human-readable per-kind summary of the purge ``result``.

    Written to *stream* (stdout by default — the purge summary is the tool's
    primary output in ``--purge-unresolvable`` non-JSON mode).
    """
    if stream is None:
        stream = sys.stdout
    sections = (
        ("objdir", "Object CAS"),
        ("pchdir", "PCH CAS"),
        ("pcmdir", "PCM CAS"),
        ("exedir", "Executable CAS"),
    )
    print("=" * 60, file=stream)
    print("Unresolvable-cell purge complete", file=stream)
    for section, title in sections:
        info = result.get(section)
        if info is None:
            continue
        print(f"  {title} (pool: {info['pool']}):", file=stream)
        print(f"    Cells purged:       {info['cells_purged']}", file=stream)
        print(f"    Cells skipped warm: {info['cells_skipped_warm']}", file=stream)
        print(f"    Cells deferred:     {info['cells_deferred']}", file=stream)
        print(f"    Bytes freed:        {_format_size(info['bytes_freed'])} (lower bound)", file=stream)
    total_deferred = sum((result.get(s) or {}).get("cells_deferred", 0) for s, _t in sections)
    if total_deferred:
        print(
            "  Note: deferred cells are retried on the next run; "
            "nonzero 'Cells deferred' with low 'Bytes freed' is expected under contention.",
            file=stream,
        )
    print(f"  Total space freed: {_format_size(result.get('total_bytes_freed', 0))} (lower bound)", file=stream)
    print("=" * 60, file=stream)


def _format_age_days(newest_mtime, now=None):
    """Format a cell's newest-file mtime as an age string in days.

    ``None`` (an empty cell with no files) renders as ``"-"``. Otherwise the
    age is ``(now - newest_mtime)`` in whole days, e.g. ``"12d"``.
    """
    if newest_mtime is None:
        return "-"
    if now is None:
        now = time.time()
    age_days = max(0, int((now - newest_mtime) // 86400))
    return f"{age_days}d"


def print_unresolvable_report(result, *, stream=None):
    """Print a human-readable per-kind table of the discovery ``result``.

    Written to *stream* (stdout by default — this report is the tool's primary
    output in ``--list-unresolvable`` mode). For each cell shows its label,
    name, human size, and age-in-days of the newest file (so an operator can
    tell a dead variant from someone else's live one).
    """
    if stream is None:
        stream = sys.stdout
    now = time.time()
    sections = (
        ("objdir", "Object CAS"),
        ("pchdir", "PCH CAS"),
        ("pcmdir", "PCM CAS"),
        ("exedir", "Executable CAS"),
    )
    for section, title in sections:
        info = result.get(section)
        if info is None:
            continue
        print(f"{title} (pool: {info['pool']}):", file=stream)
        cells = info["cells"]
        if not cells:
            print("  (no cells)", file=stream)
            continue
        for cell in cells:
            print(
                f"  {cell['label']:<12} {cell['name']}"
                f"  ({_format_size(cell['total_bytes'])}, age {_format_age_days(cell['newest_mtime'], now)})",
                file=stream,
            )


def _format_size(size_bytes):
    """Format a byte count as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
