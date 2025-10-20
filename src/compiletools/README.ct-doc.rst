.. image:: https://github.com/DrGeoff/compiletools/actions/workflows/main.yml/badge.svg
    :target: https://github.com/DrGeoff/compiletools/actions

============
compiletools
============

--------------------------------------------------------
C/C++ build tools that requires almost no configuration.
--------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-09
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 6.1.3
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
    ct-* [compilation args] [filename.cpp] [--variant=<VARIANT>]

DESCRIPTION
===========
The various ct-* tools exist to build C/C++ executables with almost no
configuration. For example, to build a C or C++ program, type

.. code-block:: bash

    ct-cake

which will automatically determine the correct source files to generate executables
from and also determine the tests to build and run. (The ``--auto`` flag is the
default; use ``--no-auto`` to disable automatic target detection.)

A variant is a configuration file that specifies various configurable settings
like the compiler and compiler flags. Common variants are "debug" and "release".

CONFIGURATION
=============
Options are parsed using the python package ConfigArgParse.  This means they can be passed
in on the command line, as environment variables or in config files.
Command-line values override environment variables which override config file 
values which override defaults. Note that the environment variables are 
captilized. That is, a command line option of --magic=cpp is the equivalent of 
an environment variable MAGIC=cpp.

If the option itself starts with a hypen then configargparse can fail to parse 
it as you intended. For example, on many platforms,

.. code-block:: bash

    --append-CXXFLAGS=-march=skylake

will fail. To work around this, compiletools postprocesses the options to 
understand quotes. For example,

.. code-block:: bash

    --append-CXXFLAGS="-march=skylake" 

will work on all platforms.  Note however that many shells (e.g., bash) will strip 
quotes so you need to escape the quotes or single quote stop the shell preprocessing. 
For example,

.. code-block:: bash

    --append-CXXFLAGS=\\"-march=skylake\\"  
    or 
    --append-CXXFLAGS='"-march=skylake"'

SHARED OBJECT CACHE
===================
compiletools supports a shared object file cache for multi-user/multi-host
environments. When enabled via ``shared-objects = true`` in ct.conf 
(or via the command line or environment variable), the Makefile
generation includes proper locking mechanisms to safely share object files across
concurrent builds by multiple users and hosts. 

Setting in ct.conf is the recommended way to enable this feature for teams so that 
all users gain the locking without needing to set their own environment variables. 

This feature can also be used by a single developer on a single machine to compile 
different directories in parallel, sharing the same object file cache for objects 
that are in common.

Key features:

* **Content-addressable storage**: Object files named by source + flags hash
* **Filesystem-aware locking**: Uses flock on local filesystems, atomic mkdir on network filesystems (NFS, GPFS, Lustre)
* **Multi-user safe**: Group-writable cache with proper file permissions
* **Cross-host compatible**: Automatic filesystem detection and appropriate locking strategy
* **Stale lock detection**: Automatic cleanup of locks from crashed builds
* **Minimal configuration**: Just set ``shared-objects = true`` in config

Example setup for shared cache:

.. code-block:: bash

    # In ct.conf or variant config
    shared-objects = true
    objdir = /shared/nfs/build/cache

    # Ensure cache directory is group-writable with SGID
    mkdir -p /shared/nfs/build/cache
    chmod 2775 /shared/nfs/build/cache

Configuration options in ct.conf:

* ``max_file_read_size = 0`` - Bytes to read from files (0 = entire file)
* ``shared-objects = true`` - Enable shared object cache

OTHER TOOLS
===========
Other notable tools are:

* ct-headertree: provides information about structure of the include files
* ct-filelist: provides the list of files needed to be included in a tarball (e.g. for packaging)

SEE ALSO
========
* ct-build
* ct-build-dynamic-library
* ct-build-static-library
* ct-cache
* ct-cache-clean
* ct-cake
* ct-cmakelists
* ct-compilation-database
* ct-config
* ct-cppdeps
* ct-create-cmakelists
* ct-create-makefile
* ct-filelist
* ct-findtargets
* ct-git-sha-report
* ct-gitroot
* ct-headertree
* ct-jobs
* ct-list-variants
* ct-magicflags
