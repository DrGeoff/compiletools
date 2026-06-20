================
ct-trim-cache
================

----------------------------------------------------------------------------------
Trim stale entries from the object, PCH, PCM, and linker-artefact CAS directories
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-06-11
:Version: 10.1.11
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-trim-cache [--dry-run] [--cas-objdir PATH] [--cas-pchdir PATH] [--cas-pcmdir PATH] [--cas-exedir PATH] [--max-age DAYS] [--keep-count N] [--max-size SIZE] [--all-variants] [--json] [-j N] [-v]

ct-trim-cache --list-resolvable [--json] [--cas-\*dir PATH] [-v]

ct-trim-cache --list-unresolvable [--json] [--cas-\*dir PATH] [-v]

ct-trim-cache --purge-unresolvable --max-age DAYS [--dry-run] [--json] [--cas-\*dir PATH] [-v]

DESCRIPTION
===========
Trim stale content-addressable entries from ``cas-objdir`` (compiled
object files), ``cas-pchdir`` (precompiled headers), ``cas-pcmdir``
(C++20 module BMIs), and ``cas-exedir`` (linker artefacts —
executables, static libraries, and shared libraries). The tool
identifies which entries still match the current git working tree
and removes the oldest non-current entries while preserving a
configurable safety margin.

Object-cache currency is **relative to the invoking checkout's git HEAD**:
an entry is "current" only if its embedded ``file_hash`` matches a blob the
*current* working tree tracks. On a shared, multi-branch, or multi-user pool
this means another checkout's entries look non-current here, so a naive run can
over-evict them — see `MULTI-USER SHARED CACHES`_ for why ``--max-age`` is the
right primary control there, and `ORPHANED-VARIANT CELLS`_ for reclaiming whole
cells whose variant no longer exists in this checkout.

Object files use the naming scheme
``{basename}_{file_hash}_{dep_hash}_{macro_state_hash}.o``.  The embedded
``file_hash`` is compared against current git blob SHA1s to decide whether an
entry is still current.

PCH directories use the scheme ``{command_hash}/{header}.gch``.  Each
``{command_hash}/`` represents one unique compile configuration (compiler
+ flags + header realpath) and writes a sidecar ``manifest.json`` recording
the immediate header's realpath plus content hashes for every transitive
header.  The trim policy uses the manifest to:

- **Bucket entries by header realpath**, so ``--keep-count`` is enforced
  per real header rather than globally.  Cross-variant builds of the same
  header (e.g. ``gcc.debug`` and ``gcc.release``) coexist instead of
  evicting each other at the default ``keep_count=1``.
- **Pre-evict entries whose transitive headers have changed** since the
  ``.gch`` was built, so users do not pay the slow ``cc1`` PCH-stamp
  rejection at consume time.

PCM directories use the same shape as PCH:
``{command_hash}/{name}.{pcm,gcm}`` plus a sidecar ``manifest.json``.
Each ``{command_hash}/`` is one unique compile configuration. The
manifest records ``bucket_key`` (the source realpath for named modules,
or the verbatim ``<vector>`` / ``"foo.h"`` token for header units),
``stage`` (``clang_module_interface`` / ``gcc_module_interface`` /
``clang_header_unit`` / ``gcc_header_unit``), and ``transitive_hashes``
for the same staleness pre-eviction the PCH path uses. Bucketing by
``bucket_key`` ensures cross-variant or cross-project builds with the
same module name (or the same imported system header) don't evict each
other.

Legacy entries without a manifest fall back to the previous global
ranking by mtime, keeping the rollout backwards-compatible.  If
``--max-age`` is set, anything within the cutoff is kept regardless of
bucket.

The linker-artefact cache ``cas-exedir`` uses a flatter scheme:
``<cas-exedir>/<key[:2]>/<basename>_<key>.<ext>`` with ``<ext>`` ∈
``{.exe, .a, .so}``.  No per-entry sidecar manifest is required by
default — the cache key itself is the identity — but ``ct-cas-publish``
writes a small ``<cas-path>.manifest`` (``{"source_realpath": ...}``)
at publish time so trim can bucket by source identity rather than
basename.  Trim policy:

* **Bucket** by ``(source_realpath, suffix)`` from the manifest, with
  fall-back to ``(basename, suffix)`` for legacy entries that pre-date
  the sidecar.  ``libfoo.a`` and ``libfoo.so`` bucket separately
  because the suffix differs.  The newest ``--keep-count`` per bucket
  survive bucket-rank eviction.
* **Hard-link safety:** anything with ``st_nlink > 1`` is preserved,
  on the assumption that a published ``bin/<variant>/<name>`` (or
  ``bin/<variant>/lib<name>.{a,so}``) is still pointing at it.
  Symlinked-fallback bin paths show ``st_nlink == 1`` on the cas
  entry and are NOT protected.
