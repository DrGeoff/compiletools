==========================
ct-build-static-library
==========================

--------------------------------------------------------------
Generate a static library from a root source file in one shot
--------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-build-static-library SOURCE [CT-CREATE-MAKEFILE OPTIONS]

DESCRIPTION
===========
``ct-build-static-library`` bundles the discovery + Makefile + build sequence
needed to produce a traditional ``.a`` archive from a designated translation
unit. The script performs the following steps:

1. Treat the first argument as the *root* implementation file (``SOURCE``).
2. Use ``ct-filelist --filter=source --style flat`` to enumerate every source
   file that must be compiled to satisfy that root target.
3. Run ``ct-create-makefile --static`` with the root file, the discovered file
   list, and all remaining user arguments.
4. Invoke ``make -j$(ct-jobs)`` to build the archive using the recommended
   level of parallelism.

All additional positional or option arguments after ``SOURCE`` are passed
unchanged to ``ct-create-makefile`` so the same variant, compiler flag, output
directory, or cache settings you normally rely on continue to work.

OPTIONS
=======
``SOURCE``
    Required first argument. Points to the canonical implementation file whose
    include graph determines which translation units belong in the archive.

Remaining options
    Every subsequent flag is forwarded to ``ct-create-makefile``. Commonly used
    options include ``--variant``, ``--objdir``, ``--bindir``, ``--magic``, and
    the various ``--append-*FLAGS`` switches. See ``ct-create-makefile`` (1) for
    the full option list.

ENVIRONMENT
===========
``CT_JOBS``
    Overrides the job count returned by ``ct-jobs`` for the final ``make`` run.

``ct.conf``
    Provides shared defaults for variants, include paths, caches, and toolchain
    settings used during Makefile generation.

EXAMPLES
========

Build ``libmath.a`` from ``library/math.cpp`` using the release variant::

    ct-build-static-library library/math.cpp --variant=release

Place intermediate files under ``out/`` while appending custom flags::

    ct-build-static-library src/core.cpp --objdir=out/obj --bindir=out/lib \
        --append-CXXFLAGS="-fvisibility=hidden"

SEE ALSO
========
``ct-build-dynamic-library`` (1), ``ct-create-makefile`` (1), ``ct-filelist`` (1), ``ct-jobs`` (1)

