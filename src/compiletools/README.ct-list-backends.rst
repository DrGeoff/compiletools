=================
ct-list-backends
=================

------------------------------------------------------------
List available build backends
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-03-31
:Version: 8.2.3
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-list-backends [--style STYLE] [--all]

DESCRIPTION
===========

ct-list-backends lists the build system backends that compiletools supports.
Each backend generates native build files for a different build system
(Make, Ninja, CMake, Bazel, Tup, or the builtin Shake backend).

Other ct-* applications use ``--backend=<name>`` to select which build
system to use.  ct-list-backends helps you discover which backends are
available and whether their required external tools are installed.

OPTIONS
=======
--style STYLE
    Output formatting style. Choices:

    * ``pretty`` - Human-readable table with build file names and tool
      availability (default)
    * ``flat`` - Space-separated list on one line
    * ``filelist`` - One backend per line, no headers

--all / --no-all
    List all backends, including those whose build tool is not installed.
    Default: off (show only available backends).

EXAMPLES
========

List available backends (default)::

    $ ct-list-backends
    Backend    Build file         Tool               Available
    -----------------------------------------------------------
    cmake      CMakeLists.txt     cmake              yes
    make       Makefile           make               yes
    ninja      build.ninja        ninja              yes
    shake      .ct-traces.json    (builtin)          yes

List all backends, including unavailable ones::

    ct-list-backends --all

Get backends as a simple list::

    ct-list-backends --style=filelist

SEE ALSO
========
``compiletools`` (1), ``ct-list-variants`` (1)
