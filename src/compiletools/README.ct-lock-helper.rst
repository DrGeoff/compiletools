==============
ct-lock-helper
==============

------------------------------------------------------------
Helper for file locking during concurrent compilation
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2024-01-01
:Version: 10.1.7
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-lock-helper compile --target=OUTPUT --strategy=STRATEGY -- COMMAND
ct-lock-helper link --target=OUTPUT --strategy=STRATEGY -- COMMAND

DESCRIPTION
===========

``ct-lock-helper`` manages file locks when building with file locking
(``--file-locking`` flag). It wraps compilation commands to ensure atomic
file creation and prevent race conditions in multi-user or parallel build
environments.

The helper is a Python entry point that delegates to ``locking.py``'s
``atomic_compile()`` and ``atomic_link()`` functions, reusing the same tested
locking algorithms used by the Shake backend.

The helper implements four locking strategies automatically selected based on
the target filesystem type:

- **lockdir**: For NFS, Lustre (mkdir-based, works across all filesystems)
- **fcntl**: For GPFS (fcntl.lockf, cross-node, kernel-managed)
- **cifs**: For CIFS/SMB (exclusive file creation)
- **flock**: For local filesystems like ext4, xfs, btrfs (POSIX flock, kernel-managed blocking)

Usage
-----

ct-lock-helper is invoked automatically by ``ct-cake`` when using ``--file-locking``
with the Make or Ninja backends. On local filesystems, those backends may use a
native ``flock`` fast path instead of starting Python for each rule.
You typically don't call it directly, but it's useful to understand for debugging.

Basic command format::

    ct-lock-helper compile --target=OUTPUT.o --strategy=STRATEGY -- COMPILE_COMMAND
    ct-lock-helper link --target=OUTPUT --strategy=STRATEGY -- LINK_COMMAND

Example::

    ct-lock-helper compile --target=file.o --strategy=lockdir -- gcc -c file.c

The helper will:

1. Acquire lock based on strategy
2. For ``compile``: run the compiler with ``-o OUTPUT.PID.RANDOM.tmp``, then
   atomically replace ``OUTPUT`` with the completed temp file
3. For ``link``: rewrite a recognized ``-o OUTPUT`` or ``ar ... OUTPUT ...``
   command to write ``OUTPUT.PID.RANDOM.tmp``, then atomically replace
   ``OUTPUT`` with the completed temp file
4. Release lock

If ``atomic_link()`` cannot find the target in the link command, it runs the
command unchanged and, at verbose level 2 or higher, prints a warning that
temp-and-rename atomicity could not be provided for that command shape.

Configuration
-------------

Environment variables control lock behavior:

**CT_LOCK_SLEEP_INTERVAL**
    Seconds to sleep between lock acquisition attempts for lockdir strategy (default: 0.05)

    - Lustre filesystems: 0.01 (fast parallel filesystem)
    - NFS filesystems: 0.1 (network latency)
    - Others: 0.05 (balanced)

    Note: GPFS uses the fcntl strategy (kernel-managed blocking), not lockdir polling.

**CT_LOCK_SLEEP_INTERVAL_CIFS**
    Seconds to sleep between lock acquisition attempts for CIFS strategy (default: 0.1)

**CT_LOCK_SLEEP_INTERVAL_FLOCK**
    Unused since flock blocks in kernel (no polling). Kept for backwards compatibility.

**CT_LOCK_WARN_INTERVAL**
    Seconds between lock wait warnings (default: 30)

    Set to lower value for more frequent status updates during contention.

