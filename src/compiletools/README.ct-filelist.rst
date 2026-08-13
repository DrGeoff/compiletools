================
ct-filelist
================

-------------------------------------------------------------------------------------------------------
Determine header and source dependencies of a C/C++ file by following headers and implied source files.
-------------------------------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2017-07-06
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 13.1.0
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
                    Output formatting style. ``flat`` outputs one file per
                    line. ``indent`` prefixes every dependency line with a
                    tab -- a single fixed level, not a nested tree; under
                    the default ``--merge`` the whole listing is one
                    uniformly indented block, and only ``--no-merge`` adds
                    a second level (unindented per-target headings above
                    their indented dependencies).
                    (default: flat)

--filter {header,source,all}
                    Filter output to show only headers, only source files,
                    or all files. Useful for packaging when you need to
                    separate headers from implementation files. Applies to
                    the whole listing in both merge modes: extras obey it,
                    and a ``--no-merge`` heading whose target the filter
                    drops is omitted while the group's surviving
                    dependencies still print.
                    (default: all)

--shorten
                    Strip the git root from the filenames.
                    Use ``--no-shorten`` to show full paths.
                    (default: False)

--merge
                    Merge all outputs into a single sorted list.
                    Use ``--no-merge`` to keep separate lists per input
                    file: any extras print first as their own sorted,
                    unheaded block (they belong to no input file), then
                    each target prints as a heading line followed by its
                    sorted dependencies.
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
                    separator matches the gitroot-relative path (a leading
                    ``/`` anchors there, as in gitignore); a pattern without
                    one matches any single component of the gitroot-relative
                    path, whole. So ``vendor`` excludes every file under any
                    ``vendor`` directory but never ``vendorlib``,
                    ``test_*.cpp`` excludes by basename, and ``src/legacy``,
                    ``/src/legacy`` and ``src/legacy/*`` all exclude that
                    subtree. ``*`` spans separators, so ``*/legacy`` matches
                    at any depth -- but only within the gitroot: directories
                    above it are out of reach of a relative pattern, so
                    ``*/tmp/*`` cannot exclude a whole checkout that sits
                    under ``/tmp``. An absolute pattern matches the absolute
                    path only when it reaches INTO the tree -- everything
                    before its first ``*``, ``?`` or ``[`` starts at the
                    gitroot -- which is how ``${CONF_DIR}/legacy`` works.
                    One naming an ANCESTOR is read as the gitroot-anchored
                    form it looks like, so ``/tmp`` excludes the project's
                    own ``tmp`` directory, never the whole checkout.
                    Redundant path syntax is normalised away first, so
                    ``vendor/`` is gitignore's spelling of ``vendor`` and
                    ``./vendor`` and ``src//vendor`` mean what their plain
                    forms mean; a leading ``/`` survives normalisation, so
                    ``/vendor/`` still anchors at the gitroot, as does any
                    number of leading slashes.
                    In ct-filelist the patterns filter the OUTPUT LISTING as
                    well as discovery: a file reached only through the
                    dependency walk of a non-excluded target -- a vendored
                    header, say -- is dropped from the list even though
                    ``ct-cake`` compiles it. Packaging is what the list is
                    for. Explicitly named files (a filename on the command
                    line, ``--extrafile``, ``--extrafilelist``) are never
                    filtered; files a DIRECTORY sweep turns up are --
                    ``--extradir`` listings and the sweep of every file
                    beside a ``--tests`` file name the directory, not the
                    files, so the patterns govern what the sweep keeps.
                    In a ct.conf
                    the bare ``auto-exclude`` key is last-writer-wins between
                    conf files; use ``append-AUTO-EXCLUDE`` (uppercase) to
                    accumulate across the conf hierarchy instead. A
                    command-line ``--auto-exclude`` appends to the conf
                    values rather than replacing them. See ``ct-cake`` (1).

--dynamic LIB.cpp
                    Include files needed for building a dynamic/shared library.

--static LIB.cpp
                    Include files needed for building a static library.

--tests TEST.cpp
                    Include files needed for building test executables.
                    Also sweeps in every file in the test file's own
                    directory (auxiliary data the test reads at runtime);
                    ``--auto-exclude`` filters that sweep.

**Extra Files**

--extrafile FILE
                    Extra files to directly add to the filelist.
                    Can be specified multiple times.

--extradir DIR
                    Extra directories to add all files from to the filelist.
                    Can be specified multiple times. ``--auto-exclude``
                    filters what the sweep turns up (the directory was
                    named, the files were not).

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
                    configuration is active. (default: whatever the active
                    ct.conf hierarchy sets; the bundled ct.conf ships
                    ``variant = gcc.cxx26.debug``)

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

--include PATH [PATH ...]
                    Add additional include paths for header dependency
                    resolution. One occurrence takes several paths; a
                    repeated occurrence REPLACES the earlier one. Use
                    ``--append-INCLUDE`` to accumulate instead.

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

Group dependencies under their target (heading + tab-indented deps):

.. code-block:: bash

    ct-filelist --no-merge --style=indent myfile.cpp

Include extra files for packaging (the positional target comes first --
the ``--extrafile`` / ``--extradir`` options greedily take every
following bare word as their own):

.. code-block:: bash

    ct-filelist myfile.cpp --extradir=../icons --extrafile=README.md

List files for a library build:

.. code-block:: bash

    ct-filelist --dynamic mylib.cpp


SEE ALSO
========
``ct-cake`` (1), ``ct-config`` (1), ``ct-commandline`` (1)