* **Lock-aware delete:** trim acquires the same ``<path>.lock`` sidecar
  the producer rule uses, with a re-stat of ``nlink`` under the lock
  to close the scan-to-unlink TOCTOU window (a peer publish that
  elevates ``nlink`` mid-trim aborts the unlink).

Why does PCM use a single ``command_hash`` like PCH instead of the
object cache's three-component path? **In-band BMI verification.** Both
GCC and clang record the compile environment inside the BMI itself and
verify it at consume time, rejecting any mismatch. A hypothetical 64-bit
``command_hash`` collision therefore causes a slow re-precompile, not a
silent miscompile. Object files have no such safety net (the linker
links whatever bytes it gets), so they need the additional path entropy
of three independent hashes (168 bits total) to make collisions
statistically impossible. PCH and PCM rely on the compiler's
verification and use the simpler single-hash + manifest design. See the
"C++20 Modules Caching" section of ``ct-cake`` for the full rationale.

Concurrent builds
-----------------
``ct-trim-cache`` takes the same per-target lock that ``atomic_compile``
takes before unlinking, so it will block (and not delete) a file an
in-flight build is currently writing. On filesystems where locking is
unavailable, the trim falls through to a plain unlink. Even so, prefer
running ``ct-trim-cache`` in a maintenance window; concurrent runs work
but slow active builds while the trim holds locks.

Whole-pool sweep (--all-variants)
---------------------------------
By default ``ct-trim-cache`` trims only the single cell named by the active
``--variant`` (``<pool>/<variant>/``). ``--all-variants`` instead trims **every
RESOLVABLE cell** in the pool — the same set ``--list-resolvable`` prints — in one
invocation. Cells are trimmed sequentially; a failure in one cell is isolated
(reported but not fatal, so the remaining cells still run), while intra-cell
``-j`` parallelism is preserved. It honours ``--dry-run``, ``--max-age``,
``--max-size``, ``--keep-count``, and either a single ``--cas-*-only`` scope flag
or one or more ``--cas-*-skip`` deselect flags, and is
mutually exclusive with the three pool modes (``--list-resolvable`` /
``--list-unresolvable`` / ``--purge-unresolvable``). With ``--json`` it emits one
aggregate object (``mode: all-variants``); see `MACHINE-READABLE OUTPUT`_. On a
shared multi-user or multi-branch pool, prefer ``--max-age`` as the primary
eviction control — without it, objects from other checkouts appear non-current and
are evicted down to ``--keep-count`` per basename (see `MULTI-USER SHARED
CACHES`_).

WHEN TO USE
===========
- Shared cache growing too large or approaching disk quota
- Periodic maintenance of shared build caches (cron)
- After major branch switches that invalidate many cache entries
- After refactoring that renames or removes many source files

**No built-in scheduler (A8).** ``ct-trim-cache`` has no daemon mode or
internal timer — it is a run-once tool.  Periodic cache bounding is the
job of cron (or a cluster scheduler), using ``--max-age`` to bound
retention by recency and (now) ``--max-size`` to cap the absolute pool
size.  See the cron EXAMPLES_ below for a combined invocation.

USAGE
=====
::

    # Preview what would be removed
    ct-trim-cache --dry-run

    # Trim both caches using defaults (keep 1 non-current per basename)
    ct-trim-cache

    # More aggressive: remove non-current entries older than 7 days
    ct-trim-cache --max-age 7

    # Keep 3 recent non-current entries per basename
    ct-trim-cache --keep-count 3

    # Only trim object files, skip PCH
    ct-trim-cache --cas-objdir-only

    # Only trim PCH cache, skip objects
    ct-trim-cache --cas-pchdir-only

    # Only trim PCM (C++20 module BMI) cache, skip objects and PCH
    ct-trim-cache --cas-pcmdir-only

    # Only trim the linker-artefact cache (executables, .a, .so)
    ct-trim-cache --cas-exedir-only

    # Sweep obj/pch/pcm, deselecting the write-once exe pool, then sweep exe
    # once on its own. Without --cas-exedir-skip the first sweep would also
    # stat-walk exe (freeing ~nothing at keep-count 1), doubling the dominant
    # GPFS stat-walk cost on the largest pool.
    ct-trim-cache --all-variants --max-age 14 --cas-exedir-skip
    ct-trim-cache --all-variants --cas-exedir-only --keep-count 0 --max-age 2

    # Custom directories
    ct-trim-cache --cas-objdir=/shared/build/objects --cas-pchdir=/shared/build/pch \
                  --cas-pcmdir=/shared/build/pcm --cas-exedir=/shared/build/exe

    # Machine-readable summary (JSON to stdout, all human text to stderr)
    ct-trim-cache --max-age 14 --json

    # Trim every resolvable cell in the pool, not just the active --variant cell
    ct-trim-cache --all-variants --max-age 14

    # List active (resolvable, canonical) cells -- read-only, bare names to stdout
    ct-trim-cache --list-resolvable

    # List orphaned cells (variants that no longer resolve here) -- read-only
    ct-trim-cache --list-unresolvable

    # Reclaim orphaned cells that are also cold (untouched for >= 30 days)
    ct-trim-cache --purge-unresolvable --max-age 30 --dry-run   # preview first
    ct-trim-cache --purge-unresolvable --max-age 30             # then for real

