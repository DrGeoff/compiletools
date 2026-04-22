============
ct-backends
============

------------------------------------------------------------
Build backend architecture and selection guide
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-04-02
:Version: 8.1.0
:Manual section: 7
:Manual group: developers

DESCRIPTION
===========

compiletools supports multiple build backends that generate native build files
for different build systems.  All backends share the same dependency analysis,
magic flag extraction, and header dependency tracking — only the build file
generation and execution differ.

Select a backend with::

    ct-cake --backend=<name>

Use ``ct-list-backends`` to discover which backends are installed and available.

BACKEND SUMMARY
===============

======  ======================  ===========
Name    Build file              Tool
======  ======================  ===========
make    Makefile                make
ninja   build.ninja             ninja
cmake   CMakeLists.txt          cmake
bazel   BUILD.bazel             bazel
tup     Tupfile                 tup
shake   .ct-traces.json         (builtin)
slurm   .ct-slurm-traces.json   sbatch
======  ======================  ===========

FEATURE MATRIX
==============

Y = supported, N = not supported, D = supported and on by default.

======  ===========  =====================  ============  =========  =========  ============  ==========  ============
Name    File-lock    --build-only-changed   --realclean   PCH cache  Libraries  Test runner   CA outputs  Early cutoff
======  ===========  =====================  ============  =========  =========  ============  ==========  ============
make    D            Y                      Y             Y          Y          Y             N           N
ninja   Y            Y                      Y             Y          Y          Y             N           Y (restat)
cmake   N            Y                      Y             Y          Y          Y             N           N
bazel   N            Y                      Y             Y          Y          Y             N           N
tup     N            Y                      Y             Y          Y          N (no phony)  N           N
shake   Y            Y                      Y             Y          Y          Y             Y           Y
slurm   Y            Y                      Y             Y          Y (local)  Y             Y           Y
======  ===========  =====================  ============  =========  =========  ============  ==========  ============

Notes:

* **File-lock** — ``--file-locking`` enables multi-user shared object/PCH
  caches.  CMake/Bazel/Tup manage their own coordination and skip this
  layer (see *FILE LOCKING* below).
* **--build-only-changed** — restrict the graph to a whitespace-separated
  set of source files supplied on the command line.  Implemented in the
  shared base class, so all backends honor it.
* **--realclean** — selectively remove only this build's products from a
  shared objdir (and ``.gch`` files from a shared pchdir), instead of
  ``rm -rf`` of the whole tree.  Inherited from the base backend; the
  Make backend additionally generates a ``realclean`` rule in the Makefile.
* **PCH cache** — content-addressable precompiled header cache via
  ``--pchdir``; the compile rules carry the per-cmd_hash ``.gch`` paths,
  so all backends produce and consume the cache transparently.
* **Libraries** — static and dynamic library targets via ``--static`` /
  ``--dynamic``.  All backends support both; the Slurm backend
  distributes compile rules but always links locally.
* **Test runner** — ``execute("runtests")`` runs test executables.
  Tup cannot express phony rules, so it has no top-level ``runtests``
  target — run test binaries by hand or pick a different backend.
* **CA outputs / Early cutoff** — content-addressable filenames and
  build skipping when an output's bytes are unchanged.  Native to the
  Shake/Slurm builtin engine; Ninja approximates early cutoff via
  ``restat = 1``.

BACKENDS
========

make (default)
--------------

Generates a non-recursive Makefile following Peter Miller's *Recursive Make
Considered Harmful* design.

**When to use:** General-purpose default.  Widely available, well-understood
output, good diagnostic support via ``--trace`` (GNU Make 4.0+).

**Features:**

- File locking enabled by default for multi-user shared caches
- ``.DELETE_ON_ERROR`` to clean partial outputs on failure
- Implicit rules disabled (``-rR``) for predictable behaviour
- Output synchronisation per target (``--output-sync=target``, Make 4.0+)
- ``--shuffle`` support (Make 4.4+) to detect missing dependencies in CI
- Selective build via ``--build-only-changed``
- Wraps compile and link commands with ``ct-lock-helper`` when file locking
  is enabled

**Requires:** GNU Make (``make`` in PATH).

ninja
-----

Generates a ``build.ninja`` file targeting the Ninja build system.

**When to use:** Large incremental rebuilds where Ninja's minimal overhead
outperforms Make.  Ninja re-evaluates the build graph faster than Make on
projects with many targets.

