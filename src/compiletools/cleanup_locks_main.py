#!/usr/bin/env python3
"""CLI tool for cleaning up stale locks in the object CAS.

This tool scans an object CAS for stale lockdirs and removes them.
It respects the same configuration settings as the build system (ct.conf, environment
variables, command-line arguments).

Usage:
    ct-cleanup-locks [--cas-objdir=/path/to/objects] [options]

The tool will:
1. Scan for .lockdir directories in the object directory
2. Check if locks are older than the configured timeout
3. For local locks, check if the process is still running
4. For remote locks, SSH to the host and check if the process is running
5. Remove stale locks (or report them in --dry-run mode)
"""

import os
import sys

import compiletools.apptools
import compiletools.cleanup_locks
import compiletools.configutils
import compiletools.namer
import compiletools.trim_cache


def add_arguments(cap):
    """Add cleanup-locks specific arguments.

    Args:
        cap: ConfigArgParse parser
    """
    cap.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be removed without actually removing locks",
    )
    cap.add_argument(
        "--ssh-timeout",
        type=int,
        default=5,
        help="SSH connection timeout in seconds for remote process checks (default: 5)",
    )
    cap.add_argument(
        "--min-lock-age",
        type=int,
        default=None,
        help="Only check locks older than this many seconds (default: lock-cross-host-timeout)",
    )
    cap.add_argument(
        "--all-variants",
        action="store_true",
        default=False,
        help=(
            "Sweep every RESOLVABLE obj-pool cell for stale locks, not just the "
            "active --variant cell.  Enumerates the pool root derived from "
            "--cas-objdir + --variant via cell_pool_root."
        ),
    )


def _run_all_variants(args, cleaner):
    """Sweep every RESOLVABLE obj-pool cell for stale locks.

    Derives the pool root from ``args.cas_objdir`` + ``args.variant`` via
    ``cell_pool_root``, then enumerates every RESOLVABLE cell in the pool and
    calls ``cleaner.scan_and_cleanup`` on each in sorted order.  Per-cell
    exceptions are caught and recorded — one bad cell must not abort the sweep.

    Args:
        args: Parsed args namespace (needs ``cas_objdir``, ``variant``,
            ``verbose``).
        cleaner: ``LockCleaner`` instance (already configured with ``args``).

    Returns:
        int: 0 on full success, 1 if any cell failed or raised an exception.
    """
    pool = compiletools.trim_cache.cell_pool_root(args.cas_objdir, args.variant)
    # Restrict to RESOLVABLE cells only, consistent with the trim-cache
    # --all-variants and --list-resolvable scope. NON_CANONICAL/UNRESOLVABLE
    # cells are left for the dedicated trim-cache purge modes.
    cells = [
        c
        for c in compiletools.trim_cache.enumerate_cells(pool, "obj")
        if c["label"] == compiletools.trim_cache._CELL_RESOLVABLE
    ]

    _zero_stats = {"total": 0, "active": 0, "stale_removed": 0, "stale_failed": 0, "unknown": 0, "skipped_young": 0}
    aggregate = dict(_zero_stats)
    errors: list[dict] = []
    failed = False

    for cell in sorted(cells, key=lambda c: c["name"]):
        cell_path = os.path.join(pool, cell["name"])
        try:
            cell_stats = cleaner.scan_and_cleanup(cell_path)
        except Exception as exc:
            errors.append({"variant": cell["name"], "error": str(exc)})
            failed = True
            continue
        for key in _zero_stats:
            aggregate[key] += cell_stats[key]
        if cell_stats["stale_failed"] > 0:
            failed = True

    # Print aggregate summary using the standard cleaner method
    if args.verbose >= 1:
        print(f"Swept {len(cells)} RESOLVABLE obj-pool cell(s) (--all-variants).")
    cleaner.print_summary(aggregate)

    if errors:
        for err in errors:
            print(f"  Error in cell {err['variant']!r}: {err['error']}", file=sys.stderr)

    return 1 if failed else 0


def main(argv=None):
    """Main entry point for ct-cleanup-locks.

    Args:
        argv: Command-line arguments (for testing)

    Returns:
        int: Exit code (0 = success, 1 = failure)

    Exit Codes:
        0: Success (all stale locks removed or none found)
        1: Failure (stale locks failed to remove, or exception caught)

    Exception Behavior:
        verbose < 2: Catch exceptions, print simple message, return 1
        verbose >= 2: Re-raise exceptions with full traceback for debugging
    """
    args = None
    try:
        # Create parser with standard compiletools configuration
        cap = compiletools.apptools.create_parser("Clean up stale locks in the object CAS", argv=argv)

        # Add cleanup-specific arguments
        add_arguments(cap)

        # Add only the arguments needed for cleanup-locks (not full compiler args)
        variant = compiletools.configutils.extract_variant(argv=argv)
        compiletools.apptools.add_base_arguments(cap, argv=argv, variant=variant)
        compiletools.apptools.add_locking_arguments(cap)
        compiletools.apptools.add_output_directory_arguments(cap, variant)

        # Parse arguments (use parse_args directly, we don't need compiler substitutions)
        args = cap.parse_args(args=argv)
        compiletools.apptools.resolve_cas_directory_arguments(args)
        args.verbose -= args.quiet  # Apply quiet adjustment

        # If min_lock_age not specified, use lock_cross_host_timeout
        if args.min_lock_age is None:
            args.min_lock_age = args.lock_cross_host_timeout

        # Create cleaner and run
        cleaner = compiletools.cleanup_locks.LockCleaner(args)

        if args.all_variants:
            return _run_all_variants(args, cleaner)

        # Get objdir from namer (respects ct.conf settings)
        from compiletools.build_context import BuildContext

        namer = compiletools.namer.Namer(args, argv=argv, context=BuildContext())
        objdir = namer.object_dir()

        if args.verbose >= 1:
            print("Configuration:")
            print(f"  Object directory: {objdir}")
            print(f"  Min lock age: {args.min_lock_age}s")
            print(f"  SSH timeout: {args.ssh_timeout}s")
            print(f"  Dry run: {args.dry_run}")
            print()

        # Scan and cleanup
        stats = cleaner.scan_and_cleanup(objdir)

        # Print summary
        cleaner.print_summary(stats)

        # Return error if any locks failed to remove
        if stats["stale_failed"] > 0:
            return 1

        return 0

    except OSError as ioe:
        # Check if args was set (might fail before argument parsing)
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {ioe.strerror}: {ioe.filename}", file=sys.stderr)
            return 1
        else:
            raise
    except Exception as err:
        # Check if args was set (might fail during argument parsing)
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        else:
            raise


if __name__ == "__main__":
    sys.exit(main())
