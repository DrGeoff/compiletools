================
ct-list-variants
================

------------------------------------------------------------
List available build variants
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-16
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 10.0.1
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-list-variants [--configname] [--repoonly] [--shorten] [--style STYLE]

DESCRIPTION
===========

A variant is a configuration file that specifies various configurable settings
like the compiler and compiler flags. Common variants are "debug" and "release".
Other ct-* applications use a --variant=<debug/release/clang.debug/etc>
option to specify the parameters to be used for the build.  ct-list-variants
is the tool you use to discover what variants are available on your system.

A variant is a composition of *axis* conf files — one per orthogonal
concern (toolchain ``gcc``/``clang``, optimization ``debug``/``release``,
instrumentation ``asan``/``ubsan``/``coverage``/``lto``/…). ``--variant=gcc,debug,asan``
(or ``gcc.debug.asan`` or ``gcc debug asan`` — comma, dot, and whitespace
are all equivalent separators) splits into the canonical-ordered token
list ``gcc.debug.asan`` and synthesizes the conf-file list from
``gcc.conf`` + ``debug.conf`` + ``asan.conf`` — no per-combination conf
file required. A literal ``gcc.debug.asan.conf`` anywhere in the hierarchy
acts as a *tune-on-top* override: it layers on the synthesized atoms
unless it declares ``extends = ...`` explicitly to choose its own parents.

Config files for the ct-* applications are programmatically located using
python-appdirs, which on linux is a wrapper around the XDG specification.
The default locations are /etc/xdg/ct/ and $HOME/.config/ct/.
Configuration is implemented using python-configargparse which automatically
handles environment variables, command line arguments, system configs,
and user configs.

If any config value is specified in more than one way then

* command line > environment variables > config file values > defaults

ct-list-variants shows the canonical-order axis declaration, the available
axis conf files in each priority tier, and which directories the resolver
will consult.

OPTIONS
=======
--configname / --no-configname
    Include the ``.conf`` extension in variant names. Default: off.

--repoonly / --no-repoonly
    Restrict results to config files in the local repository only.
    Default: off (show all config directories).

--shorten / --no-shorten
    Shorten full paths to just the variant name. Default: on.

--style STYLE
    Output formatting style. Choices:

    * ``pretty`` - Human-readable format with headers (default)
    * ``flat`` - Space-separated list on one line
    * ``filelist`` - One variant per line, no headers

EXAMPLES
========

List all available variants::

    $ ct-list-variants
    Variants compose via axis conf files (e.g. --variant=gcc,debug,asan).
    Canonical token order (/opt/.../src/compiletools/ct.conf.d/ct.conf):
      blank, gcc, clang, icc, msvc, debug, release, asan, ubsan, tsan, coverage, lto, pgo
    From highest to lowest priority configuration directories, the available axis confs are:
    /home/user/.config/ct
        None found
    /opt/.../src/compiletools/ct.conf.d
        asan
        blank
        clang
        coverage
        debug
        gcc
        lto
        release
        tsan
        ubsan

Get variants as a simple list::

    ct-list-variants --style=filelist

Show only repository-local variants::

    ct-list-variants --repoonly

SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1)
