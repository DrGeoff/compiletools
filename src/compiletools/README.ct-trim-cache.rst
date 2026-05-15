================
ct-trim-cache
================

----------------------------------------------------------------------------------
Trim stale entries from the object, PCH, PCM, and linker-artefact CAS directories
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 10.0.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-trim-cache [--dry-run] [--cas-objdir PATH] [--cas-pchdir PATH] [--cas-pcmdir PATH] [--cas-exedir PATH] [--max-age DAYS] [--keep-count N] [-v]

DESCRIPTION
===========
Trim stale content-addressable entries from ``cas-objdir`` (compiled
object files), ``cas-pchdir`` (precompiled headers), ``cas-pcmdir``
(C++20 module BMIs), and ``cas-exedir`` (linker artefacts —
executables, static libraries, and shared libraries). The tool
identifies which entries still match the current git working tree
and removes the oldest non-current entries while preserving a
configurable safety margin.

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

WHEN TO USE
===========
- Shared cache growing too large or approaching disk quota
- Periodic maintenance of shared build caches (cron)
- After major branch switches that invalidate many cache entries
- After refactoring that renames or removes many source files

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

    # Custom directories
    ct-trim-cache --cas-objdir=/shared/build/objects --cas-pchdir=/shared/build/pch \
                  --cas-pcmdir=/shared/build/pcm --cas-exedir=/shared/build/exe

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

OPTIONS
=======

Trim Options
------------
``--dry-run``
    Show what would be removed without actually removing files.

``--max-age DAYS``
    Only remove non-current files older than this many days.
    Default: no age limit (removal controlled by ``--keep-count`` only).

``--keep-count N``
    Keep at least N non-current files per basename/header.
    Default: 1.  Set to 0 to remove all non-current entries (the safety
    invariant still preserves at least one file per basename).

``--cas-objdir-only``
    Only trim the object CAS, skip PCH, PCM, and linker-artefact trimming.

``--cas-pchdir-only``
    Only trim the PCH CAS, skip object, PCM, and linker-artefact trimming.

``--cas-pcmdir-only``
    Only trim the PCM CAS, skip object, PCH, and linker-artefact trimming.

``--cas-exedir-only``
    Only trim the linker-artefact CAS (executables, static libraries,
    shared libraries), skip object, PCH, and PCM trimming.

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
    Success -- all targeted files removed (or none to remove).
1
    Failure -- some files could not be removed (check permissions),
    or more than one of ``--cas-objdir-only`` / ``--cas-pchdir-only``
    / ``--cas-pcmdir-only`` / ``--cas-exedir-only`` was specified
    (they are mutually exclusive).

EXAMPLES
========
**Daily cron job for shared cache maintenance**::

    #!/bin/bash
    # Run at 2 AM -- remove non-current entries older than 14 days
    ct-trim-cache --max-age 14

**Aggressive cleanup before a release**::

    ct-trim-cache --keep-count 0 --max-age 1

**Preview only, for a specific variant**::

    ct-trim-cache --dry-run --variant=gcc.release

**Trim only object cache on a custom path**::

    ct-trim-cache --cas-objdir-only --cas-objdir=/mnt/shared/build/.objects

MULTI-USER SHARED CACHES
=========================
Safe for multi-user environments.  The tool only removes files that are
no longer current (stale content hashes) and skips lock directories.
Ongoing builds targeting current source files will not be affected because
their content-addressed filenames will not match the stale entries.

For lock cleanup, use ``ct-cleanup-locks``.

SEE ALSO
========
``ct-cache-report`` (1), ``ct-cleanup-locks`` (1), ``ct-cake`` (1), ``ct-cas-publish`` (1), ``ct-config`` (1)
