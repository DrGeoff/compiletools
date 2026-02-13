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
:Date:   2025-12-17
:Version: 7.0.2
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

**Shared Object Cache**
    Multi-user/multi-host object file caching with filesystem-aware locking
    for faster builds in team environments. Enable with ``shared-objects = true``.

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

**ct-cleanup-locks**
    Clean stale locks in shared object caches.

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
* ct-build
* ct-build-dynamic-library
* ct-build-static-library
* ct-cake
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
* ct-list-variants
* ct-lock-helper
* ct-magicflags
* ct-release
* ct-watch-build
