================
ct-magicflags
================

------------------------------------------------------------------------
Show the magic flags / magic comments that a file exports
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2018-02-23
:Copyright: Copyright (C) 2011-2018 Zomojo Pty Ltd
:Version: 7.1.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-magicflags [-h] [-c CONFIG_FILE] [--headerdeps {direct,cpp}]
                   [--variant VARIANT] [-v] [-q] [--version] [-?]
                   [--ID ID] [--CPP CPP] [--CC CC]
                   [--CXX CXX] [--CPPFLAGS CPPFLAGS] [--CXXFLAGS CXXFLAGS]
                   [--CFLAGS CFLAGS] [--git-root | --no-git-root]
                   [--include [INCLUDE [INCLUDE ...]]]
                   [--shorten | --no-shorten] [--magic {cpp,direct}]
                   [--style {null,pretty}]
                   filename [filename ...]

DESCRIPTION
===========
ct-magicflags extracts the magic flags/magic comments from a given file.
It is mostly used for debugging purposes so that you can see what the
other compiletools will be using as the magic flags.  A magic flag /
magic comment is simply a C++ style comment that provides information
required to complete the build process.

compiletools works very differently to other build systems, because
compiletools expects that the compiler/link flags will be directly in the
source code. For example, if you have written your own "compress.hpp" that
requires linking against libzip you would normally specify "-lzip" in your
Makefile (or build system) on the link line.  However, compiletools based
applications add the following comment in the file that includes:

.. code-block:: cpp

    //#LDFLAGS=-lzip

For easy maintainence, it is convenient to put the magic flag directly after
the include:

.. code-block:: cpp

    #include <zip.h>
    //#LDFLAGS=-lzip

Whenever "compress.hpp" is included (either directly or indirectly), the
"-lzip" will be automatically added to the link step. If you stop using the
header, for a particular executable, compiletools will figure that out and
stop linking against libzip.

If you want to compile a cpp file with a particular optimization enabled you
would add something like:

.. code-block:: cpp

    //#CXXFLAGS=-fstrict-aliasing

Because the code and build flags are defined so close to each other, it is
much easier to tweak the compilation locally and allow for easier maintainence.

Using PKG-CONFIG
================
Instead of manually specifying compiler and linker flags, you can use pkg-config
to automatically extract the correct flags for a library. For example, with zlib:

.. code-block:: cpp

    //#PKG-CONFIG=zlib
    #include <zlib.h>

This single line automatically adds both the compilation flags (from ``pkg-config --cflags zlib``)
and link flags (from ``pkg-config --libs zlib``) to your build. This approach is
preferred over manual ``LDFLAGS`` because:

* It's more portable across different systems and distributions
* It automatically includes the correct include paths
* It handles library dependencies correctly
* It adapts to different installation locations

The PKG-CONFIG magic flag works with any library that provides a .pc file,
including common libraries like gtk+-3.0, libpng, libcurl, openssl, and many more.

