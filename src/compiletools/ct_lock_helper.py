#!/usr/bin/env python3
"""Pure Python lock helper for concurrent builds.

Performance comparison alternative to bash ct-lock-helper.
Reuses tested locking.py implementation with identical algorithms.
"""

import argparse
import os
import signal
import subprocess
import sys
from types import SimpleNamespace


class GracefulExit:
    """Handle cleanup on signals."""

    def __init__(self):
        self.lock = None
        self.tempfile = None
        self.acquired = False

    def cleanup(self, signum=None, frame=None):
        """Clean up lock and temp file on exit."""
        if self.acquired and self.lock:
            try:
                self.lock.release()
            except Exception:
                pass

        if self.tempfile and os.path.exists(self.tempfile):
            try:
                os.unlink(self.tempfile)
            except Exception:
                pass

        if signum is not None:
            sys.exit(128 + signum)


def _env_value(name, default, parse):
    """Read env var ``name`` and parse it with ``parse``. On parse failure,
    print a clear warning naming the variable and the offending value, then
    fall back to ``default``. (Issue #8: prevents a generic ValueError from
    int()/float() killing the helper with no indication of which env var was
    bad.)"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return parse(raw)
    except (ValueError, TypeError) as e:
        print(
            f"Warning: invalid value for {name}={raw!r} ({e}); using default {default!r}",
            file=sys.stderr,
        )
        return default


def create_args_from_env():
    """Create args object from environment variables matching bash version."""
    return SimpleNamespace(
        file_locking=True,
        lock_warn_interval=_env_value("CT_LOCK_WARN_INTERVAL", 30, int),
        lock_cross_host_timeout=_env_value("CT_LOCK_TIMEOUT", 600, int),
        sleep_interval_lockdir=_env_value("CT_LOCK_SLEEP_INTERVAL", 0.05, float),
        sleep_interval_cifs=_env_value("CT_LOCK_SLEEP_INTERVAL_CIFS", 0.1, float),
        sleep_interval_flock_fallback=_env_value("CT_LOCK_SLEEP_INTERVAL_FLOCK", 0.1, float),
        verbose=_env_value("CT_LOCK_VERBOSE", 0, int),
    )


def create_lock(strategy, target_file, args):
    """Create appropriate lock instance based on strategy.

    Args:
        strategy: One of 'lockdir', 'cifs', 'flock', 'fcntl'
        target_file: Target output file path
        args: Args object with lock configuration

    Returns:
        Lock instance (LockdirLock, CIFSLock, FlockLock, or FcntlLock)
    """
    # Import here to reduce startup overhead if --help is requested
    from compiletools.locking import CIFSLock, FcntlLock, FlockLock, LockdirLock

    if strategy == "lockdir":
        return LockdirLock(target_file, args)
    elif strategy == "cifs":
        return CIFSLock(target_file, args)
    elif strategy == "flock":
        return FlockLock(target_file, args)
    elif strategy == "fcntl":
        return FcntlLock(target_file, args)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")


def cmd_compile(args, exit_handler):
    """Handle 'compile' subcommand.

    Args:
        args: Parsed arguments
        exit_handler: GracefulExit instance
    """
    from compiletools.locking import atomic_compile

    # Create args object from environment
    lock_args = create_args_from_env()

    # Create lock based on strategy
    lock = create_lock(args.strategy, args.target, lock_args)

    # Register lock for signal-handler cleanup
    exit_handler.lock = lock

    # Delegate to shared atomic_compile (compile to temp, rename to target)
    atomic_compile(lock, args.target, args.compile_cmd)


def cmd_link(args, exit_handler):
    """Handle 'link' subcommand.

    Args:
        args: Parsed arguments
        exit_handler: GracefulExit instance
    """
    from compiletools.locking import atomic_link

    lock_args = create_args_from_env()
    lock = create_lock(args.strategy, args.target, lock_args)

    exit_handler.lock = lock

    atomic_link(lock, args.target, args.link_cmd)


def main(argv=None):
    """Main entry point.

    Args:
        argv: Command line arguments (default: sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if argv is None:
        argv = sys.argv[1:]

    # Set up signal handling
    exit_handler = GracefulExit()
    signal.signal(signal.SIGINT, exit_handler.cleanup)
    signal.signal(signal.SIGTERM, exit_handler.cleanup)

    # Parse arguments
    parser = argparse.ArgumentParser(
        prog="ct-lock-helper", description="File locking helper for concurrent builds (Python implementation)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Compile subcommand
    compile_parser = subparsers.add_parser("compile", help="Compile with file locking")
    compile_parser.add_argument("--target", required=True, help="Target output file (e.g., file.o)")
    compile_parser.add_argument(
        "--strategy",
        required=True,
        choices=["lockdir", "cifs", "flock", "fcntl"],
        help="Lock strategy: lockdir (NFS/Lustre), fcntl (GPFS), cifs (CIFS/SMB), flock (local)",
    )
    compile_parser.add_argument("compile_cmd", nargs="+", help="Compile command and arguments")

    # Link subcommand
    link_parser = subparsers.add_parser("link", help="Link/archive with file locking")
    link_parser.add_argument("--target", required=True, help="Target output file (e.g., file.a or executable)")
    link_parser.add_argument(
        "--strategy",
        required=True,
        choices=["lockdir", "cifs", "flock", "fcntl"],
        help="Lock strategy: lockdir (NFS/Lustre), fcntl (GPFS), cifs (CIFS/SMB), flock (local)",
    )
    link_parser.add_argument("link_cmd", nargs="+", help="Link command and arguments")

    # Parse
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Execute command
    try:
        if args.command == "compile":
            cmd_compile(args, exit_handler)
        elif args.command == "link":
            cmd_link(args, exit_handler)
        return 0
    except subprocess.CalledProcessError as e:
        # Compilation failed, return compiler's exit code
        return e.returncode
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 125  # Helper internal error (matches bash)


if __name__ == "__main__":
    sys.exit(main())
