================
ct-watch-build
================

-------------------------------------------------------------
Continuous rebuild helper that watches sources with inotify
-------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-watch-build [CT-CAKE OPTIONS]

DESCRIPTION
===========
``ct-watch-build`` wraps ``ct-cake`` in a simple inotify-powered loop so that
every save automatically triggers a rebuild. Each iteration:

1. Runs ``inotifywait -e modify,close_write,move,delete $(ct-cake --file-list)``,
   using command substitution to regenerate the watch list on the fly before
   blocking for changes.
2. Executes ``ct-cake "$@"`` the moment ``inotifywait`` reports a change,
   relying on ct-cake's default automatic target detection.

The tool never touches your Makefiles and stops only when you press Ctrl+C. All
arguments passed to ``ct-watch-build`` are forwarded to the ``ct-cake`` command
so you can continue to specify variants, cache settings, output directories,
magic modes, etc.

REQUIREMENTS
============
* ``inotifywait`` from the ``inotify-tools`` package must be installed and
  available on ``PATH`` (Linux only).
* ``ct-cake`` must be configured correctly for the project you are watching.

OPTIONS
=======
No bespoke options are defined. Use the normal ``ct-cake`` flags such as:

``--variant VARIANT``   Build with the given variant (release, debug, ...).

``--magic {direct,cpp}``   Select the magic include processing mode.

``--objdir PATH`` / ``--bindir PATH``   Override output directories.

See ``ct-cake`` (1) for the full CLI reference.

EXAMPLES
========

Watch and rebuild continuously using the default variant::

    ct-watch-build

Watch while building with release flags and a custom objdir::

    ct-watch-build --variant=release --objdir=/tmp/ct-obj

SEE ALSO
========
``ct-cake`` (1), ``ct-build`` (1), ``inotifywait`` (1)
