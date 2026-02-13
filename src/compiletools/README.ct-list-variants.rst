================
ct-list-variants
================

------------------------------------------------------------
List available build variants
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-16
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 7.0.2
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

It should be noted that a variant is simply a config file without the .conf.
Config files for the ct-* applications are programmatically located using 
python-appdirs, which on linux is a wrapper around the XDG specification. 
The default locations are /etc/xdg/ct/ and $HOME/.config/ct/.  
Configuration is implemented using python-configargparse which automatically 
handles environment variables, command line arguments, system configs, 
and user configs.  Also there are two levels of configs.  There is a ct.conf 
that contains the basic variables that apply no matter what variant 
(i.e, debug/release/etc) is being built.  Then there are variant configs that 
contain the details for the debug/release/etc.

ct.conf can specify a variant aliases map so as to reduce the amount of typing
you need to do for a --variant=some.long.variant.name. As an example,
--variant=debug is actually a variant alias for "gcc.debug".  So the config 
file that gets opened is "gcc.debug.conf".  

If any config value is specified in more than one way then

* command line > environment variables > config file values > defaults

ct-list-variants shows the variant aliases defined on the system, the various
variant configs available, and the ordering in which the configs will be called
if there is any duplication in configuration filenames.

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
    Variant aliases are:
        debug=blank
        release=blank.release
    From highest to lowest priority configuration directories, the possible variants are:
    /home/user/.config/ct
        None found
    /etc/xdg/ct
        blank
        blank.release

Get variants as a simple list::

    ct-list-variants --style=filelist

Show only repository-local variants::

    ct-list-variants --repoonly

SEE ALSO
========
``compiletools`` (1), ``ct-config`` (1)
