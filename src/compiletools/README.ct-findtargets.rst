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
gitignore); a pattern without one matches any single component of the
gitroot-relative path, whole. So ``vendor`` excludes every file under any
``vendor`` directory but never ``vendorlib``, ``test_*.cpp`` excludes by
basename, and ``src/legacy``, ``/src/legacy`` and ``src/legacy/*`` all
exclude that subtree. ``*`` spans separators, so ``*/legacy`` excludes a
``legacy`` subtree at any depth, while ``src/legacy`` never reaches
``src/legacyish``. Directories above the gitroot are out of reach of a
relative pattern of either kind, so ``*/tmp/*`` cannot exclude an entire
checkout that merely sits under ``/tmp``. An ABSOLUTE pattern is matched
against the absolute path only when it reaches INTO the tree -- when
everything before its first ``*``, ``?`` or ``[`` starts at the gitroot,
which is what makes ``${CONF_DIR}/legacy`` work. An absolute pattern
naming an ANCESTOR is read the same way as a relative one, so ``/tmp``
excludes the project's own ``tmp`` directory rather than the whole
checkout when the checkout happens to live under ``/tmp``. Redundant path
syntax is normalised away first, so ``vendor/`` is gitignore's spelling of
``vendor`` and ``./vendor`` and ``src//vendor`` mean what their plain forms
mean; a leading ``/`` survives, so ``/vendor/`` still anchors.
ct-cake and ct-filelist share this search, so an exclusion set
in a ct.conf applies to all three; targets those tools are told to build by
name are never filtered. ct-filelist registers the discovery options
without ``--style``, keeping its own; ct-cake and ct-compilation-database
register the full set and so still accept ct-findtargets' ``--style``.

Conf files have two spellings. The bare ``auto-exclude`` key is
last-writer-wins across the conf hierarchy. It does not contest with the
command line: a command-line ``--auto-exclude`` APPENDS to whatever the
conf hierarchy resolved to, so there is no way to un-exclude from the
command line. ``append-AUTO-EXCLUDE`` (uppercase) accumulates between conf
files instead of replacing, which is what a subproject conf should use to
ADD an exclusion; ``prepend-AUTO-EXCLUDE`` is a synonym, since order
carries no meaning for an exclusion set. A bare key that a
higher-priority conf overrides is reported at ``-v``.

EXAMPLES
========

ct-findtargets

ct-findtargets --variant=release

ct-findtargets --auto-exclude=vendor --auto-exclude=test_*.cpp


SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1)