HOW IT WORKS
============

Object directory trimming
-------------------------
1. Loads current file hashes from the git repository via the global hash
   registry (same hashes used during compilation).
2. Scans the object directory for ``.o`` files matching the content-addressable
   naming pattern.  Skips ``.lockdir`` entries (managed by ``ct-cleanup-locks``).
3. Groups object files by source basename.
4. For each basename:

   - **Current files** (``file_hash`` matches a current git SHA1) are always kept.
   - **Non-current files** are sorted by modification time (newest first).
     The newest ``--keep-count`` are kept; the rest are candidates for removal.
   - If ``--max-age`` is set, only candidates older than that limit are removed.
   - **Safety invariant**: at least one file per basename is always preserved,
     even if everything appears non-current (e.g. after a branch switch).

5. Removes (or reports in ``--dry-run`` mode) the selected files.

PCH directory trimming
----------------------
1. Scans the PCH directory for subdirectories matching the 16-character
   hex command-hash pattern.
2. Reads each directory's sidecar ``manifest.json`` and groups
   directories into buckets keyed by ``header_realpath``.  Entries
   without a manifest (legacy cache contents) fall into a single
   shared bucket and use the previous global-ranking semantics.
3. Within each bucket, sorts directories by modification time and keeps
   the newest ``--keep-count``.  ``--max-age`` keeps anything younger
   than the cutoff regardless of bucket.
4. **Pre-evicts** any otherwise-kept directory whose manifest records a
   transitive header whose current git-blob SHA1 no longer matches the
   value stored when the ``.gch`` was built.  Best-effort: missing or
   unreadable headers leave the entry alone.
5. Removes (or reports in ``--dry-run`` mode) every cmd_hash directory
   not in the kept set.

Linker-artefact directory trimming
----------------------------------
1. Scans ``cas-exedir`` for files matching
   ``<key[:2]>/<basename>_<key>.{exe,a,so}``.  Anything else (including
   ``*.lock`` / ``*.lock.excl`` sidecars and orphaned ``.publish.tmp``
   files) is silently skipped.
2. For each matched file, reads the optional sidecar
   ``<path>.manifest`` and groups files into buckets keyed by
   ``(source_realpath, suffix)``.  Entries without a manifest fall
   back to ``(basename, suffix)`` bucketing.
3. Within each bucket, sorts files by modification time and keeps
   the newest ``--keep-count``.  ``--max-age`` keeps anything younger
   than the cutoff regardless of bucket position.
4. **Hard-link protection:** any file with ``st_nlink > 1`` is added
   to the keep set unconditionally.  This is what couples the cas
   entry to the user-facing ``bin/<variant>/<name>`` (or
   ``bin/<variant>/lib<name>.{a,so}``) that ``ct-cas-publish`` linked
   into place.
5. Removes (or reports in ``--dry-run`` mode) every cas entry not
   in the keep set, taking ``<path>.lock`` first and re-stat'ing
   ``nlink`` under the lock to close the scan-to-unlink TOCTOU.
   The companion ``<path>.manifest`` is unlinked best-effort
   alongside the cas entry.

PCM directory trimming
----------------------
Identical algorithm to PCH trimming, with one bucketing twist:

1. Scans ``cas-pcmdir`` for subdirectories matching the 16-character
   hex command-hash pattern. Each directory holds a ``.pcm`` (clang) or
   ``.gcm`` (gcc) file plus a sidecar ``manifest.json``.
2. Reads each manifest and groups directories by ``bucket_key``: the
   source realpath for named-module entries, the verbatim ``<vector>``
   or ``"foo.h"`` token for header-unit entries. Stage marker
   (``clang_module_interface`` / ``gcc_module_interface`` /
   ``clang_header_unit`` / ``gcc_header_unit``) prevents same-named
   modules and header units from sharing a bucket.
3. Within each bucket, applies the same ``--keep-count`` /
   ``--max-age`` policy as the PCH path.
4. **Pre-evicts** entries whose recorded transitive header content no
   longer matches the on-disk content. Same git-blob-SHA1 algorithm as
   the PCH check.
5. Removes (or reports in ``--dry-run`` mode) every cmd_hash directory
   not in the kept set.

