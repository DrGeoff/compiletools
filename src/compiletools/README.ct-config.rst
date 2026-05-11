============
ct-config
============

--------------------------------------------
Helper tool for examining ct-* configuration
--------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-16
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 9.2.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-config [compilation args] [--variant=<VARIANT>] [-w output.conf]

DESCRIPTION
===========
ct-config is a helper tool for examining how config files, command line
arguments and environment variables are combined to make the internal
variables that the ct-* applications use to do their job.

Config files for the ct-* applications are programmatically located using
python-appdirs, which on linux is a wrapper around the XDG specification.
Thus default locations are /etc/xdg/ct/ and $HOME/.config/ct/.
Configuration parsing is done using python-configargparse which automatically
handles environment variables, command line arguments, system configs
and user configs.

Specifically, the config files are searched for in the following
locations (from lowest to highest priority):

1. ct/ct.conf.d subdirectory alongside the ct-* executable
2. System config: /etc/xdg/ct (XDG compliant)
3. Python virtual environment configs: ${python-site-packages}/ct/ct.conf.d
4. Package bundled config: <installed-package>/ct.conf.d
5. User config: ~/.config/ct (XDG compliant)
6. Project-level config: <gitroot>/ct.conf.d (for project-specific settings)
7. Git repository root directory
8. Current working directory
9. Environment variables (override config files)
10. Command line arguments (highest priority, override everything)

The ct-* applications are aware of two levels of configs.
There is a base level ct.conf that contains the basic variables that apply no
matter what variant (i.e, debug/release/etc) is being built. The default
ct.conf defines the following variables:

.. code-block:: ini

    variant = gcc.cxx26.debug
    exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
    testmarkers = unit_test.hpp
    max_file_read_size = 0

The canonical token ordering used during variant canonicalization is the
``_DEFAULT_VARIANT_CANONICAL_ORDER`` tuple in ``compiletools/configutils.py`` —
that constant is the single source of truth. To override project-wide,
set ``variant-canonical-order = <comma-separated tokens>`` in your
project ``ct.conf``.

VARIANT COMPOSITION
===================

A variant is a composition of *axis* conf files — one per orthogonal
concern. The bundled axes (run ``ct-list-variants`` to see them all):

- **Toolchain:** ``gcc``, ``clang``, ``icc``, ``msvc``
- **C standard:** ``c99``, ``c11``, ``c17``, ``c23``
- **C++ standard:** ``cxx11``, ``cxx14``, ``cxx17``, ``cxx20``, ``cxx23``, ``cxx26``
- **Linker:** ``ld`` (default), ``gold``, ``mold``, ``wild``
- **ABI / arch:** ``m32``, ``m64``, ``native``
  (note: ``native`` defeats cross-machine cache reuse)
- **Optimization:** ``debug``, ``release``, ``releasewithdebinfo``
- **Sanitizers (mutually exclusive — pick one):** ``asan``, ``ubsan``,
  ``tsan``, ``msan``
- **Profiling / codegen:** ``coverage``, ``lto``, ``pgo-gen`` / ``pgo-use``
- **Hardening / codegen flags:** ``hardened``, ``pie``, ``static``,
  ``splitdebug``, ``strip``
- **Codegen knobs:** ``noexceptions``, ``nortti``, ``fastmath``,
  ``werror``, ``libcxx``
- **Specialized:** ``cfi`` (clang CFI), ``shadow-call-stack`` (aarch64),
  ``time-trace`` (clang compile profiling)

To build with gcc, mold as the linker, C++20, the release optimization
level, and AddressSanitizer you ask for::

    ct-cake --variant=gcc,mold,cxx20,release,asan   # comma-separated
    ct-cake --variant=gcc.mold.cxx20.release.asan   # dot-separated (equivalent)
    ct-cake --variant=gcc mold cxx20 release asan   # whitespace-separated (also equivalent)

