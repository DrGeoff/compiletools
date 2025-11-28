==========
ct-release
==========

---------------------------------------------------------
Automated version bump + build + publish for compiletools
---------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-release {major|minor|patch}

DESCRIPTION
===========
``ct-release`` codifies the house release checklist. Given a bump level, it:

1. Locates the development checkout indicated by ``ctdevdir`` (defaults to
   ``$HOME/compiletools``) and ensures it exists.
2. Verifies that the working tree is clean and the current branch is ``master``.
3. Runs ``bump-my-version bump <level>`` to increment all version references,
   then ``git push`` + ``git push --tags``.
4. Waits briefly so the GitHub tarball is available.
5. Builds fresh artifacts via ``uv build`` and publishes them with
   ``uv publish --token $PYPI_API_TOKEN`` (checking the Simple API to avoid
   duplicates).

If the most recent commit already contains a bump message the script skips the
version increment but continues with the publish step so accidental reruns are
safe.

OPTIONS
=======
``major | minor | patch``
    Required positional argument that controls the bump level passed to
    ``bump-my-version``. The semantic versioning rules from ``pyproject.toml``
    apply.

ENVIRONMENT
===========
``ctdevdir``
    Path to the compiletools Git checkout. Defaults to ``$HOME/compiletools``.

``PYPI_API_TOKEN``
    API token used by ``uv publish``. Must have upload rights to the
    ``compiletools`` project on PyPI.

``GIT_SSH_COMMAND`` and other Git variables
    If you rely on custom SSH settings for pushes, configure them before
    running ``ct-release`` so the script inherits the environment.

REQUIREMENTS
============
* ``git`` with worktree push access.
* ``bump-my-version`` installed (``uv pip install bump-my-version`` or via the
  dev extras).
* ``uv`` for building and publishing.

EXAMPLES
========

Cut a patch release from a clean master checkout::

    ct-release patch

Force a minor release using a non-standard repo location::

    ctdevdir=$HOME/work/compiletools ct-release minor

SEE ALSO
========
``bump-my-version`` (1), ``uv`` (1), ``README.coders`` (development workflow guide)