Note on PCH/PCM/exe size control (A10)
---------------------------------------
Unlike the object cache, PCH, PCM, and exe cache entries have no separate
"current"-entry protection signal that is tied to git HEAD. Only object
files embed a ``file_hash`` that is compared against the invoking checkout's
tracked files to mark an entry as current and protect it unconditionally.
For PCH and PCM, ``--keep-count`` / ``--max-age`` / ``--max-size`` are the
primary size controls; transitive-staleness pre-eviction (described above)
provides early removal of stale entries but is not the same as currency
protection. For exe entries, hard-linked (``st_nlink > 1``) artefacts are
protected as "still published", but entries without a live hard link are
candidates for eviction by all three controls. Operators managing large
PCH/PCM/exe pools should set ``--max-age`` to bound retention by recency, and
``--max-size`` to cap absolute pool size; there is no mechanism to pin a
particular PCH or PCM entry as "current" the way object files are pinned.

Orphaned temp reclamation
--------------------------
After the normal keep/remove pass, each cache pool's orphaned producer
temp files are reclaimed.  Two patterns are matched:

* ``*.compiletools.tmp`` and ``*.compiletools.tmp.<pid>`` — temps left by
  PCH/PCM precompile rules in ``cas-pchdir`` / ``cas-pcmdir`` when a build
  is killed between the compiler write and the ``mv -f`` rename.
* ``*.publish.tmp`` — temps left by ``ct-cas-publish``'s atomic-replace
  writes.  Note: ``cas-publish`` writes these into the **published**
  ``bin/<variant>/`` directory, which ``ct-trim-cache`` does **not** scan.
  This matcher is therefore defensive — it reclaims nothing in production
  but would catch any future layout where a publish temp lands inside a CAS
  bucket directory.

**Safety properties:**

* **Age floor.** Only temps older than one day (86 400 s) are removed.
  No build invocation legitimately holds a temp open for a day, so no
  removal can race an in-flight write.
* **Lock-aware unlink.** ``_safe_locked_unlink`` acquires the same build
  lock the producer uses.  A temp that a peer has re-acquired mid-trim is
  left safely in place.
* **One-level descent.** The scan descends exactly one level into each
  immediate subdirectory (bucket or cmd_hash dir) and never recurses
  further, so it cannot accidentally walk into unrelated directory trees.

