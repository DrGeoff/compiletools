================
ct-cppdeps
================

------------------------------------------------------------
C/C++ dependency analysis using the preprocessor
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2022-06-05
:Copyright: Copyright (C) Geoffery Ericksson
:Version: 7.1.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-cppdeps [-h] [-c CONFIG_FILE] [--variant VARIANT] [-v] [-q]
[--version] [-?] [--man]
[--ID ID]
[--CPP CPP] [--CC CC] [--CXX CXX]
[--CPPFLAGS CPPFLAGS] [--CXXFLAGS CXXFLAGS]
[--CFLAGS CFLAGS]
[--git-root | --no-git-root]
[--include INCLUDE] [--pkg-config PKG_CONFIG]
[--shorten | --no-shorten]
[--headerdeps {direct,cpp}]
filename [filename ...]

DESCRIPTION
===========
ct-cppdeps generates the header dependencies for the file you specify at the
command line.  There are two possible methodologies:

* ``--headerdeps=direct`` (default) uses a built-in preprocessor to find
  ``#include`` directives. It handles conditional compilation (``#if``,
  ``#ifdef``, ``#ifndef``, ``#undef``) and computed includes where the path
  is specified via a macro, e.g. ``#include PLATFORM_HEADER``. This is fast
  and correct for the vast majority of real-world code.

* ``--headerdeps=cpp`` executes ``$CPP -MM -MF``, which is slower but
  delegates entirely to the system compiler's preprocessor.

The tool recursively follows #include directives to discover all header files
that the specified source file depends upon, either directly or transitively.
The output is a list of header file paths, one per line.

Note: ct-cppdeps only reports header dependencies. For build automation that
also discovers implementation files and processes magic flag comments, see
ct-cake or ct-build.

EXAMPLES
========

ct-cppdeps somefile1.cpp somefile2.cpp

ct-cppdeps --variant=release somefile.cpp


SEE ALSO
========
``compiletools`` (1), ``ct-findtargets`` (1), ``ct-headertree`` (1), ``ct-config`` (1), ``ct-cake`` (1)
