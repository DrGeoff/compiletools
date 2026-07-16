.. image:: https://github.com/DrGeoff/compiletools/actions/workflows/ci.yml/badge.svg
    :target: https://github.com/DrGeoff/compiletools/actions/workflows/ci.yml
    :alt: Build Status

.. image:: https://raw.githubusercontent.com/DrGeoff/compiletools/badges/coverage.svg
    :target: https://github.com/DrGeoff/compiletools/actions/workflows/ci.yml
    :alt: Coverage

============
compiletools
============

--------------------------------------------------------
C/C++ build tools that requires almost no configuration.
--------------------------------------------------------

:NOTE: The repository-level ``README.rst`` is a symlink to this file, so they
       are the same canonical document.

:Author: drgeoffathome@gmail.com
:Date:   2026-05-09
:Version: 10.3.0
:Manual section: 1
:Manual group: developers


SYNOPSIS
========
    ct-* [compilation args] [filename.cpp] [--variant=<VARIANT>]

DESCRIPTION
===========
compiletools provides C/C++ build automation with minimal configuration. The tools
automatically determine source files, dependencies, and build requirements by
analyzing your code.

To build a C or C++ project, simply type:

.. code-block:: bash

    ct-cake

This automatically determines source files, builds executables, and runs tests.
See ct-cake(1) for details.

QUICK START
===========

Try compiletools without installing using uvx:

.. code-block:: bash

    uvx --from compiletools ct-cake
    uvx --from compiletools ct-compilation-database

This runs tools directly without affecting your system. All ct-* tools work
with uvx (e.g., ``uvx --from compiletools ct-config``).

INSTALLATION
============

.. code-block:: bash

    uv pip install compiletools

Or for development:

.. code-block:: bash

    git clone https://github.com/DrGeoff/compiletools
    cd compiletools
    uv pip install -e ".[dev]"

EXAMPLES
========

Two example trees ship with the package:

* ``src/compiletools/examples-end-to-end/`` — buildable mini-projects
  exercising the full ``ct-cake --auto`` pipeline (PCH, C++20 modules,
  static / dynamic libraries, multi-target apps, sanitizer variants,
  pkg-config, …). Each has a ``README`` describing what it demonstrates;
  ``cd`` into one and run ``ct-cake`` to see the build flow end-to-end.

* ``src/compiletools/examples-features/`` — focused, fixture-style
  projects for individual magic-flag annotations and config features
  (``//#PKG-CONFIG=``, ``//#LDFLAGS=``, ``//#PCH=``, ``//#GIT=``,
  append-style variables, …). Useful as copy-paste templates when adding
  the same feature to your own project.

The ``examples_registry`` Python module (``example_path()``,
``example_file()``) maps these trees from test code; tests under
``src/compiletools/test_*.py`` use them as fixtures, so each example is
also a worked CI-verified configuration.

KEY FEATURES
============

**Magic Comments**
    Embed build requirements directly in source files using special comments
    like ``//#LDFLAGS=-lpthread`` or ``//#PKG-CONFIG=zlib``, or pull in an
    external git repository with ``//#GIT=<url>``. See ct-magicflags(1) and
    the worked example in ``src/compiletools/examples-end-to-end/sudoku_tui/``.

**Automatic Dependency Detection**
    Traces #include statements to determine what to compile and link.
    No manual dependency lists needed.

**Build Variants**
    Support for debug, release, and custom build configurations.
    Use ``--variant=release`` to select. See ct-config(1).

**Pluggable Build Backends**
    Choose from Make (default), Ninja, CMake, Bazel, or the builtin
    Shake backend.  Distribute compilation across an HPC cluster with the
    Slurm backend.  Use ``--backend=<name>`` to select.  See ct-backends(7).

**Content-Addressable Caching**
    Objects, precompiled headers, C++20 module BMIs, and the linker
    artefacts themselves (executables, static libs, shared libs) are
    cached in per-variant content-addressable directories
    (``cas-objdir``, ``cas-pchdir``, ``cas-pcmdir``, ``cas-exedir``).
    Cache keys are anchored to the git root, so identical translation
    units share entries even when the workspace is moved or cloned to
    a new path. Combined with the default ``--use-mtime=False``,
    fresh-checkout CI builds (where every source has ``mtime=now``)
    hit the cache instead of re-running the producer. Trim with
    ``ct-trim-cache``.

**C++20 Modules**
    First-class support for clang ``.pcm`` and gcc ``.gcm`` BMI artefacts
    -- including named modules, partitions, header units, and ``import std``
    -- with automatic discovery from ``import`` / ``export module`` in
    your sources. See the C++20 Modules Caching section in ct-cake(1).

**Precompiled Headers**
    Mark headers with ``//#PCH=`` and ct-cake builds them once and shares
    them across the project via ``cas-pchdir``.

**File Locking**
    Multi-user/multi-host object file caching with filesystem-aware locking
    for faster builds in team environments. Enable with ``file-locking = true``.

**Minimal Configuration**
    Works out-of-the-box with sensible defaults. Configuration only needed
    for customization.

CORE TOOLS
==========

**ct-cake**
    Main build tool. Auto-detects targets, builds executables, runs tests.

**ct-compilation-database**
    Generate compile_commands.json for IDE integration. Auto-detects targets.

**ct-config**
    Inspect configuration resolution and available compilation variants.

**ct-magicflags**
    Show magic flags extracted from source files.

