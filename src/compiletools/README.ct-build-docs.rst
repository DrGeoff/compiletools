==============
ct-build-docs
==============

-----------------------------------------------------------
Build the compiletools documentation site locally.
-----------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-04
:Version: 8.3.0
:Manual section: 1
:Manual group: developers


SYNOPSIS
========
    ct-build-docs [--help]

DESCRIPTION
===========
``ct-build-docs`` is a thin wrapper around ``sphinx-build`` that builds the
compiletools documentation site from the project's RST sources. The site
is the same one served at https://drgeoff.github.io/compiletools/ -- see
``.github/workflows/docs.yml`` for the deploy pipeline that runs on every
``vX.Y.Z`` tag push.

The build sources are:

* ``README.rst`` -- project landing page
* ``src/compiletools/README.ct-*.rst`` -- one page per ct-* tool
* ``README.coders`` -- contributor guide

Output is written to ``docs/_build/html/``. Per-tool stub ``.rst`` files
are auto-generated into ``docs/_generated/``. Both directories are
gitignored.

The build runs with ``-W`` (warnings are errors), so dead cross-references
or RST syntax errors fail the build immediately.

REQUIREMENTS
============
The ``[docs]`` optional-dependency group must be installed:

.. code-block:: bash

    uv pip install -e ".[docs]"

This pulls in ``sphinx``, ``furo``, and ``sphinx-copybutton``.

USAGE
=====

.. code-block:: bash

    scripts/ct-build-docs

After a successful build, open ``docs/_build/html/index.html`` in a
browser.

SEE ALSO
========
* ct-release (1) -- tag a new release; tag pushes trigger the docs deploy
* sphinx-build (1)
