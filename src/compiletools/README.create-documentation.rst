====================
create-documentation
====================

------------------------------------------------------------
Generate man(1) pages from the reStructuredText source tree
------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
scripts/create-documentation

DESCRIPTION
===========
``create-documentation`` is a helper script that converts the project's
reStructuredText manuals into ``man1/`` pages via ``rst2man`` (part of
``docutils``). The script dynamically resolves paths relative to its location,
so it works correctly regardless of the current working directory.

At runtime the script:

1. Creates ``man1/`` at the repository root and removes any stale ``*.1`` files.
2. Iterates over a mapping of ``README.*.rst`` files to man page names, warning
   if any source file is missing and continuing with the rest.

OUTPUT
======
Each ``README`` entry in the script's ``DOCS`` array produces a ``man1/<name>.1``
file. To add new documentation, append an entry in the format
``"README.foo.rst:foo.1"`` to the array.

REQUIREMENTS
============
* ``docutils`` installed so that ``rst2man`` is on ``PATH``.
* Permission to create/modify the ``man1/`` directory inside the repository.

SEE ALSO
========
``rst2man`` (1), ``docutils`` (1), ``README.ct-doc`` (1)
