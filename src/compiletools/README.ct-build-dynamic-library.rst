========================
ct-build-dynamic-library
========================

----------------------------------------------------------------------
Produce a shared object from a root source file with zero boilerplate
----------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-build-dynamic-library SOURCE [CT-CREATE-MAKEFILE OPTIONS]

DESCRIPTION
===========
``ct-build-dynamic-library`` mirrors ``ct-build-static-library`` but targets a
shared object. The wrapper script accepts a root translation unit, walks the
dependency graph to determine the list of files that must be compiled, and then
generates + executes a Makefile configured for ``--dynamic`` library emission.

Workflow:

1. The first argument (``SOURCE``) is treated as the umbrella implementation.
2. ``ct-filelist --filter=source --style flat`` discovers every dependent source
   reachable from ``SOURCE``.
3. ``ct-create-makefile --dynamic`` receives the root, the discovered list, and
   any remaining user arguments.
4. ``make -j$(ct-jobs)`` builds the shared object with optimal concurrency.

All flags after ``SOURCE`` are passed through to ``ct-create-makefile`` so the
same variant, compiler flag, and output directory options you already depend on
continue to work unchanged.

OPTIONS
=======
``SOURCE``
    Required entry translation unit. Determines both the exported symbols and
    the include graph used to discover supporting files.

Remaining options
    Forwarded untouched to ``ct-create-makefile``. Typical examples are
    ``--variant``, ``--objdir``, ``--bindir``, ``--magic``, and
    ``--append-LDFLAGS``. Refer to ``ct-create-makefile`` (1) for exhaustive
    documentation.

ENVIRONMENT
===========
``CT_JOBS``
    Overrides the automatically computed job count for the final ``make`` run.

``ct.conf``
    Supplies defaults for variants, compiler toolchains, include paths, cache
    options, and shared-object specific toggles.

EXAMPLES
========

Build ``libcore.so`` with the release variant::

    ct-build-dynamic-library src/core.cpp --variant=release

Emit the library into ``build/lib`` while keeping objects in ``build/obj``::

    ct-build-dynamic-library src/api.cpp \
        --objdir=build/obj --bindir=build/lib --append-LDFLAGS="-Wl,-rpath,$ORIGIN"

SEE ALSO
========
``ct-build-static-library`` (1), ``ct-create-makefile`` (1), ``ct-filelist`` (1), ``ct-jobs`` (1)
