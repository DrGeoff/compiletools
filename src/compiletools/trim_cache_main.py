#!/usr/bin/env python3
"""CLI tool for trimming stale entries from shared build caches.

This tool scans cas-objdir and cas-pchdir for content-addressable entries
that no longer match the current git state, and removes the oldest stale entries
while preserving a configurable safety margin.

Usage:
    ct-trim-cache [--dry-run] [--cas-objdir PATH] [--cas-pchdir PATH] [options]

The tool will:
1. Load current file hashes from the git repository
2. Scan cas-objdir for .o files whose file hash no longer matches any tracked source
3. Scan cas-pchdir for command-hash directories with old precompiled headers
4. Remove the oldest non-current entries, keeping at least --keep-count per basename

Note on shared / multi-user caches: object currency is relative to the invoking
checkout's git HEAD. On a shared pool used by multiple branches or users, objects
built from other checkouts will appear non-current here. Use ``--max-age`` as the
primary eviction control on shared pools to limit removal by age rather than by
checkout-relative currency.
"""

import json
import sys

import compiletools.apptools
import compiletools.configutils
import compiletools.jobs
import compiletools.trim_cache


def add_arguments(cap):
    """Add trim-cache specific arguments."""
    cap.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be removed without actually removing files",
    )
    cap.add_argument(
        "--max-age",
        type=int,
        default=None,
        help=(
            "Only remove non-current files older than this many days "
            "(default: no age limit). 'Older' means 'written more than N days ago' "
            "(mtime), NOT 'not accessed in N days' — atime is unreliable on "
            "noatime-mounted filesystems, so a hot-but-old cache entry will "
            "still be evicted. On a shared multi-branch or multi-user pool, "
            "this is the recommended primary eviction control: it removes only "
            "entries that have not been rebuilt recently, regardless of which "
            "checkout considers them current."
        ),
    )
    cap.add_argument(
        "--keep-count",
        type=int,
        default=1,
        help="Keep at least this many non-current files per basename/header (default: 1)",
    )
    cap.add_argument(
        "--cas-objdir-only",
        action="store_true",
        default=False,
        help="Only trim the object CAS, skip PCH",
    )
    cap.add_argument(
        "--cas-pchdir-only",
        action="store_true",
        default=False,
        help="Only trim the PCH CAS, skip objects",
    )
    cap.add_argument(
        "--cas-pcmdir-only",
        action="store_true",
        default=False,
        help="Only trim the C++20 module CAS (cas-pcmdir), skip objects and PCH",
    )
    cap.add_argument(
        "--cas-exedir-only",
        action="store_true",
        default=False,
        help="Only trim the executable CAS (cas-exedir), skip objects, PCH, and PCM",
    )
    cap.add_argument(
        "--json",
        action="store_true",
        default=False,
        help=(
            "Emit a single JSON object to stdout with raw integer byte counts and "
            "file counts per cache; route all human/progress text to stderr so "
            "stdout stays pure JSON for machine consumption."
        ),
    )
    cap.add_argument(
        "--list-unresolvable",
        action="store_true",
        default=False,
        help=(
            "READ-ONLY: list cache cells (per-variant <pool>/<variant>/ dirs) whose "
            "variant name no longer resolves against this checkout's conf hierarchy. "
            "Such cells are unreachable by the normal variant-driven trim. Deletes "
            "nothing. NOTE: 'unresolvable from this checkout' is NOT a durable orphan "
            "signal on a shared pool — a cell unresolvable here may be another "
            "checkout's or branch's live cache; the reported age helps tell them apart."
        ),
    )
    cap.add_argument(
        "--purge-unresolvable",
        action="store_true",
        default=False,
        help=(
            "DESTRUCTIVE: purge cache cells (per-variant <pool>/<variant>/ dirs) whose "
            "variant name no longer resolves against this checkout's conf hierarchy AND "
            "whose newest file is older than --max-age (COLD). REQUIRES --max-age > 0 — "
            "a warm unresolvable cell is most likely another live checkout's cache and is "
            "SPARED; zero or negative values are rejected. Removal is leaf-level and "
            "lock-safe; a cell whose artefacts a peer build is mid-write to is deferred "
            "to the next run, not hard-failed. Mutually exclusive with "
            "--list-unresolvable. A single --cas-*-only flag scopes the purge to that "
            "one cache. Honours --dry-run."
        ),
    )