New JSON/summary stats: ``orphan_temps_removed``, ``orphan_temp_bytes_freed``
(per pool; also included in the pool's ``bytes_freed`` total).

Retry behaviour (A7)
---------------------
Every removal — from the normal trim pass, the orphan-temp reclaim, and
the budget pass — that fails to acquire the build lock on the first attempt
is **not immediately counted as a failure**.  Instead, it is queued and
retried exactly once after all four cache pools have been trimmed.  Only a
second failure increments ``failed``; entries left in place after a second
failure are intentional leaks, not hard errors (a peer build is likely
holding the lock and will clean up on its own exit).  The retry pass runs
*before* the summary, so reported counts already reflect the final
post-retry state.

Size budget pass (--max-size)
------------------------------
After the normal keep/remove pass and orphan-temp reclamation, if
``--max-size`` was supplied the tool re-scans each pool and evicts
non-protected entries **oldest-first** until the pool's total on-disk size
falls at or below the budget, or no more non-protected units remain.

**Per-pool semantics.** The budget is applied to each pool independently —
it is *not* an aggregate across all four caches.

**Peer safety.** A "protected" unit is never evicted, regardless of the
budget:

* ``obj`` pool — an object whose ``file_hash`` is in the current checkout's
  tracked-file set.
* ``exe`` pool — an artefact with ``st_nlink > 1`` (a published /
  hard-linked reference is still live).
* ``pch`` / ``pcm`` pools — no per-unit protection signal exists at this
  layer; all cmd_hash directories are eviction candidates (the compiler
  re-precompiles on the next build).

If protected entries alone exceed the budget the overflow is reported via
``budget_unmet_bytes`` and the pool is left over budget rather than
violating safety.

**Below ``--keep-count``.** The budget pass is the only control that may
evict non-current entries below ``--keep-count``.  That is deliberate: an
explicit size budget means those entries are rebuildable and space is the
binding constraint.

New JSON/summary stats: ``budget_removed``, ``budget_bytes_freed``,
``budget_unmet_bytes`` (per pool).

ORPHANED-VARIANT CELLS
======================
Each CAS is laid out ``<pool>/<variant>/<entries>``; one ``<pool>/<variant>/``
directory is a *cell*. The normal trim only reaches the cell whose variant
resolves from the current checkout's ``ct.conf.d`` hierarchy. Every cell is
classified into exactly one of four labels:

**RESOLVABLE**
    The variant name resolves here **and** is a canonicalization fixed point
    (already in canonical token order). These are the live cells the normal trim
    and ``--all-variants`` operate on.
**NON_CANONICAL**
    The variant resolves but the name is **not** a canonicalization fixed point —
    e.g. a doubled-token directory like ``gcc.gcc.debug.debug`` left by an older
    build. A current build always addresses the canonical name, so these cells are
    dead weight that the variant-driven trim never reaches.
**UNRESOLVABLE**
    The variant name no longer resolves against this checkout's conf hierarchy
    (e.g. an axis conf for a retired toolchain or product line was removed), but
    the directory is still structurally a real cache cell.
**UNKNOWN**
    Not resolvable and not a structurally valid cell — stray top-level buckets,
    ``TraceStore/``, and other non-cell directories. Never purged.

When an axis conf is removed, every cached cell for that variant becomes
**unreachable** by the variant-driven trim — its bytes can never be reclaimed by
the normal mode. Three pool-level modes address the non-RESOLVABLE cells:

``--list-resolvable`` (read-only)
    The complement of ``--list-unresolvable``: report only the **RESOLVABLE**
    cells (the active, canonical variants the normal trim operates on). Prints the
    bare sorted variant names to stdout (all human/progress text to stderr) so the
    output can be piped: ``ct-trim-cache --list-resolvable | while read v; do
    ...; done``. NON_CANONICAL / UNRESOLVABLE / UNKNOWN cells are excluded.
    Deletes nothing.

``--list-unresolvable`` (read-only)
    For each cache pool, report the immediate ``<variant>/`` cells whose variant
    name no longer resolves here, with each cell's size and the age of its newest
    file. Deletes nothing.

``--purge-unresolvable`` (destructive, requires ``--max-age``)
    Remove cells that are cold (newest file older than ``--max-age``) **and**
    either UNRESOLVABLE here **or** NON_CANONICAL. The NON_CANONICAL reclamation
    is what lets ``--purge-unresolvable`` clean up legacy doubled-token cells that
    a current build will never address again, under the same coldness guard.

**"Unresolvable here" is not, by itself, an orphan signal.** On a shared pool a
cell unresolvable from this checkout may be *another* checkout's or branch's live
cache (confs are git-tracked, so a variant valid on one branch is unresolvable
from another). The same caveat applies to **NON_CANONICAL**: the canonical token
order is itself checkout-overridable (CLI / ``CT_VARIANT_CANONICAL_ORDER`` env /
``variant-canonical-order`` in any ct.conf), so a peer checkout with a different
order legitimately writes cells that classify NON_CANONICAL *here* — "a current
build always addresses the canonical name" means a build from *this* checkout's
order. The purge therefore layers two guards:

* **Coldness gate (mandatory).** ``--purge-unresolvable`` requires
  ``--max-age > 0`` and SPARES any unresolvable **or** non-canonical cell whose
  newest file is within the cutoff — a warm cell is most likely a peer's live
  cache. ``--max-age`` values ``<= 0`` are rejected (a zero cutoff would defeat
  this guard).
* **Leaf-level lock safety.** Removal descends into each cell and takes the same
  per-artefact ``<path>.lock`` the producer rules use; it never recursively
  deletes a cell root unlocked. A cell whose artefacts a peer build is mid-write
  to (or that a peer repopulates during the sweep) is left intact and reported
  **deferred** to the next run, never hard-failed.

Only cells that are UNRESOLVABLE or NON_CANONICAL (and structurally a real cache
cell) are touched; ``UNKNOWN`` directories — stray top-level buckets,
``TraceStore/``, and other non-cell directories — are never purged. A single
``--cas-*-only`` flag scopes any of these modes to one cache. The three pool
modes (``--list-resolvable`` / ``--list-unresolvable`` / ``--purge-unresolvable``)
are mutually exclusive with each other and with ``--all-variants``. Run
``--list-unresolvable`` first; it is the safe way to see exactly what
``--purge-unresolvable`` would consider.

MACHINE-READABLE OUTPUT
=======================
``--json`` emits a single JSON object on **stdout** (all human/progress/warning
text is routed to **stderr**, so stdout stays pure JSON) with raw integer byte
counts and per-cache counts. Every payload carries a ``"schema": 1`` version
marker and a ``"mode"`` discriminator — ``"trim"`` for the normal trim,
``"all-variants"`` for the whole-pool sweep, and ``"list-resolvable"`` /
``"list-unresolvable"`` / ``"purge-unresolvable"`` for the pool modes — so a
consumer can tell the shapes apart. ``--json`` composes with any mode.

In ``"mode": "all-variants"`` output the object is an aggregate::

    {
      "schema": 1,
      "mode": "all-variants",
      "variants": [ { "variant": "<name>", "objdir": {...}, "pchdir": {...},
                     "pcmdir": {...}, "exedir": {...} }, ... ],
      "errors":   [ { "variant": "<name>", "error": "<message>" }, ... ]
    }

Each ``variants`` entry is one swept cell's per-pool ``"trim"`` stats (the same
per-pool dicts described below) tagged with its ``"variant"`` name; the entry's
own ``"schema"``/``"mode"`` keys are stripped so only the envelope carries them.
The ``errors`` list holds one record per cell whose trim raised an isolated
failure.