**Features:**

- Depfile tracking via ``-MMD -MF`` and Ninja's ``deps = gcc`` integration
- ``restat = 1`` on rules to support early cutoff when outputs are unchanged
- File locking via ``ct-lock-helper`` wrappers (optional)
- Selective build via ``--build-only-changed``

**Requires:** Ninja (``ninja`` in PATH).

cmake
-----

Generates a ``CMakeLists.txt`` and builds via CMake in an out-of-source
build directory (``{objdir}/cmake-build``).

**When to use:** Integration with CMake-based toolchains or IDEs that expect
CMake projects.  Useful when downstream consumers need a ``CMakeLists.txt``.

**Features:**

- Aggregates low-level compile/link rules into high-level CMake targets
  (``add_executable``, ``add_library``)
- Separates flags into ``target_include_directories``,
  ``target_compile_options``, ``target_link_directories``,
  ``target_link_libraries``, and ``target_link_options``
- Automatically detects C, C++, or both languages
- Passes user-configured ``CC`` and ``CXX`` to CMake
- Enforces out-of-source builds

**Requires:** CMake 3.15+ (``cmake`` in PATH).

**Limitations:** File locking is disabled — CMake manages its own
coordination.

bazel
-----

Generates ``BUILD.bazel``, ``WORKSPACE``, and ``MODULE.bazel`` files
targeting Bazel.

**When to use:** Projects that need Bazel's hermeticity, remote caching,
or remote execution capabilities.

**Features:**

- Aggregates rules into ``cc_binary`` and ``cc_library`` targets using
  ``@rules_cc``
- Strips ``-I`` paths (Bazel manages includes internally)
- Resolves relative ``-L`` paths to absolute for Bazel's sandbox
- Sources outside the workspace are copied to ``ext/``
- Outputs in ``bazel-bin/`` are copied to the final destination

**Requires:** Bazel or Bazelisk (``bazel`` or ``bazelisk`` in PATH).

**Limitations:** File locking is disabled — Bazel manages its own
sandboxing and caching.

tup
---

Generates a ``Tupfile`` targeting the Tup build system.

**When to use:** Projects where Tup's file-system monitoring provides
fast incremental rebuilds without explicit dependency declarations.

**Features:**

- Simple rule format: ``: inputs |> command |> outputs``
- No explicit depfile tracking — Tup monitors the filesystem directly
- Automatic ``.tup/`` initialisation on first run

**Requires:** Tup (``tup`` in PATH).

**Limitations:**

- File locking is disabled (Tup manages file monitoring internally)
- No phony target support — top-level ``build`` and ``runtests`` targets
  are unavailable

shake (builtin)
---------------

A self-executing backend implementing the *Build Systems a la Carte*
(Mokhov, Mitchell, Jones 2018) rebuild strategy with verifying traces and
early cutoff.  No external build tool is required.

**When to use:** Best rebuild precision.  Ideal for large projects where
avoiding unnecessary rebuilds matters most, and for shared caches on
network filesystems where content-addressable outputs prevent stale reuse.

**Features:**

- **Content-addressable outputs:** compile rules produce files whose names
  encode source hash, dependency hash, and macro state — existence implies
  correctness, skipping expensive content checks
- **Verifying traces:** records content hashes of all inputs, outputs, and
  commands in ``.ct-traces.json``; on rebuild, verifies all input hashes
  before re-executing
- **Early cutoff:** if a rebuilt output is byte-identical to the previous
  version, dependents are not rebuilt
- Async execution with ``asyncio.Semaphore`` limiting concurrency
- File locking via ``FileLock`` for multi-user shared caches
- Atomic output creation via temp file + rename

**Requires:** Nothing — builtin to compiletools.

slurm (HPC)
------------

Extends the Shake backend to distribute compile rules across an HPC cluster
via Slurm job arrays.  Link and library rules still run locally.

**When to use:** Large codebases on HPC clusters where distributing
compilation across many nodes dramatically reduces wall-clock time.

**Features:**

- Distributes compile rules via ``sbatch --array``
- Dynamic memory estimation per rule based on include complexity
- Memory tiers with automatic OOM retry (doubles memory and resubmits)
- Job chunking for large submissions (``--slurm-max-array``, default 1000)
- Polls ``sacct`` for job completion
- Traces recorded for successfully compiled outputs (incremental rebuilds
  across cluster jobs)
