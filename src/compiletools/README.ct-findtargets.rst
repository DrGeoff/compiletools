================
ct-findtargets
================

------------------------------------------------------------
Find executable and test targets in a C/C++ project
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2018-04-17
:Copyright: Copyright (C) 2011-2018 Zomojo Pty Ltd
:Version: 12.1.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-findtargets [-h] [-c CONFIG_FILE] [--variant VARIANT] [-v] [-q]
                    [--version] [-?] [--ID ID]
                    [--CPP CPP] [--CC CC] [--CXX CXX] [--CPPFLAGS CPPFLAGS]
                    [--CXXFLAGS CXXFLAGS] [--CFLAGS CFLAGS]
                    [--git-root | --no-git-root]
                    [--include [INCLUDE [INCLUDE ...]]]
                    [--shorten | --no-shorten] [--bindir BINDIR]
                    [--cas-objdir OBJDIR] [--exemarkers EXEMARKERS]
                    [--testmarkers TESTMARKERS] [--auto | --no-auto]
                    [--auto-exclude AUTO_EXCLUDE]
                    [--append-AUTO-EXCLUDE AUTO_EXCLUDE]
                    [--prepend-AUTO-EXCLUDE AUTO_EXCLUDE]
                    [--style {indent,null,args,flat}]
                    [--filenametestmatch | --no-filenametestmatch]


DESCRIPTION
===========
ct-findtargets uses the variables exemarkers and testmarkers (usually
defined in ct.conf) to find the source files that will
compile to either an executable or a unit test.  The default settings are

* exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
* testmarkers = unit_test.hpp

A filename that starts with "test" and also satisfies the exemarkers will
be reported as a test, unless --no-filenametestmatch is set.

--auto-exclude drops files from the search. Give it multiple times to build
up a list, or set it in a ct.conf. A pattern containing a path separator
matches the gitroot-relative path (a leading ``/`` anchors there, as in
gitignore) and the absolute path; a pattern without one matches any single
component of the gitroot-relative path, whole. So ``vendor`` excludes every
file under any ``vendor`` directory but never ``vendorlib``, ``test_*.cpp``
excludes by basename, and ``src/legacy``, ``/src/legacy`` and
``src/legacy/*`` all exclude that subtree. ``*`` spans separators, so
``*/legacy`` excludes a ``legacy`` subtree at any depth, while
``src/legacy`` never reaches ``src/legacyish``. Directories above the
gitroot are never scanned by a separator-free pattern.
ct-cake and ct-filelist share this search, so an exclusion set
in a ct.conf applies to all three; targets those tools are told to build by
name are never filtered.

Conf files have two spellings. The bare ``auto-exclude`` key is
last-writer-wins across the conf hierarchy, and a command-line
``--auto-exclude`` suppresses it. ``append-AUTO-EXCLUDE`` (uppercase)
accumulates instead, which is what a subproject conf should use to ADD an
exclusion; ``prepend-AUTO-EXCLUDE`` is a synonym, since order carries no
meaning for an exclusion set.

EXAMPLES
========

ct-findtargets

ct-findtargets --variant=release

ct-findtargets --auto-exclude=vendor --auto-exclude=test_*.cpp


SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1)
