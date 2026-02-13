================
ct-jobs
================

------------------------------------------------------------
Determine optimal parallel job count for builds
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2017-04-28
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 7.0.2
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-jobs [-j NUM] [--jobs NUM] [--parallel NUM] [--variant VARIANT]

DESCRIPTION
===========
ct-jobs determines how many jobs should run concurrently during builds.
It outputs a single number suitable for use with ``make -j``.

The default is to use the number of CPU cores available to the process.
On Linux, this respects CPU affinity set via ``taskset``. On macOS, it
uses ``sysctl hw.ncpu``. On Termux, it uses ``nproc``.

OPTIONS
=======
-j NUM, --jobs NUM, --parallel NUM, --CAKE_PARALLEL NUM
    Explicitly set the number of parallel jobs. If not specified,
    defaults to the number of available CPU cores.

EXAMPLES
========

Display default job count::

    $ ct-jobs
    8

Use in a build command::

    make -j$(ct-jobs)

Override with explicit value::

    ct-jobs --jobs 4

Respect CPU affinity on Linux::

    taskset -c 0-3 ct-jobs  # Returns 4

SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1)
