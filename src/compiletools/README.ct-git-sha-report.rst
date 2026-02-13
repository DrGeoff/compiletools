=================
ct-git-sha-report
=================

--------------------------------------------------------
Report git blob SHA hashes for files in the repository
--------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2024-11-24
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-git-sha-report [--all | --untracked]

DESCRIPTION
===========
ct-git-sha-report outputs the git blob SHA hash for each file in the
repository. This is useful for:

* Verifying file content integrity
* Creating reproducible build fingerprints
* Detecting file changes without comparing content directly
* Integration with content-addressable caching systems

The output format is compatible with standard sha256sum-style tools::

    <sha>  <path>

OPTIONS
=======

--all, --untracked (aliases)
    Include untracked files (files not committed to git) in addition
    to tracked files. Without this option, only git-tracked files
    are included.

By default, only tracked files are reported. This uses the git index
which is fast and doesn't require opening each file.

EXAMPLES
========

Report SHA hashes for all tracked files:

.. code-block:: bash

    ct-git-sha-report

Report SHA hashes including untracked files:

.. code-block:: bash

    ct-git-sha-report --all

Save the report for later comparison:

.. code-block:: bash

    ct-git-sha-report > build-fingerprint.txt

HOW IT WORKS
============
For tracked files, ct-git-sha-report reads hashes directly from the git
index (very fast, no file I/O required).

For modified but unstaged files, it computes fresh hashes.

For untracked files (with --all), it computes hashes in batches to
avoid file descriptor exhaustion.

DUPLICATE FILE DETECTION
========================
By design, compiletools treats duplicate SHA1 hashes as bugs that need fixing.
This includes zero-byte files, exact copies, and any files with identical
content. The rationale is that duplicate content often indicates:

* Accidental file copies that should be removed
* Placeholder files that need to be properly initialized
* Configuration mistakes or build artifacts that shouldn't be committed

If you intentionally have files with duplicate content, add a unique comment
to each file to make its content distinct and its purpose clear. For example::

    // Placeholder stub for test scenario A
    // See docs/test-scenarios.md for details

This small documentation comment makes the file's purpose explicit and ensures
each file has a unique hash, allowing compiletools to track them individually.

OUTPUT FORMAT
=============
Each line contains::

    <git_blob_sha>  <absolute_file_path>

The SHA is the git blob hash (same as ``git hash-object <file>``),
which uniquely identifies the file content.

SEE ALSO
========
``git-hash-object`` (1), ``ct-cake`` (1)