All three forms canonicalize to ``gcc.cxx20.mold.release.asan`` (the
canonical order comes from ``_DEFAULT_VARIANT_CANONICAL_ORDER`` in
``configutils.py``; a project can override it by setting
``variant-canonical-order = ...`` in its ``ct.conf``). The canonical
name appears in ``cas-objdir/<variant>/``,
``cas-pchdir/<variant>/``, ``compile_commands.<variant>.json``, and
``bin/<variant>/``, so two typings of the same set share caches and outputs.

You do *not* need to write a conf file per combination.
``gcc.cxx20.mold.release.asan`` is synthesized at resolution time from
the five axis conf files, not one per (toolchain × std × linker × opt ×
instrument) combination. To *tune* a specific composition, drop a literal
``<canonical_name>.conf`` anywhere in the hierarchy; it layers on top of
the synthesized atoms (the composite is semantically equivalent to a conf
with ``extends = <each canonical token>``, picking up the atoms
automatically). To pick different parents than the canonical tokens — or
to replace the composition entirely with a curated parent set — write
``extends = ...`` explicitly in the composite::

    # myrelease.conf
    extends = gcc, release, lto
    append-CXXFLAGS = -DMY_PRODUCT=1

``extends = ...`` accepts the same comma/whitespace separators as
``--variant``. Diamond inheritance (two parents pulling in the same
grand-parent) is deduplicated on first visit. Cycles raise an error
naming the cycle path.

Axis conf files should use ``append-CFLAGS = ...`` (and ``append-CXXFLAGS``,
``append-LDFLAGS``, …) rather than the plain ``CFLAGS = ...`` form, so
multiple axes contribute their flags additively instead of overwriting
each other. The bundled ``gcc.conf`` and ``asan.conf`` ship this way.

The ``blank.conf`` file is intentionally empty. ``--variant=blank``
inherits all settings from the environment or parent configs, useful for
shell-driven builds that pass ``CC``/``CXX``/``CFLAGS`` directly.

OPINIONATED BUNDLES
===================

The bundled axes can be composed by hand for every build, but most
projects converge on a handful of recurring combinations. ct-* ships six
opinionated bundles (each is a tiny ``.conf`` file using ``extends = ...``)
covering common workflows:

============= ==================================== =========================
Bundle        Composition                          When to use
============= ==================================== =========================
``dev``       gcc, cxx26, debug, asan, ubsan,      Daily development —
              werror                                fast feedback,
                                                    sanitizers catch most
                                                    C++ bugs early.
``ci``        gcc, cxx26, release, hardened,       Continuous integration
              werror                                — production-like
                                                    with strict warnings.
``production`` gcc, cxx26, release, lto, hardened, Shipped binary —
              pie, strip                            optimized, secured,
                                                    minimal.
``safety``    clang, cxx26, debug, asan, ubsan     Intensive sanitizer
                                                    debugging (clang's
                                                    sanitizer libs are
                                                    most comprehensive).
``perf``      gcc, cxx26, release, lto             Performance work.
                                                    Deliberately NOT
                                                    ``native`` — preserves
                                                    cross-host cache
                                                    reuse.
``secure``    clang, cxx26, release, hardened,     Maximum hardening
              pie, cfi                              including Control-Flow
                                                    Integrity. Heavyweight
                                                    (LTO required, ~5-15%
                                                    runtime cost).
============= ==================================== =========================

Invoke a bundle the same way as any other variant::

    ct-cake --variant=dev          # daily iteration
    ct-cake --variant=production   # shipped binary
    ct-cake --variant=safety       # sanitizer-driven debug

To customize a bundle, write a project-level conf that extends it::

    # myproject/ct.conf.d/myproduction.conf
    extends = production
    append-CXXFLAGS = -DMYPROJ_RELEASE_BUILD=1

Then build with ``ct-cake --variant=myproduction``.

UPGRADING FROM VARIANTALIASES
=============================

The ``variantaliases = {...}`` mechanism has been retired and is no longer
read; an old ``ct.conf`` containing it raises a startup error pointing
back here. Replace each alias with one of the patterns below.