**CT_LOCK_TIMEOUT**
    Cross-host lock timeout in seconds (default: 600)

    After this time, cross-host locks trigger escalated warnings.
    The lock is NOT automatically removed (can't verify remote process).

**CT_LOCK_VERBOSE**
    Verbosity level (default: 0)

    - 0: Errors only
    - 1+: Show stale lock removal messages

Example::

    export CT_LOCK_WARN_INTERVAL=10
    export CT_LOCK_TIMEOUT=300
    ct-cake --file-locking

Lock Strategies
---------------

lockdir (NFS/Lustre)
^^^^^^^^^^^^^^^^^^^^

Uses ``mkdir`` for atomic lock acquisition. Works on all POSIX filesystems.

**Features:**

- Stale lock detection via hostname:pid tracking
- Automatic cleanup of locks from dead processes (same-host only)
- Age-based warnings for cross-host locks
- Permissions: 775 for lockdir, 664 for pid file

**Lock structure:**

::

    target.o.lockdir/
        pid              # Contains "hostname:pid" or "hostname:pid:start_time"

**Stale lock handling:**

- Same-host: Checks if process alive (``os.kill(pid, 0)``)
- Cross-host: Cannot verify, relies on age-based timeout warnings

fcntl (GPFS)
^^^^^^^^^^^^

Uses ``fcntl.lockf()`` for cross-node locking on GPFS. Unlike ``flock()``, which
is node-local on GPFS, ``fcntl`` record locks work correctly across nodes. The
kernel handles blocking and automatic release on process death, eliminating the
need for polling and stale lock detection.

**Features:**

- Cross-node mutual exclusion via kernel-managed record locks
- No polling: ``lockf(LOCK_EX)`` blocks in the kernel
- No stale detection: kernel releases locks automatically on process death
- Locks a sidecar ``<target>.lock`` file, not the target itself
- Compile and helper-wrapped link rules still use temp-and-rename output
- The sidecar lock file is not removed on release

**Lock structure:**

::

    target.o.lock        # Locked via fcntl.lockf(); persists after release

The fcntl advisory lock is placed on the sidecar file. Locking the target
itself would create an empty target before the compile runs, which can make a
peer ``make`` process treat that empty file as up-to-date.

cifs (CIFS/SMB)
^^^^^^^^^^^^^^^

Uses exclusive file creation (``O_CREAT|O_EXCL``) for CIFS compatibility.

**Lock structure:**

::

    target.o.lock        # Base lockfile; persists after release
    target.o.lock.excl   # Exclusive marker; removed on release

flock (Local filesystems)
^^^^^^^^^^^^^^^^^^^^^^^^^^

Uses POSIX ``flock()`` for kernel-managed blocking. Only used on local
filesystems (ext4/xfs/btrfs) where ``flock()`` is always available.

**Features:**

- Kernel-managed blocking: ``flock(LOCK_EX)`` blocks until acquired
- Automatic release on process death
- Locks a sidecar ``<target>.lock`` file, not the target itself
- Helper compile and helper link rules use temp-and-rename output
- The sidecar lock file is not removed on release

**Lock structure:**

::

    target.o.lock        # Locked via flock(); persists after release

The flock advisory lock is placed on the sidecar file. Locking the target
itself would create an empty target before the compile or link command runs,
which can make a peer ``make`` process treat that empty file as up-to-date.

When Make or Ninja use the native ``flock`` fast path, compile commands lock
``<target>.lock``, write ``<target>.compiletools.tmp``, then ``mv -f`` the temp
file into place. Native ``flock`` link commands lock ``<target>.lock`` and run
the link command unchanged; they do not rewrite the link output to a temp file.

Performance
-----------

ct-lock-helper adds ~45-65ms overhead per compilation due to Python startup
and import costs. This is negligible for real C/C++ files (100ms-10s compile
time) and under lock contention (where lock wait time dominates).

**When file locking is beneficial:**

- Multi-user team builds with shared cache
- Parallel builds on NFS/GPFS/Lustre (GPFS uses fcntl for best performance)
- CI/CD with persistent object directories

**When to skip:**

- Fast local single-threaded builds
- Many tiny files (<100ms compile time each)
- Use ``--no-file-locking`` to disable

**Filesystem detection:**

Strategy is determined once in Python and baked into Makefile/build.ninja.
No per-compilation filesystem detection overhead.

Troubleshooting
---------------

**"ct-lock-helper not found in PATH"**

Solutions:

1. Install compiletools: ``pip install compiletools``
2. Install from source: ``pip install -e .``
3. Add to PATH: ``export PATH=/path/to/compiletools:$PATH``
4. Disable file locking: use ``--no-file-locking``

**Locks not releasing**

Check for:

- Killed processes: Use ``ct-cleanup-locks`` to remove stale locks
- Permission issues: Ensure parent directory has SGID bit and group write
- Network issues: Check NFS mount status

**Slow builds with locking**

ct-lock-helper adds ~45-65ms overhead per compilation due to Python startup.
This is negligible for real C/C++ files (100ms-10s compile time) but may be
noticeable for many tiny files.

Solutions:

- Adjust sleep intervals::

    export CT_LOCK_SLEEP_INTERVAL=0.01      # For lockdir on Lustre
    export CT_LOCK_SLEEP_INTERVAL_CIFS=0.05 # For CIFS strategy

- For very fast local-only builds, consider ``--no-file-locking``

**Cross-host lock stuck**

If a remote host crashes, locks must be manually removed::

    rm -rf /path/to/target.o.lockdir

Or use ``ct-cleanup-locks --dry-run`` to identify, then ``ct-cleanup-locks`` to remove.

Multi-User Shared Caches
-------------------------

For team environments with shared build directories:

**Setup:**

1. Create shared cache with SGID bit::

    mkdir -p /shared/build/cache
    chmod 2775 /shared/build/cache  # SGID + group write
    chgrp developers /shared/build/cache

2. Configure compiletools::

    ct-cake --file-locking --cas-objdir=/shared/build/cache

**Lock permissions:**

- Lockdirs inherit group from parent (via SGID)
- 775 permissions allow group members to remove stale locks
- PID files are 664 for group readability

**Maintenance:**

Run periodic cleanup of stale locks::

    ct-cleanup-locks --cas-objdir=/shared/build/cache --dry-run
    ct-cleanup-locks --cas-objdir=/shared/build/cache

See Also
--------

- ``ct-cleanup-locks`` - Remove stale locks from shared caches
- ``ct-cake --help`` - Build system documentation
- ``README.ct-doc.rst`` - Main compiletools documentation

Algorithm Details
-----------------

The locking algorithms are implemented in ``locking.py`` and shared between
ct-lock-helper (Make/Ninja backends) and the Shake backend:

1. **Acquire:**

   - Try ``mkdir`` (lockdir), ``fcntl.lockf`` on ``<target>.lock`` (fcntl),
     exclusive create of ``<target>.lock.excl`` (cifs), or ``flock`` on
     ``<target>.lock`` (flock)
   - For lockdir: if fails, check if stale (same-host process check), remove and retry
   - For fcntl: kernel handles blocking and deadlock avoidance
   - For cifs: same-host stale ``.lock.excl`` holders are removed; cross-host holders wait
   - If not stale, wait with periodic warnings
   - Write hostname:pid:start-time holder info for lockdir and CIFS locks

2. **Execute:**

   - Helper ``compile`` always writes ``<target>.<pid>.<random>.tmp`` and
     atomically replaces the target after the compiler succeeds
   - Helper ``link`` / ``ar`` uses the same temp-and-replace pattern when the
     target appears in a recognized command form
   - Native ``flock`` compile fast path writes ``<target>.compiletools.tmp``
     and moves it into place
   - Native ``flock`` link fast path only locks ``<target>.lock`` and runs the
     link command unchanged

3. **Release:**

   - lockdir removes its pid file and lock directory
   - cifs removes ``<target>.lock.excl`` and leaves ``<target>.lock``
   - fcntl/flock unlock and close ``<target>.lock``; the sidecar file remains
   - Cleanup via signal handlers on SIGINT/SIGTERM

Examples
--------

**Manual invocation:**

::

    # Compile with lockdir strategy
    ct-lock-helper compile --target=main.o --strategy=lockdir -- gcc -c main.c

    # Compile with cifs strategy and custom timeout
    CT_LOCK_TIMEOUT=120 ct-lock-helper compile --target=test.o --strategy=cifs -- gcc -c test.c

**Debugging lock contention:**

::

    # Verbose output
    CT_LOCK_VERBOSE=1 CT_LOCK_WARN_INTERVAL=5 ct-cake --file-locking

**Testing lock strategies:**

::

    # Force specific strategy (override auto-detection)
    ct-lock-helper compile --target=file.o --strategy=flock -- gcc -c file.c

Installation
------------

ct-lock-helper is installed automatically with compiletools::

    pip install compiletools

It will be in your PATH if the Python scripts directory is in PATH
(e.g., ``~/.local/bin`` or virtual environment's ``bin/``).

For development::

    pip install -e .
    # or
    pip install -e ".[dev]"
