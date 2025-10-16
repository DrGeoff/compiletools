"""Lock cleanup utility for shared object caches.

Scans for stale lockdirs in shared object directories and removes them.
Handles both local and remote lock verification via SSH.
"""

import os
import sys
import socket
import compiletools.locking
import compiletools.wrappedos
import compiletools.lock_utils


class LockCleaner:
    """Scans and cleans up stale locks in shared object directories."""

    def __init__(self, args):
        """Initialize lock cleaner.

        Args:
            args: ConfigArgParse args object with lock configuration
        """
        self.args = args
        self.hostname = socket.gethostname()
        self.dry_run = getattr(args, 'dry_run', False)
        self.ssh_timeout = getattr(args, 'ssh_timeout', 5)
        self.min_lock_age = getattr(args, 'min_lock_age', args.lock_cross_host_timeout)
        self.verbose = getattr(args, 'verbose', 1)

    def _get_lock_age_seconds(self, lockdir):
        """Get lock age in seconds (uses shared lock_utils).

        Args:
            lockdir: Path to lockdir

        Returns:
            float: Age in seconds, or 0 if lock doesn't exist or has future mtime
        """
        return compiletools.lock_utils.get_lock_age_seconds(lockdir, self.verbose)

    def _read_lock_info(self, lockdir):
        """Read hostname:pid from lock file (uses shared lock_utils).

        Args:
            lockdir: Path to lockdir

        Returns:
            tuple: (hostname, pid) or (None, None) if unreadable
        """
        return compiletools.lock_utils.read_lock_info(lockdir)

    def _is_process_alive_local(self, pid):
        """Check if process is alive on local host (uses shared lock_utils).

        Args:
            pid: Process ID to check

        Returns:
            bool: True if process exists, False otherwise
        """
        return compiletools.lock_utils.is_process_alive_local(pid)

    def _is_process_alive_remote(self, hostname, pid):
        """Check if process is alive on remote host via SSH (uses shared lock_utils).

        Args:
            hostname: Remote hostname
            pid: Process ID to check

        Returns:
            tuple: (is_alive: bool, ssh_error: bool)
                is_alive: True if process is running
                ssh_error: True if SSH connection failed (unknown status)
        """
        return compiletools.lock_utils.is_process_alive_remote(hostname, pid, self.ssh_timeout)

    def _is_lock_stale(self, lockdir):
        """Check if lock is stale.

        Args:
            lockdir: Path to lockdir

        Returns:
            tuple: (is_stale: bool, status_msg: str)
        """
        lock_host, lock_pid = self._read_lock_info(lockdir)

        if lock_host is None:
            return True, "STALE (no lock info)"

        if lock_host == self.hostname:
            # Local lock - use psutil
            if self._is_process_alive_local(lock_pid):
                return False, "ACTIVE (local process running)"
            else:
                return True, "STALE (local process not running)"
        else:
            # Remote lock - SSH check
            is_alive, ssh_error = self._is_process_alive_remote(lock_host, lock_pid)

            if ssh_error:
                return False, "UNKNOWN (SSH connection failed)"
            elif is_alive:
                return False, "ACTIVE (remote process running)"
            else:
                return True, "STALE (remote process not running)"

    def _remove_lockdir(self, lockdir):
        """Remove lockdir.

        Args:
            lockdir: Path to lockdir to remove

        Returns:
            bool: True if removed successfully, False otherwise
        """
        try:
            import shutil
            shutil.rmtree(lockdir, ignore_errors=True)

            if os.path.exists(lockdir):
                print("  ERROR: Failed to remove lockdir (check permissions)", file=sys.stderr)
                return False

            return True
        except Exception as e:
            print(f"  ERROR: Failed to remove lockdir: {e}", file=sys.stderr)
            return False

    def scan_and_cleanup(self, objdir):
        """Scan objdir for stale locks and clean them up.

        Args:
            objdir: Directory to scan for lockdirs

        Returns:
            dict: Statistics about locks found/cleaned
        """
        stats = {
            'total': 0,
            'active': 0,
            'stale_removed': 0,
            'stale_failed': 0,
            'unknown': 0,
            'skipped_young': 0
        }

        if not os.path.exists(objdir):
            print(f"ERROR: Object directory does not exist: {objdir}", file=sys.stderr)
            return stats

        print(f"Scanning for lockdirs in: {objdir}")
        if self.dry_run:
            print("DRY RUN MODE: No locks will be removed")
        print()

        # Find all .lockdir directories
        for root, dirs, files in os.walk(objdir):
            for dirname in dirs:
                if not dirname.endswith('.lockdir'):
                    continue

                lockdir = os.path.join(root, dirname)
                stats['total'] += 1

                lock_host, lock_pid = self._read_lock_info(lockdir)
                lock_age = self._get_lock_age_seconds(lockdir)

                print(f"Lock: {lockdir}")
                print(f"  Host: {lock_host}")
                print(f"  PID: {lock_pid}")
                print(f"  Age: {lock_age:.0f}s")

                # Skip young locks (respect timeout policy)
                if lock_age < self.min_lock_age:
                    print(f"  Status: SKIPPED (younger than {self.min_lock_age}s threshold)")
                    stats['skipped_young'] += 1
                    print()
                    continue

                # Check if stale
                is_stale, status = self._is_lock_stale(lockdir)
                print(f"  Status: {status}")

                if is_stale:
                    if self.dry_run:
                        print("  Action: Would remove lockdir")
                        stats['stale_removed'] += 1
                    else:
                        if self._remove_lockdir(lockdir):
                            print("  Action: REMOVED lockdir")
                            stats['stale_removed'] += 1
                        else:
                            print("  Action: FAILED to remove lockdir")
                            stats['stale_failed'] += 1
                elif "UNKNOWN" in status:
                    stats['unknown'] += 1
                else:
                    stats['active'] += 1

                print()

        return stats

    def print_summary(self, stats):
        """Print summary statistics.

        Args:
            stats: Statistics dict from scan_and_cleanup()
        """
        print("=" * 60)
        print("Scan complete")
        print(f"  Total locks found: {stats['total']}")
        print(f"  Active locks: {stats['active']}")
        print(f"  Stale locks removed: {stats['stale_removed']}")
        if stats['stale_failed'] > 0:
            print(f"  Stale locks failed to remove: {stats['stale_failed']}")
        if stats['unknown'] > 0:
            print(f"  Unknown status (SSH failed): {stats['unknown']}")
        if stats['skipped_young'] > 0:
            print(f"  Young locks skipped: {stats['skipped_young']}")
