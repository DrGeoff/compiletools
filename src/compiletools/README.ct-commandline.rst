==============
ct-commandline
==============

---------------------------------------------------------------------------------------
This document describes the command line arguments that are common across ct-* programs
---------------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2017-07-06
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 7.0.2
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
    ct-* [args]

DESCRIPTION
===========
All ct-* tools share a common set of command line arguments for configuration,
verbosity control, and build variant selection. These arguments are processed
uniformly across the toolkit.

OPTIONS
=======
--variant VARIANT
    Specifies which build variant configuration to use. Common variants include
    ``debug``, ``release``, ``clang.debug``, ``clang.release``, ``gcc.debug``,
    and ``gcc.release``. Use ``ct-list-variants`` to discover available variants
    on your system. The variant determines compiler flags, optimization levels,
    and other build parameters.

-c CONFIG_FILE, --config CONFIG_FILE
    Specifies an additional configuration file to load. Configuration values
    are merged with system and user configs according to precedence rules:
    command line > environment variables > config files > defaults.

-v, --verbose
    Increase output verbosity. Can be specified multiple times for more detail
    (e.g., ``-vv`` or ``-v -v``).

-q, --quiet
    Decrease output verbosity. Useful for silencing tools that default to
    verbose output.

--version
    Display the version number and exit.

-?, -h, --help
    Display help message and exit.

--man, --doc
    Display the full documentation/manual page for the tool. Requires the
    ``rich_rst`` Python module to be installed.

CONFIGURATION
=============
Configuration is handled through a hierarchy of sources:

1. **Command line arguments** (highest priority)
2. **Environment variables** (e.g., ``CT_VARIANT``, ``CT_VERBOSE``)
3. **User config files** (``~/.config/ct/ct.conf`` on Linux)
4. **System config files** (``/etc/xdg/ct/ct.conf`` on Linux)
5. **Built-in defaults** (lowest priority)

The configuration system uses python-configargparse and follows XDG directory
conventions on Linux.

SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1), ``ct-list-variants`` (1)
