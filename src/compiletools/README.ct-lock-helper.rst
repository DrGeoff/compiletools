==============
ct-lock-helper
==============

------------------------------------------------------------
Helper for file locking during concurrent compilation
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2024-01-01
:Version: 8.0.2
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-lock-helper compile --target=OUTPUT --strategy=STRATEGY -- COMMAND

DESCRIPTION
===========

``ct-lock-helper`` manages file locks when building with file locking
(``--file-locking`` flag). It wraps compilation commands to ensure atomic
file creation and prevent race conditions in multi-user or parallel build
environments.

The helper is a Python entry point that delegates to ``locking.py``'s
``atomic_compile()`` function, reusing the same tested locking algorithms
used by the Shake backend.

The helper implements four locking strategies automatically selected based on
the target filesystem type:

- **lockdir**: For NFS, Lustre (mkdir-based, works across all filesystems)
- **fcntl**: For GPFS (fcntl.lockf, cross-node, kernel-managed)
- **cifs**: For CIFS/SMB (exclusive file creation)
- **flock**: For local filesystems like ext4, xfs, btrfs (POSIX flock, kernel-managed blocking)

Usage
-----

ct-lock-helper is invoked automatically by ``ct-cake`` when using ``--file-locking``
with the Make or Ninja backends.
You typically don't call it directly, but it's useful to understand for debugging.

Basic command format::

    ct-lock-helper compile --target=OUTPUT.o --strategy=STRATEGY -- COMPILE_COMMAND

Example::

    ct-lock-helper compile --target=file.o --strategy=lockdir -- gcc -c file.c

The helper will:

1. Acquire lock based on strategy
2. For fcntl/flock: compile directly to target (no temp file)
   For others: compile to temp file (``file.o.PID.RANDOM.tmp``), then rename to target
3. Release lock

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
        pid              # Contains "hostname:12345"

**Stale lock handling:**

- Same-host: Checks if process alive (psutil)
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
- Locks the target ``.o`` file directly — no sidecar ``.lock`` file
- Compiles directly to target (no temp file, no rename)

**Lock structure:**

::

    target.o             # Locked directly via fcntl (no sidecar files)

The fcntl advisory lock is placed on the target file itself. Since gcc opens
the output with ``O_WRONLY|O_CREAT|O_TRUNC``, which preserves the inode, the
lock held by the build process remains valid throughout compilation.

cifs (CIFS/SMB)
^^^^^^^^^^^^^^^

Uses exclusive file creation (``O_CREAT|O_EXCL``) for CIFS compatibility.

**Lock structure:**

::

    target.o.lock        # Base lockfile
    target.o.lock.excl   # Exclusive marker

flock (Local filesystems)
^^^^^^^^^^^^^^^^^^^^^^^^^^

Uses POSIX ``flock()`` for kernel-managed blocking. Only used on local
filesystems (ext4/xfs/btrfs) where ``flock()`` is always available.

**Features:**

- Kernel-managed blocking: ``flock(LOCK_EX)`` blocks until acquired
- Automatic release on process death
- Locks the target ``.o`` file directly — no sidecar ``.lock`` file
- Compiles directly to target (no temp file, no rename)

**Lock structure:**

::

    target.o             # Locked directly via flock (no sidecar files)

The flock advisory lock is placed on the target file itself. Since gcc opens
the output with ``O_WRONLY|O_CREAT|O_TRUNC``, which preserves the inode, the
lock held by the build process remains valid throughout compilation. Same
reasoning as the fcntl strategy.

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

    ct-cake --file-locking --objdir=/shared/build/cache

**Lock permissions:**

- Lockdirs inherit group from parent (via SGID)
- 775 permissions allow group members to remove stale locks
- PID files are 664 for group readability

**Maintenance:**

Run periodic cleanup of stale locks::

    ct-cleanup-locks --objdir=/shared/build/cache --dry-run
    ct-cleanup-locks --objdir=/shared/build/cache

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

   - Try ``mkdir`` (lockdir), ``fcntl.lockf`` (fcntl), or exclusive create (cifs/flock)
   - For lockdir: if fails, check if stale (same-host process check), remove and retry
   - For fcntl: kernel handles blocking and deadlock avoidance
   - If not stale, wait with periodic warnings
   - Write hostname:pid to lock

2. **Execute:**

   - For fcntl/flock: compile directly to target (advisory lock protects target)
   - For others: compile to temporary file, then rename to target (atomic)

3. **Release:**

   - Remove lock files (except fcntl/flock, which lock the target directly)
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