- Slurm logs written to ``{objdir}/slurm-ct-*.out`` (cleaned on success)

**Requires:** Slurm (``sbatch`` in PATH).

**Configuration options:**

- ``--slurm-partition`` — Slurm partition name
- ``--slurm-account`` — Slurm account for billing
- ``--slurm-time`` — job time limit
- ``--slurm-mem`` — maximum memory per job (also the OOM retry ceiling)
- ``--slurm-cpus`` — CPUs per task
- ``--slurm-poll-interval`` — seconds between ``sacct`` polls
- ``--slurm-export`` — comma-separated env vars passed via ``sbatch --export=``.
  Default propagates a curated allowlist
  (``PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH``) instead of the
  submitter's full environment, so unrelated state does not leak into compute
  nodes.  ``LD_LIBRARY_PATH`` is included because non-system compilers
  (Spack, Lmod, custom installs) typically need it to locate their shared libs.

**Overriding the default export list:**

- ``--slurm-export=ALL`` — propagate the full submitter environment (legacy
  behavior; use sparingly, leaks unrelated state).
- ``--slurm-export=NONE`` — fully isolated environment.
- For Lmod/Spack sites, extend the default explicitly, e.g.::

      --slurm-export=PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH,MODULEPATH,LMOD_CMD,LMOD_DIR,SPACK_ROOT

**File-locking semantics:** For the slurm backend, "file locking" means
(a) local link/library steps go through ``FileLock`` + ``atomic_link`` on
the submitter, and (b) compute-node compiles use atomic temp+rename
(compile to ``$OUT.$SLURM_JOB_ID.$SLURM_ARRAY_TASK_ID.tmp``, then
``mv -f`` onto the final ``.o``) -- the same correctness guarantee that
``atomic_compile`` provides locally, without requiring a cross-node lock
on the compute path.

**Limitations:** Link rules cannot be distributed and always run locally.

FILE LOCKING
============

File locking enables multiple users and build hosts to share compiled object
files safely.  It is supported by the Make, Ninja, Shake, and Slurm backends.

Enable with ``--file-locking`` (on by default for Make).  The locking
strategy is auto-detected based on the target filesystem:

- **lockdir** — NFS, Lustre (mkdir-based, works everywhere)
- **fcntl** — GPFS (kernel-managed cross-node locks)
- **cifs** — CIFS/SMB (exclusive file creation)
- **flock** — local filesystems like ext4, xfs, btrfs

See ``ct-lock-helper`` (1) and ``ct-cleanup-locks`` (1) for details.

CONTENT-ADDRESSABLE OUTPUTS
============================

The Shake and Slurm backends use content-addressable object file naming::

    basename_filehash_dephash_macrohash.o

Because the filename encodes all inputs (source content, dependency hashes,
and macro state), if the file exists it is guaranteed correct.  This
eliminates the need for content verification on cache hits and prevents
stale reuse on low-resolution filesystems.

CHOOSING A BACKEND
==================

=================================  ==================================
Scenario                           Recommended backend
=================================  ==================================
General-purpose builds             ``make`` (default)
Large incremental rebuilds         ``ninja``
Shared multi-user caches           ``shake`` or ``make --file-locking``
HPC cluster distribution           ``slurm``
CMake IDE integration              ``cmake``
Bazel ecosystem integration        ``bazel``
Filesystem-monitored rebuilds      ``tup``
Maximum rebuild precision          ``shake``
=================================  ==================================

EXAMPLES
========

Build with Ninja backend::

    ct-cake --backend=ninja

Build with Shake backend and file locking::

    ct-cake --backend=shake --file-locking

Distribute compilation across a Slurm cluster::

    ct-cake --backend=slurm --slurm-partition=build --slurm-mem=8G

List available backends::

    ct-list-backends
    ct-list-backends --all --style=pretty

REFERENCES
==========

* Andrey Mokhov, Neil Mitchell, Simon Peyton Jones. *Build Systems a la Carte*.
  Proc. ACM Program. Lang., Vol. 2, ICFP, Article 79, September 2018.
  https://doi.org/10.1145/3236774

* Peter Miller. *Recursive Make Considered Harmful*. 2008.
  https://api.semanticscholar.org/CorpusID:54117644

SEE ALSO
========
``ct-cake`` (1), ``ct-list-backends`` (1), ``ct-lock-helper`` (1),
``ct-cleanup-locks`` (1)