def main(argv=None):
    """Main entry point for ct-trim-cache.

    Returns:
        int: Exit code (0 = success, 1 = failure)
    """
    args = None
    try:
        cap = compiletools.apptools.create_parser("Trim stale entries from shared build caches", argv=argv)

        add_arguments(cap)
        # --parallel / -j: governs scan fan-out on high-latency filesystems
        # (GPFS/NFS/Lustre). Default honours CPU affinity / cgroups / slurm.
        compiletools.jobs.add_arguments(cap)

        variant = compiletools.configutils.extract_variant(argv=argv)
        compiletools.apptools.add_base_arguments(cap, argv=argv, variant=variant)
        compiletools.apptools.add_output_directory_arguments(cap, variant)

        args = cap.parse_args(args=argv)
        compiletools.apptools.resolve_cas_directory_arguments(args)
        args.verbose -= args.quiet

        only_flags = sum(
            bool(getattr(args, name))
            for name in ("cas_objdir_only", "cas_pchdir_only", "cas_pcmdir_only", "cas_exedir_only")
        )
        if only_flags > 1:
            print(
                "Error: --cas-objdir-only / --cas-pchdir-only / --cas-pcmdir-only / "
                "--cas-exedir-only are mutually exclusive (pick at most one)",
                file=sys.stderr,
            )
            return 1

        # --list-unresolvable and --purge-unresolvable are the two standalone
        # pool-level modes. They are MUTUALLY EXCLUSIVE WITH EACH OTHER (the
        # one mode-exclusivity guard). A single --cas-*-only flag is NOT
        # forbidden here — it scopes either pool mode to the one selected cache
        # (handled by the selection logic inside list_/purge_unresolvable_cells).
        if args.list_unresolvable and args.purge_unresolvable:
            print(
                "Error: --list-unresolvable and --purge-unresolvable are mutually exclusive (pick one)",
                file=sys.stderr,
            )
            return 1

        # --list-unresolvable is a standalone READ-ONLY mode: run the orphan
        # listing and return without touching the normal trim path.
        if args.list_unresolvable:
            result = compiletools.trim_cache.list_unresolvable_cells(args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                compiletools.trim_cache.print_unresolvable_report(result)
            return 0

        # --purge-unresolvable is a standalone DESTRUCTIVE pool-level mode. It
        # HARD-ERRORS without --max-age (there is no safe age cutoff to tell a
        # dead variant from another checkout's live cache without one), then
        # runs the purge and returns without the normal cell-level trim.
        if args.purge_unresolvable:
            if args.max_age is None or args.max_age <= 0:
                print(
                    f"Error: --purge-unresolvable requires --max-age > 0 (a positive "
                    f"number of days; only cells whose newest file is older than that "
                    f"are purged — a warm cell may be another checkout's live cache); "
                    f"got {args.max_age!r}",
                    file=sys.stderr,
                )
                return 1
            result = compiletools.trim_cache.purge_unresolvable_cells(args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                compiletools.trim_cache.print_purge_report(result)
            return 0

        # ``--cas-X-only`` flags select a single cache; with none set we
        # trim all four. Each cache runs unless any *other* "only" flag is on.
        do_objdir = not (args.cas_pchdir_only or args.cas_pcmdir_only or args.cas_exedir_only)
        do_pchdir = not (args.cas_objdir_only or args.cas_pcmdir_only or args.cas_exedir_only)
        do_pcmdir = not (args.cas_objdir_only or args.cas_pchdir_only or args.cas_exedir_only)
        do_exedir = not (args.cas_objdir_only or args.cas_pchdir_only or args.cas_pcmdir_only)

        trimmer = compiletools.trim_cache.CacheTrimmer(args)

        objdir_stats = None
        pchdir_stats = None
        pcmdir_stats = None
        exedir_stats = None

        # Object cache currency check is the only consumer of the
        # tracked-files set. PCM, PCH, and exe-cache use sidecar
        # manifests / hard-link refcounts / bucketing instead.
        if do_objdir:
            from compiletools.build_context import BuildContext
            from compiletools.global_hash_registry import load_hashes

            context = BuildContext()
            load_hashes(verbose=args.verbose, context=context)
            current_hashes = compiletools.trim_cache.build_current_hash_set(context)

            if args.verbose >= 1:
                print(f"Loaded {len(current_hashes)} current file hashes from git", file=trimmer._human)
                print(f"Trimming object directory: {args.cas_objdir}", file=trimmer._human)
            objdir_stats = trimmer.trim_objdir(args.cas_objdir, current_hashes)
            if objdir_stats["total_scanned"] == 0:
                compiletools.trim_cache.warn_if_suspicious_cas_dir(
                    args.cas_objdir, "objdir", args.variant, verbose=args.verbose
                )
            compiletools.trim_cache.warn_if_wrong_checkout(
                args.cas_objdir, objdir_stats, args.max_age, verbose=args.verbose
            )

        if do_pchdir:
            if args.verbose >= 1:
                print(f"Trimming PCH directory: {args.cas_pchdir}", file=trimmer._human)
            pchdir_stats = trimmer.trim_pchdir(args.cas_pchdir)
            if pchdir_stats["total_dirs_scanned"] == 0:
                compiletools.trim_cache.warn_if_suspicious_cas_dir(
                    args.cas_pchdir, "pchdir", args.variant, verbose=args.verbose
                )

        if do_pcmdir:
            if args.verbose >= 1:
                print(f"Trimming PCM directory: {args.cas_pcmdir}", file=trimmer._human)
            pcmdir_stats = trimmer.trim_pcmdir(args.cas_pcmdir)
            if pcmdir_stats["total_dirs_scanned"] == 0:
                compiletools.trim_cache.warn_if_suspicious_cas_dir(
                    args.cas_pcmdir, "pcmdir", args.variant, verbose=args.verbose
                )

        if do_exedir:
            if args.verbose >= 1:
                print(f"Trimming executable cache: {args.cas_exedir}", file=trimmer._human)
            exedir_stats = trimmer.trim_exedir(args.cas_exedir)
            if exedir_stats["total_scanned"] == 0:
                compiletools.trim_cache.warn_if_suspicious_cas_dir(
                    args.cas_exedir, "exedir", args.variant, verbose=args.verbose
                )

        # Retry any first-attempt failures exactly once. Must run AFTER all four
        # trim blocks so each cache's trim_* method has had a chance to queue its
        # failures, and BEFORE the summary/JSON output so reported numbers reflect
        # the final post-retry state.
        trimmer.retry_failed()

        if args.json:
            print(
                json.dumps(
                    trimmer.summary_json(
                        objdir_stats=objdir_stats,
                        pchdir_stats=pchdir_stats,
                        pcmdir_stats=pcmdir_stats,
                        exedir_stats=exedir_stats,
                    ),
                    indent=2,
                )
            )
        else:
            trimmer.print_summary(objdir_stats, pchdir_stats, pcmdir_stats, exedir_stats)

        any_failed = (
            (objdir_stats or {}).get("failed", 0)
            + (pchdir_stats or {}).get("failed", 0)
            + (pcmdir_stats or {}).get("failed", 0)
            + (exedir_stats or {}).get("failed", 0)
        )
        return 1 if any_failed else 0

    except OSError as ioe:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {ioe.strerror}: {ioe.filename}", file=sys.stderr)
            return 1
        else:
            raise
    except Exception as err:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
