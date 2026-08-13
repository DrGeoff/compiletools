================
ct-findtargets
================

------------------------------------------------------------
Find executable and test targets in a C/C++ project
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2018-04-17
:Copyright: Copyright (C) 2011-2018 Zomojo Pty Ltd
:Version: 13.1.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-findtargets [-h] [-c CONFIG_FILE] [--variant VARIANT] [-v] [-q]
                    [--version] [-?] [--ID ID]
                    [--CPP CPP] [--CC CC] [--CXX CXX]
                    [--CPPFLAGS CPPFLAGS [CPPFLAGS ...]]
                    [--CXXFLAGS CXXFLAGS [CXXFLAGS ...]]
                    [--CFLAGS CFLAGS [CFLAGS ...]]
                    [--git-root | --no-git-root]
                    [--include INCLUDE [INCLUDE ...]]
                    [--shorten | --no-shorten] [--bindir BINDIR]
                    [--cas-objdir CAS_OBJDIR] [--exemarkers EXEMARKERS]
                    [--testmarkers TESTMARKERS] [--auto | --no-auto]
                    [--disable-tests | --no-disable-tests]
                    [--disable-exes | --no-disable-exes]
                    [--auto-exclude AUTO_EXCLUDE]
                    [--prepend-AUTO-EXCLUDE AUTO_EXCLUDE]
                    [--append-AUTO-EXCLUDE AUTO_EXCLUDE]
                    [--filenametestmatch | --no-filenametestmatch]
                    [--style {null,flat,indent,args}]
                    [--dynamic [DYNAMIC ...]] [--static [STATIC ...]]
                    [--tests [TESTS ...]]
                    [filename ...]

The synopsis is abridged to the options this page documents;
``ct-findtargets --help`` shows the full inherited build-system surface
(``--variant-canonical-order``, the ``--cas-*dir`` family, pkg-config
and locking options, and the ``--prepend-*``/``--append-*`` flag
variants). See ``ct-config`` (1) and ``ct-commandline`` (1).


DESCRIPTION
===========
ct-findtargets uses the variables exemarkers and testmarkers (usually
defined in ct.conf) to find the source files that will
compile to either an executable or a unit test.  The default settings are

* exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
* testmarkers = unit_test.hpp

A filename that starts with "test" and also satisfies the exemarkers will
be reported as a test, unless --no-filenametestmatch is set.

``--disable-tests`` empties the tests bucket after discovery and
``--disable-exes`` empties the executables bucket — the report's schema
is unchanged (``--style=indent`` still prints the heading, reading
``None found``), only the discovered entries are dropped. These are the
same flags ct-cake uses to build only executables or only tests.

Naming targets explicitly suppresses discovery, the same gate ct-cake
uses: with a positional filename, ``--tests``, or a library slot
(``--static``/``--dynamic``) given, ct-findtargets reports exactly
those targets and never walks the filesystem. With
``--no-auto`` and nothing named it reports nothing — ``--no-auto``
means "do not walk", and the correct output for "do not walk, nothing
named" is empty. Discovered targets re-anchor configuration the same
way ``ct-cake --auto`` does, so a subproject conf reached only through
a discovered target still shapes the final target set (including its
``append-AUTO-EXCLUDE`` additions).

Output path shape differs between the two modes: discovery reports
absolute realpaths (as returned by the tracked-files walk), while a
named target — a positional filename, a ``--tests``/``--static``/
``--dynamic`` value, or a conf-set slot — is echoed back in its
caller's own spelling, unresolved. Harmless for ``ct-build``, which
feeds the printed string
straight to ``ct-create-makefile``, but a caller comparing or
deduplicating paths across the two modes must resolve them itself.

``--static`` and ``--dynamic`` are reported, in their own buckets,
however the value arrives -- on the command line, from any standard
ct.conf tier, from a conf tier anchored on an explicit target, or from a
subproject conf that only ``--auto`` discovery reaches. That is a parity
requirement, not a convenience: ct-cake builds the named library as a
first-class target (it appears on the generated makefile's ``build:``
line, and a discovered executable links against it), and ct-filelist and
ct-compilation-database both act on it too, so a reporter that dropped
it would answer a different question than the tools it exists to
describe.

A library slot counts as a named target for the discovery gate, exactly
as in ct-cake: a slot visible before discovery starts suppresses the
walk, so ``--auto`` with a gitroot ``ct.conf`` setting ``static``
reports that library and nothing else. A slot that arrives later,
through a subproject conf reached by discovery, is added to the set
discovery already found.

All four buckets reach every ``--style``. ``indent`` prints the four
headings unconditionally, empty ones reading ``None found``, so the
output schema does not depend on the tree. ``args`` emits the
executables as positionals first and then ``--tests``, ``--static`` and
``--dynamic``, each only when non-empty -- every one of those slots
takes ``nargs="*"`` and a greedy slot ahead of the positional would
swallow it. That is the form ``ct-build`` feeds to ct-create-makefile.
``flat`` joins all four unlabelled and ``null`` prints four lists.

Where a subproject conf CONTRADICTS the configuration already in force,
ct-findtargets writes the conflict and a "may be incomplete" note to
stderr, reports the targets its first discovery pass found, and exits 0,
while ct-cake fails. The asymmetry is deliberate: a reporter may degrade
and say so, an actor may not proceed on a set discovery could not
finish.

Write the positionals BEFORE the library flag: ``--static`` and
``--dynamic`` each take a list, so a positional written after one is
absorbed into that list. That accident builds rather than failing -- the
executable's object is archived into the library and no executable is
produced -- so a source in a library slot that carries an exemarker
draws a warning on stderr naming the file, the marker and the ordering
rule. It is a warning, not a refusal: a source carrying ``main`` can
legitimately be part of a library. ``-q`` silences it. The warning
reaches the ct-findtargets route only; ``ct-create-makefile`` does not
read exemarkers, so the same mistake made directly against it is
unwarned.

Discovery never INVENTS a library target. Whether a source belongs in a
static library, a shared library or an executable is a partition its
author chooses -- nothing in the source distinguishes the three -- so
the library buckets hold exactly what the command line named plus what
the conf tiers supply.

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
mean; a leading ``/`` survives, so ``/vendor/`` still anchors, and extra
leading slashes read as that same anchored form. A ``..`` inside a pattern
resolves to the path the pattern names, which differs from gitignore: ``a/../vendor``
means ``vendor`` and so excludes a ``vendor`` directory at any depth. A
LEADING ``..`` names a path above the gitroot, where ``--auto`` never
looks, so ``../vendor`` excludes nothing.
ct-cake, ct-filelist and ct-compilation-database share this search, so
an exclusion set in a ct.conf applies to all four tools; targets those
tools are told to build by name are never filtered. ct-filelist, ct-cake and ct-compilation-database
all register ct-findtargets' discovery options without its ``--style``.
ct-cake keeps ct-filelist's own flat/indent ``--style`` for its
``--filelist`` output; ct-compilation-database has no ``--style`` at all
(nor does ct-create-makefile, which registers neither tool's options).

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