**ct-fetch**
    Clone, update, or report the ``//#GIT=`` external git repositories a
    target tree depends on, without running a build.

**ct-headertree**
    Visualize include dependency structure.

**ct-filelist**
    Generate file lists for packaging and distribution.

**ct-timing-report**
    Analyze build timing data from ``ct-cake --timing``.  Interactive TUI,
    static summary, run comparison, and Chrome Trace export.

**ct-trim-cache**
    Trim aged entries from the object, PCH, PCM, and linker-artefact
    content-addressable caches with configurable retention.

**ct-cache-report**
    Summarize content-addressable cache occupancy and flag duplication
    in the object, PCH, PCM, and linker-artefact caches.

**ct-debug-pcm-hash-inputs**
    Dump the seven inputs that drive a C++20 module BMI's
    ``<cas-pcmdir>/<cmd_hash>/`` cache path as JSON. Run twice across
    back-to-back ``ct-cake`` invocations and ``diff`` the outputs to
    identify which input drifted when a BMI lands under a fresh subdir
    on unchanged source.

**ct-cas-publish**
    Helper invoked from generated build recipes: atomically publish a
    cas-exedir entry to a user-facing ``bin/<variant>/<name>`` path
    via ``link()`` + ``rename()``, with ``EXDEV``-only symlink
    fallback. Not normally run by hand.

**ct-cleanup-locks**
    Clean stale locks from file locking.

**ct-check-venv**
    Verify that the ``ct-cake`` on PATH imports the same ``compiletools``
    as the active venv. Run after ``git worktree add`` and a fresh
    ``uv pip install -e .`` to confirm subprocess-driven tools see the
    expected source tree.

**ct-list-backends**
    List available build backends (make, ninja, cmake, bazel, shake).

**Shell Wrappers**
    Convenience scripts in ``scripts/``: ct-build, ct-build-static-library,
    ct-build-dynamic-library, ct-watch-build, ct-lock-helper, ct-release.

CONFIGURATION
=============
Options are parsed using ConfigArgParse, allowing configuration via command line,
environment variables, or config files.

Configuration hierarchy (lowest to highest priority):

* Executable directory (ct/ct.conf.d alongside the ct-* executable)
* System config (/etc/xdg/ct/)
* Python virtual environment (${python-site-packages}/ct/ct.conf.d)
* Package bundled config (<installed-package>/ct.conf.d)
* User config (~/.config/ct/)
* Project config (<gitroot>/ct.conf.d/)
* Git repository root directory
* Current working directory, and the nearest-ancestor ct.conf / ct.conf.d
  of every explicit or --auto-discovered build target (target-anchored
  layers; same priority tier as the current working directory)
* Environment variables (capitalized, e.g., VARIANT=release)
* Command-line arguments

All loaded configuration applies to the whole invocation — there is no
per-translation-unit scoping. Because target-anchored layers and the
current-working-directory layer share one tier, they must agree: if two
same-tier config files set the same key to different values (including
append-/prepend- keys), the tools stop with an error naming both files and
the two remedies (build the subprojects separately, or make the values
identical).

For a target outside any git repository (or under ``--no-git-root``) the
ancestor walk is bounded only by the filesystem root; a layer it finds at or
above your home directory triggers an unconditional ``ct: warning:`` because
such a file (e.g. a forgotten ``~/ct.conf``) is almost certainly stray —
legitimate user-level config belongs in ``~/.config/ct/``.

Build variants are composed from *axis* conf files — one per orthogonal
concern: toolchain (``gcc``, ``clang``), linker (``ld``, ``gold``, ``mold``,
``wild``), optimization (``debug``, ``release``), instrumentation
(``asan``, ``ubsan``, ``tsan``, ``coverage``, ``lto``).
``--variant=gcc,debug,asan`` (or ``gcc.debug.asan`` or ``gcc debug asan``
— comma, dot, and whitespace are all equivalent) synthesizes the
composition from ``gcc.conf`` + ``debug.conf`` + ``asan.conf``. No per-
combination conf file is required.

Common usage:

.. code-block:: bash

    ct-cake --variant=gcc,release             # toolchain + opt
    ct-cake --variant=clang,debug,asan        # toolchain + opt + sanitizer
    ct-cake --variant=gcc,mold,release,lto    # everything composes
    ct-cake --append-CXXFLAGS="-march=native"

For details on configuration hierarchy, file format, and variant system, see ct-config(1).

ATTRIBUTION
===========
This project is derived from the original compiletools developed at Zomojo Pty Ltd
(between 2011-2019). Zomojo ceased operations in February 2020. This repository
continues the development and maintenance of the compiletools project.

SEE ALSO
========
* ct-backends (7) -- build backend architecture and selection guide
* ct-build
* ct-build-docs
* ct-build-dynamic-library
* ct-build-static-library
* ct-cake
* ct-cas-publish
* ct-check-venv
* ct-cleanup-locks
* ct-compilation-database
* ct-config
* ct-cppdeps
* ct-create-makefile
* ct-debug-pcm-hash-inputs
* ct-fetch
* ct-filelist
* ct-findtargets
* ct-git-sha-report
* ct-gitroot
* ct-headertree
* ct-jobs
* ct-list-backends
* ct-list-variants
* ct-lock-helper
* ct-magicflags
* ct-pytest-monitor
* ct-release
* ct-timing-report
* ct-trim-cache
* ct-watch-build
