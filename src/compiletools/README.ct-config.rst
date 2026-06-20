============
ct-config
============

--------------------------------------------
Helper tool for examining ct-* configuration
--------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-16
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 10.1.11
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
that constant is the single source of truth. See WHY CANONICAL ORDERING
EXISTS for the motivation and AUTHORING YOUR OWN CANONICAL ORDER for the
recipe to override it project-wide.

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

.. list-table::
   :header-rows: 1
   :widths: 15 40 45

   * - Bundle
     - Composition
     - When to use
   * - ``dev``
     - gcc, cxx26, debug, asan, ubsan, werror
     - Daily development — fast feedback, sanitizers catch most C++ bugs early.
   * - ``ci``
     - gcc, cxx26, release, hardened, werror
     - Continuous integration — production-like with strict warnings.
   * - ``production``
     - gcc, cxx26, release, lto, hardened, pie, strip
     - Shipped binary — optimized, secured, minimal.
   * - ``safety``
     - clang, cxx26, debug, asan, ubsan
     - Intensive sanitizer debugging (clang's sanitizer libs are most comprehensive).
   * - ``perf``
     - gcc, cxx26, release, lto
     - Performance work. Deliberately NOT ``native`` — preserves cross-host cache reuse.
   * - ``secure``
     - clang, cxx26, release, hardened, pie, cfi
     - Maximum hardening including Control-Flow Integrity. Heavyweight (LTO required, ~5-15% runtime cost).

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

WHY CANONICAL ORDERING EXISTS
=============================

Canonicalising every variant string into one well-defined token order
serves three concrete purposes:

1. **Cache-key stability.** The canonical name is the on-disk directory
   that holds build artefacts: ``cas-objdir/<variant>/``,
   ``cas-pchdir/<variant>/``, ``compile_commands.<variant>.json``,
   ``bin/<variant>/``. Without canonicalisation, two developers typing
   the same axes in different orders — ``--variant=gcc,debug`` vs
   ``--variant=debug,gcc`` — would carve two separate cache trees and
   neither would benefit from the other's work. Canonicalisation
   collapses both forms to ``gcc.debug`` so the cache hits.

2. **Flag-layering parity between** ``extends = ...`` **and**
   ``--variant=...``. The resolver walks parents in *declared* order
   and ``configargparse`` is last-writer-wins per scalar key, so
   ``extends = werror, gcc`` produces different flag layering than
   ``extends = gcc, werror`` (in the first form ``gcc.conf``'s
   ``CC=gcc`` overwrites whatever ``werror.conf`` set; in the second
   form it's the other way around). Canonical ordering pins one
   layering as the reference: a composite ``.conf`` written with parents
   in canonical order produces the same flags as the CLI form
   ``--variant=<same tokens>``. Out-of-order ``extends`` triggers a
   runtime warning (``_check_extends_canonical_order``) naming the
   recommended reordering.

3. **Deterministic output paths.** Because the canonical name appears
   in user-facing paths and in ``-vv`` provenance traces, the order
   must be deterministic across machines. Two CI hosts and a
   developer's laptop must all agree on the same string for the same
   set of axes, otherwise build outputs are unfindable and caches
   diverge silently.

Tokens absent from the canonical order trail at the end of the canonical
name in user-typed order, so a project can introduce its own axis (e.g.
``myproject``) without re-declaring the whole order — see the "Custom
project alias" recipe in UPGRADING FROM VARIANTALIASES above.

AUTHORING YOUR OWN CANONICAL ORDER
==================================

Two reasons you might want to specify your own canonical order:

1. **You don't like the builtin order.** Maybe your team puts the
   linker before the toolchain, or sanitizers before optimization,
   and you want the canonical names to reflect that.

2. **You added project-specific axes and want them to land in a
   specific position** — not just trail at the end. For example, an
   ``mlops`` axis that should sort right after the C++ standard so
   ``mlops.debug`` and ``debug.mlops`` both canonicalise to
   ``mlops.debug``, and so ``extends = gcc, cxx26, mlops, debug``
   doesn't trip the out-of-order warning.

The full override hierarchy is documented in CANONICAL-ORDER OVERRIDES
below (CLI > environment > ct.conf > builtin), but the common case is
a project-wide pin in your ``ct.conf``. Copy the commented example
shown in the bundled ``ct.conf`` as a starting point, then edit::

    # In <project-root>/ct.conf or <project-root>/ct.conf.d/ct.conf
    variant-canonical-order = blank, gcc, ccache-gcc, clang, ccache-clang,
        c99, c11, c17, c23, cxx11, cxx14, cxx17, cxx20, cxx23, cxx26,
        mlops,                                     # <-- new project axis
        ld, gold, mold, wild, m32, m64, native,
        debug, release, releasewithdebinfo,
        asan, ubsan, tsan, msan, coverage, lto,
        pgo-gen, pgo-use, hardened, pie, static, splitdebug, strip,
        noexceptions, nortti, fastmath, werror, libcxx,
        cfi, shadow-call-stack, time-trace,
        dev, ci, production, safety, perf, secure

The value replaces the builtin tuple entirely — you must list every
token you care about, including the bundled ones. Tokens you omit
behave the same as user-defined axes: they trail at the end in
user-typed order. (This is also useful for *narrowing* the order: an
embedded project that doesn't use ``cfi`` / ``shadow-call-stack`` /
``time-trace`` can drop them from its order without affecting anything
else.) The drift-guard unit test ``test_bundled_ct_conf_comment_example_matches_builtin``
only checks the bundled ``ct.conf`` — your project override is free to
diverge.

For a one-off experiment without editing any conf file, use the CLI or
environment forms documented in CANONICAL-ORDER OVERRIDES below.

CANONICAL-ORDER OVERRIDES
=========================

The canonical token order has four override layers, highest priority wins:

1. **Command line** — ``--variant-canonical-order=<comma-separated tokens>``
   overrides everything for one invocation. Useful for one-off experiments
   or shell aliases that pin an order without touching project conf::

       ct-cake --variant-canonical-order=blank,gcc,clang,debug,release,asan \
               --variant=asan,gcc,debug a.cpp

2. **Environment** — ``CT_VARIANT_CANONICAL_ORDER=<comma-separated tokens>``
   pins the order for a shell session. Useful in CI to enforce the
   organization-wide order without editing each repo's ct.conf::

       export CT_VARIANT_CANONICAL_ORDER="blank,gcc,clang,icc,msvc,debug,release,asan,ubsan,tsan,coverage,lto"

3. **Conf file** — ``variant-canonical-order = <comma-separated tokens>``
   in any ``ct.conf`` (highest-priority conf wins, per the standard
   priority hierarchy). This is the recommended place for a project-wide
   pin so that all contributors get the same canonicalization.

4. **Builtin** — ``_DEFAULT_VARIANT_CANONICAL_ORDER`` in
   ``compiletools/configutils.py``. Used when none of the above is set.

The order is consulted very early — at parser-construction time, before
configargparse processes ``--variant`` — so the CLI form scans ``argv``
directly. Drift between the builtin tuple and the example shown in the
bundled ``ct.conf`` is guarded by
``test_bundled_ct_conf_comment_example_matches_builtin``.

PRIORITY HIERARCHY
==================

If any config value is specified in more than one way then the following
hierarchy is used to overwrite the final value

* command line > environment variables > config file values > defaults

Conf files are read in low-to-high priority order:
``ct.conf (bundled→cwd) < axis_1 (bundled→cwd) < axis_2 < ... < composite_override < --config``.
Scalar keys (``CC``, ``CXX``, …) follow last-writer-wins. Append-style
keys (``append-CFLAGS``, ``append-CXXFLAGS``, ``append-LDFLAGS``,
``append-INCLUDE``, ``append-pkg-config-path``, …) accumulate across the
entire stack: every contributing conf file's value is preserved, and
matching ``--append-*`` / ``--prepend-*`` CLI flags accumulate on top
of the conf-file values (CLI tokens land after conf tokens in the
joined flag string, so the compiler's "last occurrence wins" rule
keeps CLI override semantics for conflicting flags like ``-O0``/``-O3``).
Lower-priority confs appear earlier in the final flag string than
higher-priority confs, so a higher-priority axis (or composite override)
can supersede a lower one by emitting a conflicting flag later. The
accumulation rule is enforced by ``apptools._ComposingArgumentParser``
+ ``_AccumulatingConfigFileParser``; see
``TestAppendFlagsAccumulateAcrossConfHierarchy`` in ``test_apptools.py``
for the contract.

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

**${CONF_DIR} placeholder**

Inside any conf-file value, ``${CONF_DIR}`` expands at parse time to the
absolute directory of the conf file that value lives in. Use it to anchor
path-bearing directives in an axis conf so they keep working regardless
of the consumer's cwd at compile time:

.. code-block:: ini

    # ct.conf.d/flavor-b.conf
    prepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig-b
    append-INCLUDE          = ${CONF_DIR}/include
    append-CFLAGS           = -I${CONF_DIR}/include
    append-CXXFLAGS         = -I${CONF_DIR}/include
    LD                      = ${CONF_DIR}/wrapper.sh
    cas-objdir              = ${CONF_DIR}/../shared-cas

The placeholder is generic — it works in every key, not just
``*-PATH``. Bare relative paths stay bare and resolve against the
consumer cwd (matching the CLI's
``--prepend-PKG-CONFIG-PATH=relative/path`` behaviour), so
``${CONF_DIR}`` is the explicit way to opt into conf-relative anchoring.
A working fixture lives at
``examples-features/conf_dir_relative_pkgconfig/``.

**Environment variables and ~ in conf values**

Conf-file values also expand ``$VAR``, ``${VAR}``, and ``~`` at parse
time, after the ``${CONF_DIR}`` substitution. The pipeline is:

1. ``${CONF_DIR}`` is substituted first (above).
2. ``$VAR`` and ``${VAR}`` are expanded via ``os.path.expandvars`` —
   unset variables stay literal.
3. ``~`` and ``~user`` are expanded via ``os.path.expanduser``.

This lets a checked-in axis conf express a per-user cache root without
hardcoding one developer's absolute path:

.. code-block:: ini

    # ct.conf.d/shared.conf — shared cache for multi-user dev hosts
    extends = mold
    cas-objdir = $HOME/cache/cas-objs
    cas-pchdir = ~/cache/cas-pch

To keep a literal ``$`` in a value, double it: ``$$``. For example,
``append-CXXFLAGS = -DVERSION=$$BUILD_NUM`` expands to the literal flag
``-DVERSION=$BUILD_NUM`` rather than expanding ``$BUILD_NUM`` as an
environment variable.

**Backward-compat note:** a user with a literal ``$HOME`` or ``~`` in a
conf today now gets it expanded. Those values were broken under the
prior parser (compiletools would have tried to open
``/abs/$HOME/cache/...`` and failed), so the change is a fix rather
than a regression.

For diagnostics, at high verbosity (``-vvvv``) ``ct-config`` (or any
``ct-*`` tool) prints the source ``conf-file:line`` for every
PKG_CONFIG_PATH entry it emits, distinguishing conf-file values from
CLI flags and auto-discovered cwd/gitroot defaults.

**Variant suffix is auto-appended to cas-*dir paths**

Any user-supplied value for ``cas-objdir``, ``cas-pchdir``,
``cas-pcmdir``, or ``cas-exedir`` is normalised to end in
``/<variant>`` so the four CAS layers stay separated per variant. A
user pointing every host at a shared pool only needs to write the bare
root:

.. code-block:: ini

    # ct.conf.d/shared.conf
    cas-objdir = $HOME/cache/cas-objs
    cas-pchdir = ~/cache/cas-pch

Building ``--variant=gcc.release`` resolves these to
``$HOME/cache/cas-objs/gcc.release`` and
``~/cache/cas-pch/gcc.release`` respectively. The append is
idempotent: a path that already ends in ``/<variant>`` is left alone,
so a conf migrated from before this contract needs no edit. Built-in
defaults (``<gitroot>/cas-objdir/<variant>`` and the no-gitroot
``bin/<variant>/obj`` fallback) already incorporate the variant and
are unchanged.

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