Project-Level PKG-CONFIG Overrides
----------------------------------
If your project needs custom or patched ``.pc`` files (e.g., to pin a library
version, override flags, or provide a package that isn't installed system-wide),
place them in ``ct.conf.d/pkgconfig/`` at the root of your git repository::

    myproject/
        ct.conf.d/
            pkgconfig/
                zlib.pc        # overrides the system zlib.pc
                mylib.pc       # provides a package not installed system-wide
        src/
            main.cpp

compiletools prepends this directory to ``PKG_CONFIG_PATH`` before any
pkg-config invocation, so project-level ``.pc`` files take priority over
system-installed ones. This follows the same layering principle as the rest
of the configuration system (project config overrides system config).

In monorepos, a subdirectory can have its own ``ct.conf.d/pkgconfig/`` that
takes priority over the repo-wide one. When ``ct-cake`` is invoked from a
subdirectory that contains ``ct.conf.d/pkgconfig/``, the resulting
``PKG_CONFIG_PATH`` priority is::

    cwd/ct.conf.d/pkgconfig/  >  gitroot/ct.conf.d/pkgconfig/  >  system

This lets subprojects override repo-wide ``.pc`` files without shell-script
workarounds.

CPPFLAGS and CXXFLAGS Unification
==================================
By default, ``CPPFLAGS`` and ``CXXFLAGS`` magic flags are merged into a single
deduplicated set. A flag set via ``//#CPPFLAGS=`` is also applied as a CXXFLAGS
and vice versa. Use ``--separate-flags-CPP-CXX`` on the command line to keep
them separate.

VALID MAGIC FLAGS
=================
A magic flag follows the pattern ``//#key=value``. Whitespace around the
equal sign is acceptable.

The known magic flags are::

    ===========  ==============================================================
    Key          Description
    ===========  ==============================================================
    CPPFLAGS     C Pre Processor flags
    CFLAGS       C compiler flags
    CXXFLAGS     C++ flags (do not confuse these with the C PreProcessor flags)
    INCLUDE      Specify include paths without "-I".
                 Adds the path to CPPFLAGS, CFLAGS and CXXFLAGS.
    LDFLAGS      Linker flags
    LINKFLAGS    Linker flags (deprecated, use LDFLAGS)
    SOURCE       Inject an extra source file into the list of files to be built.
                 This is most commonly used in cross platform work.
    PKG-CONFIG   Extract the cflags and libs using pkg-config.
                 Multiple packages on one line (``PKG-CONFIG=a b``)
                 create hard link-order constraints between them.
    PCH          Precompiled header. Specifies a header to precompile into a
                 .gch file. The path is resolved relative to the source file.
    READMACROS   Read macro definitions from specified file before evaluating
                 conditional compilation. Useful for system headers.
    ===========  ==============================================================

**Note:** Magic flags with arbitrary keys (not listed above) are also accepted
and will be passed through to the output. This will allow for project-specific
extensions in the future.

Multi-Package PKG-CONFIG
------------------------
When a source file depends on multiple packages *and their relative link order
matters*, list them in a single annotation:

.. code-block:: cpp

    //#PKG-CONFIG=libssh2 numa

This tells compiletools that ``libssh2`` must be linked **before** ``numa``.
The ordering is treated as a **hard constraint** — it will never be
silently discarded, and contradictory hard constraints between files
are reported as a cyclic-dependency error.

Compare this with two separate single-package annotations:

.. code-block:: cpp

    //#PKG-CONFIG=libssh2
    //#PKG-CONFIG=numa

Here each package's ``pkg-config --libs`` output is collected independently.
The ordering between ``libssh2`` and ``numa`` flags is a **soft constraint**
— if another file's pkg-config output implies the opposite order (common
with shared transitive dependencies like ``-lssl``/``-lcrypto``), compiletools
cancels both directions and picks a deterministic order instead of raising an
error.

**Rule of thumb:** use a single multi-package annotation when you know the
link order matters; use separate annotations when you just need both
packages and don't care about their relative order.

Library Link-Order Resolution
-----------------------------
When multiple source files contribute ``LDFLAGS`` or ``PKG-CONFIG`` flags,
compiletools merges the ``-l`` flags using a topological sort:

1. Each file's flag list defines pairwise ordering constraints
   (``-la -lb`` means ``a`` before ``b``).
2. Constraints from single-package ``PKG-CONFIG`` or plain ``LDFLAGS`` are
   **soft**.  If two files disagree on the order of the same pair, both
   edges are cancelled — this commonly happens when different packages
   list shared transitive dependencies in different orders.
3. Constraints from multi-package ``PKG-CONFIG`` annotations are **hard**.
   A hard edge always wins over a soft edge for the same pair.
4. If a genuine cycle remains after cancellation (e.g., two multi-package
   annotations asserting opposite orders), compiletools reports a
   ``Cyclic library dependency`` error with the cycle path and the
   contributing source files.

IMPORTANT: Library Linking
==========================
compiletools does **not** automatically detect library requirements from includes.
For example, ``#include <pthread.h>`` does NOT automatically add ``-lpthread``.
All library linking must be explicitly specified using either:

* ``//#LDFLAGS=-lpthread`` for direct library specification
* ``//#PKG-CONFIG=libname`` for pkg-config managed libraries

Using READMACROS
================
The READMACROS magic flag allows extracting macro definitions from a file
before evaluating conditional compilation. This is useful when magic flags
depend on macros defined in system headers that aren't in the include path.

.. code-block:: cpp

    #include <fake_system_include/system/version.h>
    //#READMACROS=fake_system_include/system/version.h

    #if SYSTEM_VERSION_MAJOR >= 2
    //#CPPFLAGS=-DSYSTEM_ENABLE_V2
    #else
    //#CPPFLAGS=-DUSE_LEGACY_API
    #endif

The file path is resolved relative to the source file containing the READMACROS
flag, or as an absolute path if specified.

Using PCH (Precompiled Headers)
===============================
The PCH magic flag marks a header for precompilation. The build system
compiles the header into a ``.gch`` file and ensures it is built before any
source that references it.

.. code-block:: cpp

    //#PCH=stdafx.h
    #include "stdafx.h"

    int main() { return 0; }

The header path is resolved relative to the source file containing the
annotation, matching SOURCE semantics. Absolute paths are also accepted.

EXAMPLES
========

* ct-magicflags main.cpp
* ct-magicflags --variant=release main.cpp

SEE ALSO
========
``compiletools`` (1), ``ct-cake`` (1)
