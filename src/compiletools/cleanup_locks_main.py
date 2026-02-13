#!/usr/bin/env python3
"""CLI tool for cleaning up stale locks in shared object caches.

This tool scans a shared object directory for stale lockdirs and removes them.
It respects the same configuration settings as the build system (ct.conf, environment
variables, command-line arguments).

Usage:
    ct-cleanup-locks [--objdir=/path/to/objects] [options]

The tool will:
1. Scan for .lockdir directories in the object directory
2. Check if locks are older than the configured timeout
3. For local locks, check if the process is still running
4. For remote locks, SSH to the host and check if the process is running
5. Remove stale locks (or report them in --dry-run mode)
"""

import sys
import compiletools.apptools
import compiletools.cleanup_locks
import compiletools.configutils
import compiletools.namer


def add_arguments(cap):
    """Add cleanup-locks specific arguments.

    Args:
        cap: ConfigArgParse parser
    """
    cap.add(
        '--dry-run',
        action='store_true',
        default=False,
        help='Show what would be removed without actually removing locks'
    )
    cap.add(
        '--ssh-timeout',
        type=int,
        default=5,
        help='SSH connection timeout in seconds for remote process checks (default: 5)'
    )
    cap.add(
        '--min-lock-age',
        type=int,
        default=None,
        help='Only check locks older than this many seconds (default: lock-cross-host-timeout)'
    )


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
    try:
        # Create parser with standard compiletools configuration
        cap = compiletools.apptools.create_parser(
            "Clean up stale locks in shared object caches",
            argv=argv
        )

        # Add cleanup-specific arguments
        add_arguments(cap)

        # Add only the arguments needed for cleanup-locks (not full compiler args)
        variant = compiletools.configutils.extract_variant(argv=argv)
        compiletools.apptools.add_base_arguments(cap, argv=argv, variant=variant)
        compiletools.apptools.add_locking_arguments(cap)
        compiletools.apptools.add_output_directory_arguments(cap, variant)

        # Parse arguments (use parse_args directly, we don't need compiler substitutions)
        args = cap.parse_args(args=argv)
        args.verbose -= args.quiet  # Apply quiet adjustment

        # If min_lock_age not specified, use lock_cross_host_timeout
        if args.min_lock_age is None:
            args.min_lock_age = args.lock_cross_host_timeout

        # Create cleaner and run
        cleaner = compiletools.cleanup_locks.LockCleaner(args)

        # Get objdir from namer (respects ct.conf settings)
        namer = compiletools.namer.Namer(args, argv=argv)
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
        if stats['stale_failed'] > 0:
            return 1

        return 0

    except IOError as ioe:
        # Check if args was set (might fail before argument parsing)
        verbose = getattr(args, 'verbose', 0) if 'args' in locals() else 0
        if verbose < 2:
            print(f"Error: {ioe.strerror}: {ioe.filename}", file=sys.stderr)
            return 1
        else:
            raise
    except Exception as err:
        # Check if args was set (might fail during argument parsing)
        verbose = getattr(args, 'verbose', 0) if 'args' in locals() else 0
        if verbose < 2:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        else:
            raise


if __name__ == '__main__':
    sys.exit(main())
