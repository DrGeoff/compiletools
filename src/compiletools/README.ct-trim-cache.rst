================
ct-trim-cache
================

-------------------------------------------------------------------------
Trim stale entries from shared object and PCH caches
-------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-04-21
:Version: 8.2.3
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-trim-cache [--dry-run] [--objdir PATH] [--pchdir PATH] [--max-age DAYS] [--keep-count N] [-v]

DESCRIPTION
===========
Trim stale content-addressable entries from ``shared-objdir`` (compiled object
files) and ``shared-pchdir`` (precompiled headers).  The tool identifies
which entries still match the current git working tree and removes the oldest
non-current entries while preserving a configurable safety margin.

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

Legacy entries without a manifest fall back to the previous global
ranking by mtime, keeping the rollout backwards-compatible.  If
``--max-age`` is set, anything within the cutoff is kept regardless of
bucket.

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
    ct-trim-cache --objdir-only

    # Only trim PCH cache, skip objects
    ct-trim-cache --pchdir-only

    # Custom directories
    ct-trim-cache --objdir=/shared/build/objects --pchdir=/shared/build/pch

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

``--objdir-only``
    Only trim the shared object directory, skip PCH trimming.

``--pchdir-only``
    Only trim the shared PCH directory, skip object trimming.

Directory Options
-----------------
``--objdir PATH``
    Override object directory from configuration
    (default: ``{git_root}/shared-objdir/{variant}``).

``--pchdir PATH``
    Override PCH directory from configuration
    (default: ``{git_root}/shared-pchdir/{variant}``).

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
    or ``--objdir-only`` and ``--pchdir-only`` both specified.

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

    ct-trim-cache --objdir-only --objdir=/mnt/shared/build/.objects

MULTI-USER SHARED CACHES
=========================
Safe for multi-user environments.  The tool only removes files that are
no longer current (stale content hashes) and skips lock directories.
Ongoing builds targeting current source files will not be affected because
their content-addressed filenames will not match the stale entries.

For lock cleanup, use ``ct-cleanup-locks``.

SEE ALSO
========
``ct-cleanup-locks`` (1), ``ct-cake`` (1), ``ct-config`` (1)
