#!/usr/bin/env python3
"""CLI tool for trimming stale entries from shared build caches.

This tool scans shared-objdir and shared-pchdir for content-addressable entries
that no longer match the current git state, and removes the oldest stale entries
while preserving a configurable safety margin.

Usage:
    ct-trim-cache [--dry-run] [--objdir PATH] [--pchdir PATH] [options]

The tool will:
1. Load current file hashes from the git repository
2. Scan shared-objdir for .o files whose file hash no longer matches any tracked source
3. Scan shared-pchdir for command-hash directories with old precompiled headers
4. Remove the oldest non-current entries, keeping at least --keep-count per basename
"""

import sys

import compiletools.apptools
import compiletools.configutils
import compiletools.trim_cache


def add_arguments(cap):
    """Add trim-cache specific arguments."""
    cap.add(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be removed without actually removing files",
    )
    cap.add(
        "--max-age",
        type=int,
        default=None,
        help=(
            "Only remove non-current files older than this many days "
            "(default: no age limit). 'Older' means 'written more than N days ago' "
            "(mtime), NOT 'not accessed in N days' — atime is unreliable on "
            "noatime-mounted filesystems, so a hot-but-old cache entry will "
            "still be evicted."
        ),
    )
    cap.add(
        "--keep-count",
        type=int,
        default=1,
        help="Keep at least this many non-current files per basename/header (default: 1)",
    )
    cap.add(
        "--objdir-only",
        action="store_true",
        default=False,
        help="Only trim the shared object directory, skip PCH",
    )
    cap.add(
        "--pchdir-only",
        action="store_true",
        default=False,
        help="Only trim the shared PCH directory, skip objects",
    )


def main(argv=None):
    """Main entry point for ct-trim-cache.

    Returns:
        int: Exit code (0 = success, 1 = failure)
    """
    try:
        cap = compiletools.apptools.create_parser("Trim stale entries from shared build caches", argv=argv)

        add_arguments(cap)

        variant = compiletools.configutils.extract_variant(argv=argv)
        compiletools.apptools.add_base_arguments(cap, argv=argv, variant=variant)
        compiletools.apptools.add_output_directory_arguments(cap, variant)

        args = cap.parse_args(args=argv)
        args.verbose -= args.quiet

        if args.objdir_only and args.pchdir_only:
            print("Error: --objdir-only and --pchdir-only are mutually exclusive", file=sys.stderr)
            return 1

        trimmer = compiletools.trim_cache.CacheTrimmer(args)

        objdir_stats = None
        pchdir_stats = None

        if not args.pchdir_only:
            # Load git hashes for current-file detection
            from compiletools.build_context import BuildContext
            from compiletools.global_hash_registry import load_hashes

            context = BuildContext()
            load_hashes(verbose=args.verbose, context=context)
            current_hashes = compiletools.trim_cache.build_current_hash_set(context)

            if args.verbose >= 1:
                print(f"Loaded {len(current_hashes)} current file hashes from git")
                print(f"Trimming object directory: {args.objdir}")
            objdir_stats = trimmer.trim_objdir(args.objdir, current_hashes)

        if not args.objdir_only:
            if args.verbose >= 1:
                print(f"Trimming PCH directory: {args.pchdir}")
            pchdir_stats = trimmer.trim_pchdir(args.pchdir)

        trimmer.print_summary(objdir_stats, pchdir_stats)

        any_failed = (objdir_stats or {}).get("failed", 0) + (pchdir_stats or {}).get("failed", 0)
        return 1 if any_failed else 0

    except OSError as ioe:
        verbose = getattr(args, "verbose", 0) if "args" in locals() else 0
        if verbose < 2:
            print(f"Error: {ioe.strerror}: {ioe.filename}", file=sys.stderr)
            return 1
        else:
            raise
    except Exception as err:
        verbose = getattr(args, "verbose", 0) if "args" in locals() else 0
        if verbose < 2:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