**Default variant alias** — old::

    variant = debug
    variantaliases = {'debug':'gcc.debug', 'release':'gcc.release'}

new — just set the default to the composed name::

    variant = gcc.debug

(``--variant=release`` still works because ``release.conf`` exists as an
axis conf and ``gcc.release`` is synthesized on demand.)

**Flat per-combination conf file** — old:

``gcc.debug.conf`` with the full flag list duplicated in ``gcc.release.conf``,
``clang.debug.conf``, ``clang.release.conf``.

new — split into per-axis files using ``append-`` form so they compose::

    # gcc.conf
    ID = GNU
    CC = gcc
    CXX = g++
    LD = g++
    append-CFLAGS   = -fPIC -Wall
    append-CXXFLAGS = -std=c++17 -fPIC -Wall

    # debug.conf
    append-CFLAGS   = -g
    append-CXXFLAGS = -g

    # release.conf
    append-CFLAGS   = -O3 -DNDEBUG -finline-functions -Wno-inline
    append-CXXFLAGS = -O3 -DNDEBUG -finline-functions -Wno-inline

Call as ``--variant=gcc,debug``, ``--variant=gcc,release``, etc. No per-
combination files needed.

**Custom project alias** — old::

    variantaliases = {'rls':'myproject.release', 'dbg':'myproject.debug'}

new — write the project axis once and compose::

    # myproject.conf
    append-CXXFLAGS = -DMY_PRODUCT=1 -I${MYPROJECT_INCLUDE}

    # then use:
    ct-cake --variant=myproject,debug
    ct-cake --variant=myproject,release,asan

(``myproject`` is not in the builtin canonical order; the resolver
appends it to the end of the canonical name, preserving the user-typed
order for tokens it doesn't know about. Add it to
``variant-canonical-order`` in your project's ``ct.conf`` to lock its
position.)

**Mid-project upgrade path** — for projects that want to keep their
existing ``foo.debug.conf`` literal files during a transition, those keep
working: a literal composite conf takes precedence over synthesis. You
can migrate one axis at a time.

PRIORITY HIERARCHY
==================

If any config value is specified in more than one way then the following
hierarchy is used to overwrite the final value

* command line > environment variables > config file values > defaults

Conf files are read in low-to-high priority order:
``ct.conf (bundled→cwd) < axis_1 (bundled→cwd) < axis_2 < ... < composite_override < --config``.
Scalar keys (``CC``, ``CXX``, …) follow last-writer-wins. Append-style
keys (``append-CFLAGS``, ``append-CXXFLAGS``, ``append-LDFLAGS``,
``append-pkg-config-path``, …) accumulate across the stack.

If you need to append values rather than replace values, this can be
done (currently only for environment variables) by specifying
--variable-handling-method append
or equivalently add an environment variable
VARIABLE_HANDLING_METHOD=append. Inside conf files, just use the
``append-CFLAGS = ...`` form directly — there's no global flag needed.

COMPILER VERSION REQUIREMENTS
=============================

The default variant ``gcc.cxx26.debug`` and the opinionated bundles
(``dev``, ``ci``, ``production``, ``perf``) all pin **C++26**, which
requires:

- **gcc >= 14**, or
- **clang >= 18**

Older compilers spell C++26 as ``-std=c++2c``; cxx26.conf uses
``-std=c++26`` for forward compatibility, so an older compiler will
emit ``unrecognized command line option`` and the build will fail.

Two startup-time guards now catch the common misconfigurations before
the compile step fails opaquely (``apptools._check_resolved_compiler_available``
and ``apptools._check_compiler_supports_requested_standard``):

1. **Missing toolchain.** Picking ``--variant=gcc.*`` on a system
   without gcc installed used to fail later with a generic
   ``g++: command not found`` and no pointer at *which* variant
   requested g++. The startup check now raises clearly::

       Resolved CXX='g++' is not on PATH and is not an executable file.
         variant: gcc.cxx26.debug
         This usually means the toolchain axis pinned by your --variant
         isn't installed. Install it, or switch to a different toolchain
         axis (e.g. --variant=clang,...) that resolves to a binary you have.
         Run `ct-config --variant=gcc.cxx26.debug -vv` to see which conf
         file set CXX.

