===================
ct-create-makefile
===================

-----------------------------------------
Generate Makefile from compiletools magic
-----------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2024-11-24
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-create-makefile [options] [--dynamic lib.cpp] [--static lib.cpp] [--tests test.cpp] filename.cpp

DESCRIPTION
===========
ct-create-makefile generates a Makefile from compiletools magic comments and
automatic dependency detection. This is the underlying tool used by ct-cake
and the ct-build shell scripts.

Unlike ct-cake, which generates and immediately executes the Makefile,
ct-create-makefile only generates the Makefile. This is useful when you need
to customize the build process or integrate with other build systems.

The tool analyzes source files for:

* Header dependencies (automatic)
* Magic flags (//#LDFLAGS, //#CXXFLAGS, //#PKG-CONFIG, etc.)
* Library requirements

OPTIONS
=======

--dynamic LIB.cpp
    Build a dynamic/shared library from the specified source file.
    Multiple --dynamic options can be specified.

--static LIB.cpp
    Build a static library from the specified source file.
    Multiple --static options can be specified.

--tests TEST.cpp
    Build and register test executables. Multiple --tests options
    can be specified.

--makefilename NAME
    Name of the generated Makefile. Default: Makefile

--variant VARIANT
    Use the specified build variant (e.g., gcc.debug, gcc.release).
    Default: blank

--bindir DIR
    Output directory for built binaries. Default: bin/<variant>

--objdir DIR
    Output directory for object files. Default: bin/<variant>/obj

--shared-objects
    Enable shared object cache for faster rebuilds across projects.
    Uses content-addressable storage with proper locking.

--no-shared-objects
    Disable shared object cache. Default.

--serialise-tests
    Run tests sequentially rather than in parallel.

--no-serialise-tests
    Run tests in parallel. Default.

--build-only-changed FILES
    Only build binaries depending on the specified source or header files.
    FILES is a space-delimited list of absolute paths to changed files.

--project-version VERSION
    Set the project version string.

--project-version-cmd CMD
    Command to run to determine project version (e.g., "git describe").

-v, --verbose
    Increase output verbosity. Use multiple times for more detail.

--help, -h
    Show help message and exit.

EXAMPLE
=======

Generate a Makefile for a simple executable:

.. code-block:: bash

    ct-create-makefile main.cpp

Generate Makefile and then build:

.. code-block:: bash

    ct-create-makefile main.cpp
    make -j$(nproc)

Generate with a shared library:

.. code-block:: bash

    ct-create-makefile --dynamic mylib.cpp main.cpp
    make

Generate for release variant with shared object cache:

.. code-block:: bash

    ct-create-makefile --variant=release --shared-objects main.cpp
    make

COMPARISON WITH CT-CAKE
=======================

ct-cake and ct-create-makefile serve different purposes:

* **ct-cake**: All-in-one tool that finds targets, generates Makefile, and
  runs make. Use for quick builds during development.

* **ct-create-makefile**: Only generates the Makefile. Use when you need
  more control over the build process, want to customize the Makefile,
  or integrate with other build systems.

The ct-build shell scripts use ct-create-makefile internally:

.. code-block:: bash

    # ct-build is roughly equivalent to:
    ct-create-makefile $(ct-findtargets --style=args) "$@"
    make -j$(ct-jobs)

SEE ALSO
========
``ct-cake`` (1), ``ct-findtargets`` (1), ``ct-magicflags`` (1)