In ``"mode": "trim"`` output the per-pool dicts (``objdir``, ``pchdir``,
``pcmdir``, ``exedir``) include the following new keys (always present,
zero when the feature did not fire):

``orphan_temps_removed``
    Number of orphaned producer temp files removed from this pool.
``orphan_temp_bytes_freed``
    Bytes reclaimed by orphan-temp removal (subset of ``bytes_freed``).
``budget_removed``
    Number of units evicted by the ``--max-size`` budget pass.
``budget_bytes_freed``
    Bytes reclaimed by the budget pass (subset of ``bytes_freed``).
``budget_unmet_bytes``
    Bytes by which the pool still exceeds ``--max-size`` after the budget
    pass, because protected (current/hard-linked) entries cannot be evicted.
    Zero when the budget was met or ``--max-size`` was not supplied.

OPTIONS
=======

Trim Options
------------
``--dry-run``
    Show what would be removed without actually removing files.

``--max-age DAYS``
    Only remove non-current files older than this many days ("older" means
    "written more than N days ago" by mtime, not "not accessed in N days" —
    atime is unreliable on ``noatime`` mounts). Default: no age limit (removal
    controlled by ``--keep-count`` only). On a shared, multi-branch, or
    multi-user pool this is the **primary control**: because currency is
    checkout-relative, ``--max-age`` is what stops a run from one checkout
    over-evicting another's recent entries. It is **required** by
    ``--purge-unresolvable`` (which rejects values ``<= 0``).

``--keep-count N``
    Keep at least N non-current files per basename/header.
    Default: 1.  Set to 0 to remove all non-current entries (the safety
    invariant still preserves at least one file per basename).

``--max-size SIZE``
    Optional per-pool total size budget, applied independently to each cache
    pool (not an aggregate across all four pools).  After the normal
    keep/remove pass and orphan-temp reclamation, the oldest rebuildable
    (non-protected) entries are evicted until the pool fits within the
    budget.

    *Accepted forms* (1024-based binary; case-insensitive; trailing ``B``
    optional; decimals allowed):

    - Plain integer bytes: ``1073741824``
    - With suffix: ``10G``, ``512M``, ``500MB``, ``2g``, ``1.5T``
    - Suffixes: ``K`` (1024), ``M`` (1024²), ``G`` (1024³), ``T`` (1024⁴)

    *Peer safety.*  Current objects (``file_hash`` in the git-tracked set)
    and hard-linked (published) exe artefacts are **never** evicted.  If
    protected entries alone exceed the budget, the overflow is reported as
    ``budget_unmet_bytes`` and the pool is left over budget rather than
    violating safety.

    *Relationship to* ``--keep-count``.  This is the only control that may
    evict non-current entries below ``--keep-count``: an explicit budget
    means the entries are rebuildable and space is the binding constraint.
    Without ``--max-size`` the keep-count floor is unconditional.

    New JSON/summary stats: ``budget_removed``, ``budget_bytes_freed``,
    ``budget_unmet_bytes`` (present in each pool's stats dict under
    ``--json`` regardless of whether any budget eviction occurred).

    Default: no budget (only ``--keep-count`` / ``--max-age`` govern
    eviction).

``--cas-objdir-only``
    Only trim the object CAS, skip PCH, PCM, and linker-artefact trimming.

``--cas-pchdir-only``
    Only trim the PCH CAS, skip object, PCM, and linker-artefact trimming.

``--cas-pcmdir-only``
    Only trim the PCM CAS, skip object, PCH, and linker-artefact trimming.

``--cas-exedir-only``
    Only trim the linker-artefact CAS (executables, static libraries,
    shared libraries), skip object, PCH, and PCM trimming.

``--cas-objdir-skip`` / ``--cas-pchdir-skip`` / ``--cas-pcmdir-skip`` / ``--cas-exedir-skip``
    Deselect a single pool from the trim sweep, running every *other* pool —
    the inverse of the ``--cas-*-only`` flags. only/skip are opposite selection
    mechanisms and cannot be combined, and ``--cas-*-skip`` cannot deselect all
    four pools. The common use is ``--all-variants ... --cas-exedir-skip`` so the
    write-once exe pool is stat-walked only by a dedicated
    ``--cas-exedir-only --keep-count 0`` pass instead of twice per run (stat-walk
    is the dominant cost on GPFS and exe is the largest pool). Skip flags scope
    the trim sweep only; the orphan-cell modes scope via ``--cas-*-only``.

Whole-pool Sweep
----------------
``--all-variants``
    Trim every RESOLVABLE cell in the pool (the same set ``--list-resolvable``
    prints), not just the active ``--variant`` cell. Cells are trimmed
    sequentially with per-cell failure isolation; intra-cell ``-j`` parallelism
    is preserved. Honours ``--dry-run`` / ``--max-age`` / ``--max-size`` /
    ``--keep-count`` and either a single ``--cas-*-only`` scope flag or one or
    more ``--cas-*-skip`` deselect flags. Mutually exclusive
    with the three orphan-cell modes. With ``--json`` emits one aggregate object
    (``mode: all-variants``). See `Whole-pool sweep (--all-variants)`_.