2. **Compiler too old.** Picking ``--variant=...,cxx26`` on gcc 11
   (Ubuntu 22.04 LTS, e.g.) used to fail with an opaque ``unrecognized
   command line option '-std=c++26'``. The startup check now raises
   clearly::

       Resolved CXX='g++' is gcc 11, which does not support -std=c++26
       (requires gcc >= 14).
         variant: gcc.cxx26.debug
         Either upgrade your gcc toolchain, or compose a lower standard
         axis (e.g. --variant=..,cxx20 in place of ..,cxx26).
         Run `ct-config --variant=gcc.cxx26.debug -vv` to see which conf
         file requested -std=c++26.

For older compilers, choose a different language-standard axis from the
catalog (``cxx20`` is widely supported on gcc 10+ / clang 10+, ``cxx17``
on gcc 7+ / clang 5+). To switch the default, set ``variant = gcc.cxx20.debug``
(or similar) in your project's ``ct.conf``::

    # myproject/ct.conf
    variant = gcc.cxx20.debug

The bundled opinionated composites (``dev``, ``ci``, ``production``,
``perf``, ``secure``, ``safety``) also pin ``cxx26`` via their
``extends = ...`` lists; to retarget them to an older standard for your
project, write a project-level alternative with adjusted extends::

    # myproject/ct.conf.d/dev.conf
    extends = gcc, cxx20, debug, asan, ubsan, werror

The project-level ``dev.conf`` takes precedence over the bundled one
under the normal config-priority hierarchy.

PROVENANCE TRACE
================

Every ct-* tool can emit a per-axis breakdown of which conf files
contributed to the resolved variant, including the canonical-order
source, the ``extends`` chain, and any composite override. Pass ``-vv``
(or higher) on any tool to print it. ``ct-config`` auto-bumps verbosity
to ``-vvv`` and so always shows the trace::

    $ ct-cake --variant=gcc,debug,asan -vv
    Variant: 'gcc,debug,asan'  ->  gcc.debug.asan  (canonicalized)
    Canonical order: blank, gcc, clang, ..., asan, ...
      source: /opt/.../src/compiletools/ct.conf.d/ct.conf

    Base ct.conf files (low -> high priority):
      /opt/.../src/compiletools/ct.conf.d/ct.conf
      /home/geoff/.config/ct/ct.conf

    Axes (each axis lists its conf files low -> high priority):
      [gcc]
          /opt/.../src/compiletools/ct.conf.d/gcc.conf
      [debug]
          /opt/.../src/compiletools/ct.conf.d/debug.conf
      [asan]
          /opt/.../src/compiletools/ct.conf.d/asan.conf

Use this to answer "why did I get these flags?" without re-running under
``-vvv`` and grepping. Quiet by default so build-system wrappers around
``ct-cake`` aren't surprised by extra stdout.

ct-config can be used to create a new config and write the config to file
simply by using the ``-w`` flag.

OPTIONS
=======

--verbose, -v  Output verbosity. Add more v's to make it more verbose (default: 0). Note: Use -vvv to see configuration values.
--version      Show program's version number and exit
--help, -h     Show help and exit
--variant VARIANT  Specifies which variant of the config should be used. Use the config name without the .conf (default: blank)
--write-out-config-file OUTPUT_PATH, -w OUTPUT_PATH  takes the current command line args and writes them out to a config file at the given path, then exits (default: None)

``compilation args``
    Any of the standard compilation arguments you want to go into the config.

CONFIGURATION FILE FORMAT
=========================

Configuration files use INI-style syntax parsed by ConfigArgParse. The format
supports the following features:

**Basic Syntax**

.. code-block:: ini

    # Comments start with hash
    key = value
    key=value          # Spaces around = are optional
    key = value with spaces

**Data Types**

