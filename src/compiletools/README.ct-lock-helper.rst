==============
ct-lock-helper
==============

------------------------------------------------------------
Helper script for file locking during concurrent compilation
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2024-01-01
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-lock-helper compile --target=OUTPUT --strategy=STRATEGY -- COMMAND

DESCRIPTION
===========

``ct-lock-helper`` manages file locks when building with shared object caching
(``--shared-objects`` flag). It wraps compilation commands to ensure atomic
file creation and prevent race conditions in multi-user or parallel build
environments.

The helper implements three locking strategies automatically selected based on
the target filesystem type:

- **lockdir**: For NFS, GPFS, Lustre (mkdir-based, works across all filesystems)
- **cifs**: For CIFS/SMB (exclusive file creation)
- **flock**: For local filesystems like ext4, xfs, btrfs (POSIX flock)

Usage
-----

ct-lock-helper is invoked automatically by ``ct-cake`` when using ``--shared-objects``.
You typically don't call it directly, but it's useful to understand for debugging.

Basic command format::

    ct-lock-helper compile --target=OUTPUT.o --strategy=STRATEGY -- COMPILE_COMMAND

Example::

    ct-lock-helper compile --target=file.o --strategy=lockdir -- gcc -c file.c

The helper will:

1. Acquire lock based on strategy
2. Create temporary file (``file.o.PID.RANDOM.tmp``)
3. Execute: ``gcc -c file.c -o file.o.PID.RANDOM.tmp``
4. Move temp to target: ``mv file.o.PID.RANDOM.tmp file.o``
5. Release lock

Configuration
-------------

Environment variables control lock behavior:

**CT_LOCK_SLEEP_INTERVAL**
    Seconds to sleep between lock acquisition attempts for lockdir strategy (default: 0.05)

    - Lustre filesystems: 0.01 (fast parallel filesystem)
    - NFS filesystems: 0.1 (network latency)
    - GPFS and others: 0.05 (balanced)

**CT_LOCK_SLEEP_INTERVAL_CIFS**
    Seconds to sleep between lock acquisition attempts for CIFS strategy (default: 0.1)

**CT_LOCK_SLEEP_INTERVAL_FLOCK**
    Seconds to sleep between lock acquisition attempts for flock strategy (default: 0.1)

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
    ct-cake --shared-objects

Lock Strategies
---------------

lockdir (NFS/GPFS/Lustre)
^^^^^^^^^^^^^^^^^^^^^^^^^^

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

- Same-host: Checks if process alive (``kill -0`` + ``/proc`` check on Linux)
- Cross-host: Cannot verify, relies on age-based timeout warnings

cifs (CIFS/SMB)
^^^^^^^^^^^^^^^

Uses exclusive file creation (``O_CREAT|O_EXCL``) for CIFS compatibility.

**Lock structure:**

::

    target.o.lock        # Base lockfile (fd 9)
    target.o.lock.excl   # Exclusive marker

flock (Local filesystems)
^^^^^^^^^^^^^^^^^^^^^^^^^^

Uses POSIX ``flock()`` when available, falls back to ``O_EXCL`` polling.

**Lock structure:**

::

    target.o.lock        # Lockfile (fd 9)
    target.o.lock.pid    # PID marker (fallback only)

Implementations
---------------

Two implementations are available:

**ct-lock-helper** (bash - default):
- Overhead: ~12-18ms per compilation
- Requires bash
- Faster for simple operations

**ct-lock-helper-py** (Python - alternative):
- Overhead: ~45-52ms per compilation
- Python 3.9+ required
- Better error handling
- Cross-platform (Windows compatible)
- Reuses tested locking.py code

Performance Comparison
^^^^^^^^^^^^^^^^^^^^^^

Measured overhead (vs direct gcc, 100 iterations):