Orphan-cell Modes
-----------------
``--list-resolvable``
    Read-only. The complement of ``--list-unresolvable``: print the bare sorted
    names of the RESOLVABLE cells (active variants in canonical token order, the
    cells the normal trim and ``--all-variants`` operate on) to stdout, one per
    line, so the output can be piped. NON_CANONICAL / UNRESOLVABLE / UNKNOWN
    cells are excluded. Deletes nothing. See `ORPHANED-VARIANT CELLS`_.

``--list-unresolvable``
    Read-only. List the per-variant cells (``<pool>/<variant>/``) whose variant
    no longer resolves against this checkout's conf hierarchy, with each cell's
    size and newest-file age. Deletes nothing. See `ORPHANED-VARIANT CELLS`_.

``--purge-unresolvable``
    Destructive. Remove cells that are cold (newest file older than
    ``--max-age``) **and** either UNRESOLVABLE here **or** NON_CANONICAL (a
    legacy doubled-token cell like ``gcc.gcc.debug.debug`` that a current build
    will never address again). **Requires ``--max-age > 0``.** Warm cells are
    spared (likely another live checkout's cache); removal is leaf-level and
    lock-safe (contended cells are deferred, not hard-failed). Mutually exclusive
    with ``--list-resolvable`` / ``--list-unresolvable`` / ``--all-variants``; a
    single ``--cas-*-only`` flag scopes it to one cache. Honours ``--dry-run``.

Output Options
--------------
``--json``
    Emit a single JSON object on stdout (all human/progress/warning text goes to
    stderr) with raw integer byte/entry counts per cache, a ``"schema"`` version
    marker, and a ``"mode"`` discriminator. Composes with any mode. See
    `MACHINE-READABLE OUTPUT`_.

Directory Options
-----------------
``--cas-objdir PATH``
    Override object directory from configuration
    (default: ``{git_root}/cas-objdir/{variant}``).

``--cas-pchdir PATH``
    Override PCH directory from configuration
    (default: ``{git_root}/cas-pchdir/{variant}``).

``--cas-pcmdir PATH``
    Override PCM directory from configuration
    (default: ``{git_root}/cas-pcmdir/{variant}``).

``--cas-exedir PATH``
    Override linker-artefact directory from configuration
    (default: ``{git_root}/cas-exedir/{variant}``).

``--bindir PATH``
    Output directory for executables (default: ``bin/{variant}``).

General Options
---------------
``-j N, --jobs N, --parallel N``
    Number of worker threads used to scan the cache directories.
    The scan is metadata-bound (one ``stat()`` per cached entry), so on a
    high-latency cluster / network filesystem (GPFS, Lustre, NFS, CIFS/SMB,
    PanFS, BeeGFS) the per-entry metadata round-trips are fanned out across
    threads for a large speedup.  On local-disk or unrecognised filesystems
    the scan stays single-threaded regardless of this value — threads would
    only add overhead where ``stat()`` is already served from the page cache.
    Default: the available CPU count (honouring CPU affinity, cgroups, and
    slurm allocations), the same as every other ``ct-*`` tool.  See
    PERFORMANCE below.

``--variant VARIANT``
    Build variant to use for configuration (default: blank).

``-v, --verbose``
    Increase output verbosity.  Use ``-v`` for standard output,
    ``-vv`` for debug output.

``-q, --quiet``
    Decrease verbosity.

``-c, --config FILE``
    Specify a configuration file.

``--version``
    Show version and exit.

``--man, --doc``
    Show the full documentation/manual page.

EXIT CODES
==========
0
    Success — all targeted files removed (or none to remove).

    The following outcomes do **not** count as failures and return 0:

    - Deferred cells (``--purge-unresolvable`` mode) left for a later run
      because a peer build was active.
    - Entries that could not be removed on the **first** attempt but
      succeeded on the **single automatic retry** (A7: every first-attempt
      failure is retried once before the summary is printed).
    - Pools that still exceed ``--max-size`` because protected (current or
      hard-linked) entries cannot be evicted (reported as
      ``budget_unmet_bytes`` but not a failure).

1
    Failure, or invalid invocation. Causes include:

    - Some files could not be removed even after the automatic retry — only
      genuine **second-attempt** failures increment the ``failed`` counter and
      trigger exit code 1 (the entry is intentionally left in place; a peer
      build is most likely holding the lock).
    - More than one of ``--cas-objdir-only`` / ``--cas-pchdir-only`` /
      ``--cas-pcmdir-only`` / ``--cas-exedir-only`` was specified (mutually
      exclusive).
    - A ``--cas-*-only`` flag was combined with a ``--cas-*-skip`` flag (opposite
      selection mechanisms), all four pools were ``--cas-*-skip``-ped (nothing
      left to trim), or a ``--cas-*-skip`` flag was combined with one of the
      orphan-cell modes (skip scopes the trim sweep only).
    - More than one of ``--list-resolvable`` / ``--list-unresolvable`` /
      ``--purge-unresolvable`` was specified, or any of them was combined with
      ``--all-variants`` (all mutually exclusive).
    - ``--purge-unresolvable`` was given without ``--max-age``, or with
      ``--max-age <= 0``.
    - With ``--all-variants``, one or more swept cells raised an isolated trim
      failure (reported per-cell in the ``errors`` list of the aggregate;
      the remaining cells still run).
    - ``--max-size`` was given an unrecognised value (not a valid integer or
      decimal with optional K/M/G/T suffix).

EXAMPLES
========
**Daily cron job for shared cache maintenance (age + size budget)**::

    #!/bin/bash
    # Run at 2 AM -- remove non-current entries older than 14 days,
    # and cap each cache pool at 50 GiB regardless of age.
    ct-trim-cache --max-age 14 --max-size 50G

**Daily cron job (age only, no size cap)**::

    #!/bin/bash
    # Remove non-current entries older than 14 days
    ct-trim-cache --max-age 14

**Aggressive cleanup before a release**::

    ct-trim-cache --keep-count 0 --max-age 1

**Preview what the budget pass would remove**::

    ct-trim-cache --max-age 14 --max-size 50G --dry-run

**Preview only, for a specific variant**::

    ct-trim-cache --dry-run --variant=gcc.release

**Trim only object cache on a custom path**::

    ct-trim-cache --cas-objdir-only --cas-objdir=/mnt/shared/build/.objects

PERFORMANCE
===========
Trimming a large CAS is dominated by metadata I/O: the tool must ``stat()``
cached entries to rank them by age.  On a high-latency cluster / network
filesystem (GPFS, Lustre, NFS, CIFS/SMB, PanFS, BeeGFS) each ``stat()`` is a
round-trip to the metadata server, so a serial scan of hundreds of thousands
of entries is slow.  Two measures address this:

- **Parallel scan (filesystem-gated).**  On the filesystems above, the
  per-shard scans are fanned out across ``--parallel`` / ``-j`` worker
  threads; ``stat()`` releases the GIL, so the metadata round-trips overlap.
  On local-disk or unrecognised filesystems the scan stays single-threaded
  (no benefit, only thread overhead) — behaviour there is unchanged.  The
  filesystem is detected the same way the locking subsystem picks its lock
  strategy, so the two always agree.

- **Stat elision for the object CAS.**  Object entries whose content hash
  still matches a tracked source are kept regardless of age, so they are
  never ``stat()``-ed at all; only the non-current entries (which must be
  ranked by mtime) pay a metadata round-trip.  In a healthy cache that is a
  small minority of the entries.

Both measures are transparent — they change only *how fast* the scan runs,
never which entries are kept or removed.

MULTI-USER SHARED CACHES
=========================
Removal is concurrency-safe: the tool takes the producer's per-target lock
before unlinking and skips lock directories, so an in-flight build's files are
never deleted mid-write. The subtlety on a shared pool is *which* entries it
considers stale, not whether deletion races.

**Currency is checkout-relative.** "Non-current" means "not tracked by the
*invoking* checkout's git HEAD". Entries that are current for another branch,
worktree, or user look non-current here. A naive run (default ``--keep-count 1``,
no ``--max-age``) from one checkout can therefore evict other checkouts' recent
entries down to one per basename. On a shared pool:

- Use ``--max-age`` as the primary control. Age is checkout-independent, so it
  preserves other branches' entries as long as they are actively rebuilt
  (e.g. a nightly ``ct-trim-cache --max-age 14``).
- Running with no ``--max-age`` from the wrong checkout is the classic footgun
  (it can show a huge "would remove" set that is really another branch's live
  cache). On a network filesystem the tool emits a wrong-checkout warning when a
  non-empty object scan finds zero current entries and no ``--max-age`` was
  given.
- To reclaim whole cells for variants that no longer exist anywhere in this
  checkout, use the ``--list-unresolvable`` / ``--purge-unresolvable`` modes
  described under `ORPHANED-VARIANT CELLS`_ rather than a bare trim.

Pointed at a bare pool path with the default variant, the tool also warns when
the resolved ``<pool>/<variant>/`` directory does not exist but sibling variant
directories do — the usual sign the wrong ``--variant`` (or a bare pool path)
was given, which would otherwise read as an empty "nothing to do".

For lock cleanup, use ``ct-cleanup-locks``.

SEE ALSO
========
``ct-cache-report`` (1), ``ct-cleanup-locks`` (1), ``ct-cake`` (1), ``ct-cas-publish`` (1), ``ct-config`` (1)