* **Strings**: Values are strings by default. No quotes needed unless preserving whitespace.
* **Booleans**: Use ``true``/``false`` (case-insensitive)
* **Numbers**: Integer or floating-point values
* **Python Literals**: Dicts and lists use Python syntax and are evaluated with ``ast.literal_eval()``

.. code-block:: ini

    # String
    CC = gcc

    # Boolean
    file-locking = true

    # Number
    max_file_read_size = 0

    # Python list (for markers)
    exemarkers = [main(,main (,wxIMPLEMENT_APP]

**Environment Variable Mapping**

Command-line options automatically map to environment variables by:

1. Removing leading dashes
2. Converting to uppercase
3. Replacing dashes with underscores

.. code-block:: bash

    --variant=release    -> VARIANT=release
    --file-locking       -> FILE_LOCKING=true
    --append-CXXFLAGS    -> APPEND_CXXFLAGS="-O2"

**Common Configuration Options**

Base configuration (ct.conf):

.. code-block:: ini

    variant = gcc.debug                                # Default variant (canonical composed form)
    variant-canonical-order = blank, gcc, clang, icc, msvc, debug, release, asan, ubsan, tsan, coverage, lto
    exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
    testmarkers = unit_test.hpp
    max_file_read_size = 0                            # 0 = read entire file
    # file-locking = true                              # Enable file locking
    # cas-objdir = /path/to/cache                      # Object file cache location

Axis configuration (e.g., gcc.conf — toolchain axis):

.. code-block:: ini

    ID = GNU                                          # Compiler identifier
    CC = gcc                                          # C compiler
    CXX = g++                                         # C++ compiler
    LD = g++                                          # Linker
    append-CFLAGS   = -fPIC -Wall                     # ADDITIVE — composes with other axes
    append-CXXFLAGS = -std=c++17 -fPIC -Wall
    append-LDFLAGS  = -fPIC -Wall -Werror

Note ``append-CFLAGS = ...`` not ``CFLAGS = ...``: the append- form
accumulates across axes (debug.conf, asan.conf, …), so a build with
``--variant=gcc,debug,asan`` ends up with all three contributions on
the command line. The override form (``CFLAGS = ...``) would clobber
any earlier-applied axis's flags.

EXAMPLE
=======

Say that you are cross compiling to a beaglebone. First off you might discover that the following line worked but was rather tedious to type

* ct-cake main.cpp --CXX=arm-linux-gnueabihf-g++ --CPP=arm-linux-gnueabihf-g++  --CC=arm-linux-gnueabihf-g++ --LD=arm-linux-gnueabihf-g++

What you would really prefer to type is

* ct-cake main.cpp --variant=bb,debug
* ct-cake main.cpp --variant=bb,release,asan

The trick: write a single ``bb.conf`` describing the cross-toolchain
axis once, and let composition with the existing ``debug.conf`` /
``release.conf`` / instrumentation axes give you every combination for
free.

* ct-config --CXX=arm-linux-gnueabihf-g++ --CPP=arm-linux-gnueabihf-g++  --CC=arm-linux-gnueabihf-g++ --LD=arm-linux-gnueabihf-g++ -w ~/.config/ct/bb.conf

Then edit ``~/.config/ct/bb.conf`` and turn any ``CFLAGS = ...`` /
``CXXFLAGS = ...`` / ``LDFLAGS = ...`` lines into their ``append-...`` forms,
so they compose with the optimization and instrumentation axes instead
of overriding them.

To make ``--variant=release`` (with no toolchain) mean "bb plus release"
by default, set ``variant = bb.release`` in your project's ``ct.conf``.
No alias dict needed — the default variant string IS the composition.

* ct-cake main.cpp                       # uses the configured default
* ct-cake main.cpp --variant=bb,debug    # bb + debug
* ct-cake main.cpp --variant=bb,release  # bb + release
* ct-cake main.cpp --variant=bb,release,asan  # bb + release + AddressSanitizer

SEE ALSO
========
``compiletools`` (1), ``ct-list-variants`` (1)
