============
ct-config
============

--------------------------------------------
Helper tool for examining ct-* configuration
--------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2016-08-16
:Copyright: Copyright (C) 2011-2016 Zomojo Pty Ltd
:Version: 7.0.2
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

    variant = debug
    variantaliases = {'debug':'blank', 'release':'blank.release'}
    exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
    testmarkers = unit_test.hpp
    max_file_read_size = 0

The second layer of config files are the variant configs that contain the
details for the debug/release/etc.  The variant names are simply a config file
name but without the .conf. There are also variant aliases to make for less
typing. So ``--variant=debug`` looks up the variant alias (specified in ct.conf)
and notices that "debug" really means "blank".  So the config file that
gets opened is ``blank.conf``.

The ``blank.conf`` file is intentionally empty, inheriting all settings from the
environment or parent configs. This allows environment variables (CC, CXX,
CFLAGS, etc.) to control the build without explicit config file settings.
When no environment variables are set, the built-in defaults apply (gcc with
debug flags).

To use explicit compiler configs, either specify them directly
(``--variant=gcc.debug``) or customize the aliases in your
``~/.config/ct/ct.conf``:

.. code-block:: ini

    variantaliases = {'debug':'gcc.debug', 'release':'gcc.release'}

For reference, the ``gcc.debug.conf`` variant defines:

.. code-block:: ini

    ID=GNU
    CC=gcc
    CXX=g++
    LD=g++
    CFLAGS=-fPIC -g -Wall
    CXXFLAGS=-std=c++17 -fPIC -g -Wall
    LDFLAGS=-fPIC -Wall -Werror -Xlinker --build-id

If any config value is specified in more than one way then the following
hierarchy is used to overwrite the final value

* command line > environment variables > config file values > defaults

If you need to append values rather than replace values, this can be 
done (currently only for environment variables) by specifying 
--variable-handling-method append 
or equivalently add an environment variable 
VARIABLE_HANDLING_METHOD=append

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
    shared-objects = true

    # Number
    max_file_read_size = 0

    # Python dict (for variant aliases)
    variantaliases = {'debug':'blank', 'release':'blank.release'}

    # Python list (for markers)
    exemarkers = [main(,main (,wxIMPLEMENT_APP]

**Environment Variable Mapping**

Command-line options automatically map to environment variables by:

1. Removing leading dashes
2. Converting to uppercase
3. Replacing dashes with underscores

.. code-block:: bash

    --variant=release    -> VARIANT=release
    --shared-objects     -> SHARED_OBJECTS=true
    --append-CXXFLAGS    -> APPEND_CXXFLAGS="-O2"

**Common Configuration Options**

Base configuration (ct.conf):

.. code-block:: ini

    variant = debug                                    # Default variant
    variantaliases = {'debug':'blank', 'release':'blank.release'}
    exemarkers = [main(,main (,wxIMPLEMENT_APP,g_main_loop_new]
    testmarkers = unit_test.hpp
    max_file_read_size = 0                            # 0 = read entire file
    # shared-objects = true                           # Enable shared object cache
    # objdir = /path/to/cache                         # Object file cache location

Variant configuration (e.g., gcc.debug.conf):

.. code-block:: ini

    ID = GNU                                          # Compiler identifier
    CC = gcc                                          # C compiler
    CXX = g++                                         # C++ compiler
    LD = g++                                          # Linker
    CFLAGS = -fPIC -g -Wall                          # C compiler flags
    CXXFLAGS = -std=c++17 -fPIC -g -Wall             # C++ compiler flags
    LDFLAGS = -fPIC -Wall -Werror                    # Linker flags

EXAMPLE
=======

Say that you are cross compiling to a beaglebone. First off you might discover that the following line worked but was rather tedious to type

* ct-cake main.cpp --CXX=arm-linux-gnueabihf-g++ --CPP=arm-linux-gnueabihf-g++  --CC=arm-linux-gnueabihf-g++ --LD=arm-linux-gnueabihf-g++

What you would really prefer to type is 

* ct-cake main.cpp --variant=bb.debug
* ct-cake main.cpp --variant=bb.release

Which leads you to the question, how do you write the new variant? A variant is just a config file (with extension .conf) so you could simply copy an existing variant config and edit with a text editor. Alternatively, there is an app for that.  The -w option on the ct-config command will write a new config file.

* ct-config --CXX=arm-linux-gnueabihf-g++ --CPP=arm-linux-gnueabihf-g++  --CC=arm-linux-gnueabihf-g++ --LD=arm-linux-gnueabihf-g++ -w ~/.config/ct/bb.debug.conf

Once that has written you should now use your favourite editor to edit ~/.config/ct/bb.debug.conf.  You will probably need to edit the various FLAGS variables.  Most of the other variables can be removed as they will default to the values shown in the file anyway.

Now if almost all you ever do is cross compile to the beaglebone then you might prefer that the "debug" meant "bb.debug" and similarly for release. That is, you really might prefer to type

* ct-cake main.cpp --variant=release   # meaning bb.release
* ct-cake main.cpp                     # meaning bb.debug

To achieve that you have to edit the ct.conf file in ~/.config/ct/ct.conf (or /etc/xdg/ct/ct.conf if you are doing a systemwide setup) to include the following lines

variant = debug
variantaliases = {'debug':'bb.debug', 'release':'bb.release'}

SEE ALSO
========
``compiletools`` (1), ``ct-list-variants`` (1)
