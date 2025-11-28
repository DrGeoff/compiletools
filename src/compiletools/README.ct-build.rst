============
ct-build
============

-----------------------------------------------------
One-command wrapper around ct-findtargets + make -jN
-----------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-build [CT-FINDTARGETS OPTIONS] [CT-CREATE-MAKEFILE OPTIONS]

DESCRIPTION
===========
``ct-build`` is a convenience front-end that chains the usual steps needed to
compile a project with compiletools:

1. ``ct-findtargets --style=args`` is run with all user-supplied arguments to
   discover buildable executables and tests.
2. ``ct-create-makefile`` is invoked with the discovered targets plus the same
   argument list to produce an up-to-date Makefile.
3. ``make -j$(ct-jobs)`` executes the generated Makefile using the job count
   calculated by ``ct-jobs`` (which honors ``ct.conf`` and ``CT_JOBS``).

Because every flag is passed through untouched, you can use the exact same
options you would normally hand to ``ct-findtargets`` or ``ct-create-makefile``
(e.g., ``--variant``, ``--magic``, ``--append-CXXFLAGS``, ``--objdir``,
``--bindir``). The wrapper simply glues those tools together and ensures the
parallel make step uses the recommended level of concurrency.

OPTIONS
=======
``ct-build`` does not define its own bespoke flags; it accepts the combined
option set of ``ct-findtargets`` and ``ct-create-makefile``. Frequently used
examples include:

``--variant VARIANT``
    Choose the build variant (debug, release, etc.). Defaults to ``blank``.

``--auto`` / ``--no-auto``
    Enable or disable automatic target discovery. Auto mode is on by default.

``--magic {direct,cpp}``
    Select magic include processing strategy passed through to ct-cake logic.

``--makefilename PATH``
    Override the location of the intermediate Makefile.

``--objdir PATH`` / ``--bindir PATH``
    Override where objects and binaries are written.

See the manuals for ``ct-findtargets`` and ``ct-create-makefile`` for the
complete option reference.

ENVIRONMENT
===========
``CT_JOBS``
    If set, overrides the job count returned by ``ct-jobs`` for the final
    ``make`` invocation. Otherwise ``ct-jobs`` uses CPU count and config data.

``ct.conf``
    Settings such as include paths, shared object cache directives, and variant
    defaults influence every stage that ct-build orchestrates.

EXAMPLES
========

Build everything in the current tree using the release variant::

    ct-build --variant=release

Compile with automatic magic flag detection disabled and custom output dirs::

    ct-build --no-auto --objdir=out/obj --bindir=out/bin

SEE ALSO
========
``ct-findtargets`` (1), ``ct-create-makefile`` (1), ``ct-cake`` (1), ``ct-jobs`` (1)

