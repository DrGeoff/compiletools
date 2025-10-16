ct-cleanup-locks
================

Purpose
-------
Clean up stale lock directories in shared object caches from crashed builds,
network failures, or terminated processes.

When to Use
-----------
- After system crashes or power failures
- When builds hang waiting for locks
- Periodic maintenance of shared build caches (cron)
- Multi-user shared cache environments

**Warning**: Only run when no builds are actively running, or use ``--dry-run`` first.

Usage
-----
::

    # Preview what would be removed
    ct-cleanup-locks --dry-run

    # Remove stale locks (uses objdir from ct.conf)
    ct-cleanup-locks

    # Custom object directory
    ct-cleanup-locks --objdir=/shared/build/cache

    # Increase verbosity
    ct-cleanup-locks --verbose 2

Configuration
-------------
Respects settings from ct.conf:

- ``objdir``: Object directory to scan (default from configuration)
- ``lock-cross-host-timeout``: Minimum lock age before considering stale
- Uses same timeout policy as build system for consistency

Options
-------
``--dry-run``
    Show what would be removed without actually removing locks

``--objdir PATH``
    Override object directory from configuration

``--min-lock-age SECONDS``
    Only check locks older than this (default: lock-cross-host-timeout)

``--ssh-timeout SECONDS``
    SSH connection timeout for remote process checks (default: 5)

``--verbose LEVEL``
    Increase output verbosity (0=minimal, 1=standard, 2=debug)

How It Works
------------
1. Scans objdir for .lockdir directories
2. Reads hostname:pid from each lock
3. For local locks: checks if process still exists
4. For remote locks: SSHs to host and checks if process exists
5. Removes locks where process is dead or very old

Lock Age Policy
---------------
Locks younger than ``min-lock-age`` are always preserved, even if the
process appears dead. This protects against clock skew between hosts.

Default: Uses ``lock-cross-host-timeout`` from ct.conf (typically 600s)

Multi-User Shared Caches
-------------------------
Safe for multi-user environments. Only removes locks where:

- Process is confirmed dead (local) or
- Process check fails via SSH (remote) or
- Lock exceeds maximum age threshold

Active locks and locks that can't be verified (SSH failures) are preserved.

Exit Codes
----------
0
    Success - all stale locks removed or none found
1
    Failure - some stale locks could not be removed (check permissions)

Examples
--------
**Daily cron job for shared cache maintenance**::

    #!/bin/bash
    # Run at 2 AM when builds are unlikely
    ct-cleanup-locks --min-lock-age 7200

**Check before critical build**::

    ct-cleanup-locks --dry-run
    # Review output, then:
    ct-cleanup-locks

**Debug stuck lock**::

    ct-cleanup-locks --verbose 2 --dry-run
    # Shows detailed info about each lock

**Cleanup specific directory**::

    ct-cleanup-locks --objdir=/mnt/shared/build/.objects

Lock Format
-----------
Locks are directories named ``<filename>.lockdir`` containing a ``pid`` file
with the format::

    hostname:pid

For example::

    build01.example.com:12345

The tool uses this information to determine if the process is still running.

SSH Requirements
----------------
For remote lock verification, the tool requires:

- SSH access to remote hosts (passwordless)
- BatchMode (no interactive prompts)
- Ability to run ``kill -0 PID`` on remote hosts

If SSH fails, the lock is preserved as unknown status.

Troubleshooting
---------------
**Locks not being removed**

- Check lock age with ``--verbose 2``
- Verify locks are older than ``--min-lock-age``
- For remote locks, verify SSH connectivity
- Check permissions on lockdir

**Permission denied errors**

- Ensure you have write access to objdir
- In multi-user environments, ensure group permissions are correct
- May need to run as same user who created locks

**SSH timeouts**

- Increase ``--ssh-timeout`` for slow networks
- Check SSH configuration (BatchMode, keys)
- Verify remote hosts are reachable

See Also
--------
- ct.conf: Configuration file for lock timeout settings
- Locking documentation in compiletools
- Multi-user shared cache documentation
