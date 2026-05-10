.. image:: https://github.com/DrGeoff/compiletools/actions/workflows/main.yml/badge.svg
    :target: https://github.com/DrGeoff/compiletools/actions
    :alt: Build Status

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
:Version: 9.1.0
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

KEY FEATURES
============

**Magic Comments**
    Embed build requirements directly in source files using special comments
    like ``//#LDFLAGS=-lpthread`` or ``//#PKG-CONFIG=zlib``. See ct-magicflags(1).

**Automatic Dependency Detection**
    Traces #include statements to determine what to compile and link.
    No manual dependency lists needed.

**Build Variants**
    Support for debug, release, and custom build configurations.
    Use ``--variant=release`` to select. See ct-config(1).

**Pluggable Build Backends**
    Choose from Make (default), Ninja, CMake, Bazel, Tup, or the builtin
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
    Summarize content-addressable cache occupancy.

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
    List available build backends (make, ninja, cmake, bazel, shake, tup).

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
* Current working directory
* Environment variables (capitalized, e.g., VARIANT=release)
* Command-line arguments

Build variants (debug, release, etc.) are config profiles specifying compiler and
flags. Common variants include blank (default debug), blank.release, gcc.debug,
gcc.release, clang.debug, clang.release.

Common usage:

.. code-block:: bash

    ct-cake --variant=release
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
