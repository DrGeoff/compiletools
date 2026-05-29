==============
ct-build-docs
==============

-----------------------------------------------------------
Build the compiletools documentation site locally.
-----------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-04
:Version: 10.1.3
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

WHY ``docs/`` EARNS ITS KEEP
============================
The actual content lives in the per-tool ``README.ct-*.rst`` files in
``src/compiletools/`` and the root ``README.rst``. Those READMEs are
flat -- no cross-links, no search, no theme, no auto-generated tool
index. ``docs/`` is the small Sphinx glue (``conf.py``, ``index.rst``,
``tools.rst``, ``contributing.rst``) that turns them into a navigable
HTML site. Specifically:

* ``docs/conf.py`` -- Sphinx project config; pulls name/version/authors
  from ``pyproject.toml`` (no duplication); registers a ``setup()`` hook
  that scans ``src/compiletools/README.ct-*.rst`` and writes one stub
  ``.rst`` per tool into ``docs/_generated/`` so newly-added tools
  appear in the site automatically without manual toctree edits.
* ``docs/index.rst`` -- landing page; ``.. include::`` of root
  ``README.rst`` plus a hidden toctree linking the other pages.
* ``docs/tools.rst`` -- ``:glob:`` toctree wrapper that pulls in every
  ``_generated/ct-*`` stub.
* ``docs/contributing.rst`` -- ``literalinclude`` of ``README.coders``
  (which uses shell-style comments, not RST) so it can still appear
  in the site without breaking the RST parser.

The directory pulls double duty:

* **Published-site source** -- the deploy pipeline at
  ``.github/workflows/docs.yml`` builds from here and pushes to
  ``drgeoff.github.io/compiletools/`` on every ``vX.Y.Z`` tag.
* **RST validity gate** -- ``src/compiletools/test_docs_build.py``
  runs the same ``-W`` build under pytest so malformed RST in any
  ``README.ct-*.rst`` (broken cross-reference, malformed table,
  duplicate label, unknown directive) fails CI on every PR rather
  than silently rotting until release.

Even if no human ever browsed the published site, the validator role
alone justifies keeping the ``docs/`` glue.

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