+-----------+---------------+----------------+---------------+
| Strategy  | Bash          | Python         | Difference    |
+===========+===============+================+===============+
| flock     | 12.9ms        | 52.6ms         | **4.1x**      |
+-----------+---------------+----------------+---------------+
| lockdir   | 18.5ms        | 45.7ms         | **2.5x**      |
+-----------+---------------+----------------+---------------+
| cifs      | 11.9ms        | 47.5ms         | **4.0x**      |
+-----------+---------------+----------------+---------------+

**Verdict:** Bash is **2.5-4x faster** than Python.

**When the overhead doesn't matter:**

- Real C/C++ compilation (typically 100ms-10s per file)
- Parallel builds (``make -j8`` amortizes overhead)
- Network filesystems (NFS latency >> 50ms)

**When to use Python version:**

- Windows or non-bash environments
- Better error messages/debugging needed
- Cross-platform consistency required
- Overhead is acceptable (< 50% of compile time)

**When shared objects are beneficial:**

- Multi-user team builds with shared cache
- Parallel builds on NFS/GPFS/Lustre
- CI/CD with persistent object directories

**When to skip:**

- Fast local single-threaded builds
- Many tiny files (<100ms compile time each)
- Use ``--no-shared-objects`` to disable

**Filesystem detection:**

Strategy is determined once in Python and baked into Makefile.
No per-compilation filesystem detection overhead.

**Benchmark:**

Run your own performance comparison::

    # Available after installation
    benchmark_lock_implementations.sh

Troubleshooting
---------------

**"ct-lock-helper not found in PATH"**

Solutions:

1. Install compiletools: ``pip install compiletools``
2. Install from source: ``pip install -e .``
3. Add to PATH: ``export PATH=/path/to/compiletools:$PATH``
4. Disable shared objects: use ``--no-shared-objects``

**Locks not releasing**

Check for:

- Killed processes: Use ``ct-cleanup-locks`` to remove stale locks
- Permission issues: Ensure parent directory has SGID bit and group write
- Network issues: Check NFS mount status

**Slow builds with locking**

ct-lock-helper adds ~13-17ms overhead per compilation due to process spawn.
This is negligible for real C/C++ files (100ms-10s compile time) but may be
noticeable for many tiny files.

Solutions:

- Adjust sleep intervals::

    export CT_LOCK_SLEEP_INTERVAL=0.01      # For lockdir on Lustre
    export CT_LOCK_SLEEP_INTERVAL_CIFS=0.05 # For CIFS strategy
    export CT_LOCK_SLEEP_INTERVAL_FLOCK=0.05 # For flock strategy

- For very fast local-only builds, consider ``--no-shared-objects``

**Cross-host lock stuck**

If a remote host crashes, locks must be manually removed::

    rm -rf /path/to/target.o.lockdir

Or use ``ct-cleanup-locks --dry-run`` to identify, then ``ct-cleanup-locks`` to remove.

Multi-User Shared Caches
-------------------------

For team environments with shared object directories:

**Setup:**

1. Create shared cache with SGID bit::

    mkdir -p /shared/build/cache
    chmod 2775 /shared/build/cache  # SGID + group write
    chgrp developers /shared/build/cache

2. Configure compiletools::

    ct-cake --shared-objects --objdir=/shared/build/cache

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

The locking algorithm mirrors ``locking.py`` for consistency:

1. **Acquire:**

   - Try ``mkdir`` (lockdir) or exclusive create (cifs/flock)
   - If fails, check if lock is stale (same-host process check)
   - If stale, remove and retry immediately
   - If not stale, wait with periodic warnings
   - Write hostname:pid to lock

2. **Execute:**

   - Compile to temporary file
   - Exit immediately on compile errors (``set -euo pipefail``)

3. **Release:**

   - Move temp to target (atomic)
   - Remove lock files
   - Cleanup via trap on EXIT/INT/TERM

**Error handling:**

- All errors propagate (``set -euo pipefail``)
- Locks released even on signals (trap)
- Temp files cleaned up on exit

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
    CT_LOCK_VERBOSE=1 CT_LOCK_WARN_INTERVAL=5 ct-cake --shared-objects

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

The script is located at the repository root: ``compiletools/ct-lock-helper``
