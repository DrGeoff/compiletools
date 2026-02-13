================
ct-magicflags
================

------------------------------------------------------------------------
Show the magic flags / magic comments that a file exports
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2018-02-23
:Copyright: Copyright (C) 2011-2018 Zomojo Pty Ltd
:Version: 7.0.2
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
    PKG-CONFIG   Extract the cflags and libs using pkg-config
    READMACROS   Read macro definitions from specified file before evaluating
                 conditional compilation. Useful for system headers.
    ===========  ==============================================================

**Note:** Magic flags with arbitrary keys (not listed above) are also accepted
and will be passed through to the output. This will allow for project-specific 
extensions in the future.

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

EXAMPLES
========

* ct-magicflags main.cpp 
* ct-magicflags --variant=release main.cpp 

SEE ALSO
========
``compiletools`` (1), ``ct-cake`` (1)
