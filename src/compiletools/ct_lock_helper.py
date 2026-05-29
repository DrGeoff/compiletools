#!/usr/bin/env python3
"""Pure Python lock helper for concurrent builds.

Performance comparison alternative to bash ct-lock-helper.
Reuses tested locking.py implementation with identical algorithms.
"""

import argparse
import os
import subprocess
import sys
from types import SimpleNamespace

import compiletools.apptools


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


def _record_rule_outcome(target: str, cas_kind: str, result_was_skip: bool) -> None:
    """Append a CAS hit/miss outcome line to ``CT_RULE_OUTCOMES_LOG`` if set.

    Mirrors the writer path used by trace_backend's in-process execution so
    that ninja/make backends — whose compile/link recipes shell out to
    ``ct-lock-helper`` — also surface ``cas.*`` per-rule attributes on the
    OTel spans ct-cake emits post-build.  Best-effort: any failure here
    silently no-ops so it cannot perturb a successful build (the helper's
    own exit code semantics matter to the recipe).

    Note: the native-flock fast-path in ``build_backend.wrap_compile_with_lock``
    (local filesystems with util-linux ``flock`` available) bypasses this
    helper entirely and so does not write outcomes.  Documented in
    ``README.ct-otel.rst`` under "CAS-attribute coverage scope".
    """
    try:
        from compiletools.build_timer import append_rule_outcome

        if not os.environ.get("CT_RULE_OUTCOMES_LOG"):
            return
        # On a skip, atomic_compile/atomic_link returned None — the artefact
        # already existed when we acquired the lock (CAS hit from a peer).
        # On a real run, the target file exists post-rename; size is the
        # bytes the next downstream caller would have produced.
        cas_hit = result_was_skip
        try:
            bytes_reused = os.path.getsize(target) if cas_hit and os.path.exists(target) else 0
        except OSError:
            bytes_reused = 0
        append_rule_outcome(target, cas_kind, cas_hit, bytes_reused)
    except Exception:
        # Outcomes-log writes are diagnostics only — never fail a build over
        # them.  The build_timer module guards each syscall internally; this
        # outer try/except catches import errors or unexpected failures.
        pass


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

    # Delegate to shared atomic_compile (compile to temp, rename to target).
    # atomic_compile returns None when skip_if_exists short-circuited; we
    # leave skip_if_exists at its default False here (a recipe-driven compile
    # has already passed the build system's up-to-date check, so the helper
    # itself does not skip) so the outcome is always a "miss" (compiler ran).
    # The hit/miss distinction at this layer is whether the target exists
    # when we get inside the lock; the build system handles the higher-level
    # CAS hit semantics.  For now ct-lock-helper records every invocation as
    # a miss — the build_system-level CAS short-circuit happens before the
    # recipe is even dispatched.
    result = atomic_compile(lock, args.target, args.compile_cmd)
    _record_rule_outcome(args.target, "obj", result is None)


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

    # See cmd_compile for the result-is-None semantics.  cas_kind="exe" is
    # the closest fit — ninja/make backends route static/shared libraries
    # through their own archive/link recipes that also call into this helper;
    # at this layer we cannot tell a static library from an executable, so
    # we tag it "exe" and accept the small fidelity loss.  trace_backend,
    # which has the rule-type metadata, tags lib/pcm/pch correctly.
    result = atomic_link(lock, args.target, args.link_cmd)
    _record_rule_outcome(args.target, "exe", result is None)


def main(argv=None):
    """Main entry point.

    Args:
        argv: Command line arguments (default: sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if argv is None:
        argv = sys.argv[1:]

    # Parse arguments
    from compiletools.version import __version__

    parser = argparse.ArgumentParser(
        prog="ct-lock-helper", description="File locking helper for concurrent builds (Python implementation)"
    )
    parser.add_argument("--version", action="version", version=__version__)

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

    # Forward Ctrl-C / kill to the locked subprocess via apptools.graceful_shutdown.
    # The context manager guarantees restoration of the caller's prior handlers,
    # which matters for in-process invocations (e.g. the entry-point lint test)
    # where leaked handlers would contaminate pytest's own signal handling.
    exit_handler = GracefulExit()
    with compiletools.apptools.graceful_shutdown(exit_handler.cleanup):
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
