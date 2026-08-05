================
ct-filelist
================

-------------------------------------------------------------------------------------------------------
Determine header and source dependencies of a C/C++ file by following headers and implied source files.
-------------------------------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2017-07-06
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 12.1.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-filelist [OPTION] [filename ...]

DESCRIPTION
===========
ct-filelist uses the given variants/configs, command line arguments,
environment variables, and most importantly one or more filenames to determine
the list of files that are required to build the given filename(s). With no
filenames it discovers them itself (``--auto``, the default), reporting the
files required to build everything ``ct-cake --auto`` would build. For example,
if myfile.cpp includes myfile.hpp and myfile.hpp in turn includes awesome.h

.. code-block:: text

  myfile.cpp
  |_ myfile.hpp
     |_ awesome.h

then "ct-filelist myfile.cpp" will return

.. code-block:: text

  awesome.h
  myfile.cpp
  myfile.hpp

The command line arguments --extrafile, --extradir, --extrafilelist are used
to add extra files to the output.  This can be useful when you are using the
output to build up a set of files to include in a tarball.

OPTIONS
=======

**Output Control**

--style {flat,indent}
                    Output formatting style. ``flat`` outputs one file per line.
                    ``indent`` shows the dependency tree with indentation.
                    (default: flat)

--filter {header,source,all}
                    Filter output to show only headers, only source files,
                    or all files. Useful for packaging when you need to
                    separate headers from implementation files.
                    (default: all)

--shorten
                    Strip the git root from the filenames.
                    Use ``--no-shorten`` to show full paths.
                    (default: False)

--merge
                    Merge all outputs into a single list.
                    Use ``--no-merge`` to keep separate lists per input file.
                    (default: True)

**Build Targets**

--auto
                    With no filenames given, search the filesystem from the
                    current working directory for C/C++ files with main
                    functions and unit tests, and report the files needed to
                    build them. This is the same discovery ``ct-cake --auto``
                    performs, through the same driver, so the two agree on
                    which targets exist. Use ``--no-auto`` to keep a bare
                    ct-filelist silent. Explicitly named filenames (or
                    ``--static`` / ``--dynamic`` / ``--tests``) suppress
                    discovery. (default: True)

--auto-exclude PATTERN
                    Glob excluding files from ``--auto`` discovery. Can be
                    specified multiple times. A pattern containing a path
                    separator matches the gitroot-relative and absolute paths;
                    a pattern without one matches any single path component,
                    whole. So ``vendor`` excludes every file under any
                    ``vendor`` directory but never ``vendorlib``,
                    ``test_*.cpp`` excludes by basename, and both
                    ``src/legacy`` and ``src/legacy/*`` exclude that subtree.
                    Explicitly named targets are never filtered. In a ct.conf
                    the bare ``auto-exclude`` key is last-writer-wins; use
                    ``append-AUTO-EXCLUDE`` (uppercase) to accumulate
                    exclusions across the conf hierarchy. See ``ct-cake`` (1).

--dynamic LIB.cpp
                    Include files needed for building a dynamic/shared library.

--static LIB.cpp
                    Include files needed for building a static library.

--tests TEST.cpp
                    Include files needed for building test executables.

**Extra Files**

--extrafile FILE
                    Extra files to directly add to the filelist.
                    Can be specified multiple times.

--extradir DIR
                    Extra directories to add all files from to the filelist.
                    Can be specified multiple times.

--extrafilelist FILE
                    Read the given file to find a list of extra files to add.
                    Can be specified multiple times.

**Dependency Detection**

--headerdeps {direct,cpp}
                    Method for finding header dependencies. ``direct`` parses
                    include statements directly (faster). ``cpp`` uses the
                    C preprocessor (more accurate with macros).
                    (default: direct)

**Common Options**

--variant VARIANT
                    Build variant to use for dependency resolution
                    (debug, release, etc.). Determines which compiler
                    configuration is active. (default: blank)

-v, --verbose       Increase verbosity. Can be specified multiple times.

-q, --quiet         Decrease verbosity.

--version           Show the program version and exit.

--man, --doc        Display the full manual/documentation.

-h, -?              Show help message and exit.

**Build System Options**

The following options are (and many more) are inherited from the common build
system and control dependency resolution. See ``ct-config`` (1)
and ``ct-commandline`` (1) for the complete reference of compiler and build options.

--git-root / --no-git-root
                    Add git root to include paths for dependency detection.
                    (default: True)

--include PATH      Add additional include paths for header dependency
                    resolution. Can be specified multiple times.

--pkg-config LIBS   Use pkg-config to resolve library dependencies.
                    Can be specified multiple times.


EXAMPLES
========

Basic usage - list all dependencies:

.. code-block:: bash

    ct-filelist myfile.cpp

List everything ``ct-cake --auto`` would build, skipping a vendored subtree:

.. code-block:: bash

    ct-filelist --auto-exclude=vendor

Show only header files (useful for packaging headers separately):

.. code-block:: bash

    ct-filelist --filter=header mylib.cpp

Show only source files:

.. code-block:: bash

    ct-filelist --filter=source mylib.cpp

Show dependency tree with indentation:

.. code-block:: bash

    ct-filelist --style=indent myfile.cpp

Include extra files for packaging:

.. code-block:: bash

    ct-filelist --extradir ../icons --extrafile README.md myfile.cpp

List files for a library build:

.. code-block:: bash

    ct-filelist --dynamic mylib.cpp


SEE ALSO
========
``ct-cake`` (1), ``ct-config`` (1), ``ct-commandline`` (1)
