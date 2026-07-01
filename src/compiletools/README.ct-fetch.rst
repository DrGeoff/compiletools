========
ct-fetch
========

----------------------------------------------------------------------------------
Clone, update, or report the //#GIT= external repositories a build depends on
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-07-01
:Version: 10.1.11
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-fetch [--no-fetch] [--update] [--status] [--externals-dir DIR]
              [--git-path NAME=PATH] [--variant VARIANT]
              [--static ...] [--dynamic ...] [--tests ...]
              filename [filename ...]

DESCRIPTION
===========
``ct-fetch`` resolves the ``//#GIT=`` external git repositories declared
by a set of target source files, WITHOUT running a build. It is the
standalone counterpart to the automatic fetch step that ``ct-cake``
performs during ``--auto`` builds: both share the same discovery and
resolution machinery, so ``ct-fetch`` is useful for priming externals
ahead of a build (e.g. in a CI ``git clone`` step) and for inspecting
what a target tree pulls in.

The //#GIT= magic comment
-------------------------
A ``//#GIT=`` magic comment placed in a C or C++ source or header tells
compiletools to fold an external git repository into the build. It is a
way to combine code from several source repositories into one executable
without imposing git submodules or subtrees on the project:

.. code-block:: cpp

    //#GIT=https://github.com/me/mylib.git
    #include "mylib/widget.h"

On a ``ct-cake`` build, compiletools scans each target (and its
transitive headers) for ``//#GIT=`` declarations, clones each external
into an *externals directory*, and adds the external's root directory and
its ``include/`` subdirectory (when present) to the include path — so the
main repository can ``#include`` and link the external's sources.

Syntax
------
The value has the form ``<url>`` or ``<url>@<ref>``::

    //#GIT=<url>
    //#GIT=<url>@<ref>

* ``<url>`` is any git remote URL: an ``https://`` / ``git://`` URL, an
  scp-style ``git@host:path`` shorthand, or a ``file://`` URL. It must
  contain a ``/`` or ``:`` separator.
* ``@<ref>`` is **optional**. ``<ref>`` may be a branch, a tag, or a
  commit SHA. When omitted, the external is left on the remote's default
  branch as a plain clone leaves it.

The URL/ref boundary is found by locating the ``@`` that appears after
the rightmost ``/`` or ``:`` in the value, so both
``https://host/me/lib.git@v1`` and ``git@host:me/lib.git@v1`` parse
correctly (the ``@`` in the scp shorthand is not mistaken for the ref
separator).

**Limitation:** a branch ref that itself contains a ``/`` (e.g.
``feature/foo``) is **not** supported in the inline ``@`` form — the
trailing ``/`` defeats the boundary heuristic. Pin to a tag or a commit
SHA instead.

Derived external name
---------------------
The on-disk directory name is derived from the URL basename: the
substring after the rightmost ``/`` or ``:``, with a single trailing
``.git`` suffix stripped:

* ``git@github.com:me/mylib.git`` -> ``mylib``
* ``https://github.com/me/mylib.git`` -> ``mylib``
* ``file:///tmp/x/mylib`` -> ``mylib``

A URL that ends in ``/`` (empty basename) is rejected. This derived name
is the key used by ``--git-path`` and ``CT_GIT_PATH_<NAME>`` overrides
(matched case-insensitively).

Externals directory
-------------------
By default externals are cloned as **siblings of the project's git
root** — each external ``<name>`` lands at ``../<name>`` relative to the
gitroot. Override the location with ``--externals-dir`` (or the
``CT_EXTERNALS_DIR`` environment variable).

Because externals live OUTSIDE the gitroot, they are not part of the
project's own content-addressable cache identity — consistent with
compiletools' per-workspace caching model.

Transitive externals
---------------------
Discovery iterates to a fixpoint: an external's own headers may declare
further ``//#GIT=`` externals, and those are fetched too (deps-of-deps).
Each round widens the include search into the externals fetched so far,
so a chain of externals is resolved in a handful of rounds.

Safety
------
* ``--update`` refuses to clobber a **dirty** working tree: if a managed
  external has uncommitted changes, the update is a hard error rather
  than a forced checkout.
* An immutable ref (tag or SHA) is only checked out when the tree is
  clean; a no-op when HEAD already matches.
* A non-git directory that already sits at a managed location is used
  as-is (never cloned over) with a warning.
* Two ``//#GIT=`` declarations of the **same name with different URLs**
  are a hard error. The same name with a **different ref** warns and the
  first declaration wins. (In ``--status`` mode both cases only warn —
  a status report never fails.)

OPTIONS
=======
``filename [filename ...]``
    Target source file(s) to scan for ``//#GIT=`` declarations. Combine
    with ``--static`` / ``--dynamic`` / ``--tests`` to scan library and
    test targets too. Files that do not exist on disk are ignored; if no
    target files exist, ``ct-fetch`` prints a note and exits 0.

``--no-fetch``
    Offline: error if a ``//#GIT`` external is missing; never clone or
    fetch. Use to verify that every declared external is already present.

``--update``
    Pull / fast-forward branch and unpinned externals to their latest
    tip before reporting. Immutable (tag/SHA) externals are already
    deterministic and are left as-is. Refuses on a dirty tree.

``--status``
    Report the on-disk state of each ``//#GIT`` external
    (present / missing / dirty); never clone or update, and never fail on
    a missing external. Takes precedence over ``--no-fetch`` and
    ``--update``.

``--externals-dir DIR``
    Directory under which ``//#GIT`` externals are cloned (default: the
    parent dir of the git root, i.e. siblings ``../<name>``). Also
    settable via the ``CT_EXTERNALS_DIR`` environment variable.

``--git-path NAME=PATH``
    Override an external's location: ``NAME=absolute/path`` (repeatable;
    or set ``CT_GIT_PATH_<NAME>``). CLI wins over env. A matched external
    is used verbatim from the given path — never cloned, fetched, or
    checked out — so this points compiletools at an existing local
    checkout (e.g. one you are actively editing). ``NAME`` is matched
    case-insensitively against the URL-derived external name.

MODES
=====
The four operating modes are mutually exclusive, in this precedence
(highest first):

``--status``
    Report-only. Prints one tab-separated line per external:
    ``name<TAB>ref<TAB>state<TAB>on_disk_ref<TAB>path`` where *state* is
    ``present``, ``missing``, or ``dirty``, *ref* is the requested ref
    (or ``-``), and *on_disk_ref* is the commit SHA currently checked out
    (or ``-``). Never clones, never fails on a missing external, and only
    reaches into externals already present on disk. ``present`` means a
    checkout exists — it does NOT assert that ``on_disk_ref`` matches the
    requested ``ref``; compare the two columns to spot divergence.

``--no-fetch``
    Verify presence offline. A missing managed external (or a ref not
    present locally) is a hard error.

``--update``
    Clone any missing external, and pull / fast-forward branch and
    unpinned externals to their latest tip. Refuses on a dirty tree.

default
    Clone any missing external; leave present ones as-is.

The non-status modes print one tab-separated summary line per resolved
external: ``name<TAB>ref<TAB>source<TAB>path`` where *source* is
``managed`` (compiletools owns the checkout under the externals
directory) or ``override`` (a ``--git-path`` / ``CT_GIT_PATH_<NAME>``
target).

EXIT CODES
==========
0
    Success (including the no-targets case, where there is nothing to do,
    and ``--status`` regardless of how many externals are missing).
1
    A ``FetchError`` — e.g. git not installed, a clone/checkout failure,
    an external missing under ``--no-fetch``, a dirty-tree clobber
    refused by ``--update``, a missing ``--git-path`` target, conflicting
    URLs for the same external name, a malformed ``//#GIT=`` value, or a
    non-converging fixpoint. The message names the offending external and
    its URL; no traceback is printed.
2
    Argument-parsing failure (e.g. an unknown flag).

EXAMPLES
========
**Clone every external declared by a target (default mode)**::

    ct-fetch main.cpp

**Report status without touching the network**::

    ct-fetch --status main.cpp

**Verify all externals are present, offline (fail if any is missing)**::

    ct-fetch --no-fetch main.cpp

**Update branch/unpinned externals to their latest tip**::

    ct-fetch --update main.cpp

**Clone into an explicit directory instead of the sibling default**::

    ct-fetch --externals-dir=/scratch/externals main.cpp

**Point at a local checkout you are actively editing**::

    ct-fetch --git-path mylib=/home/me/src/mylib main.cpp

**Include library and test targets in the scan**::

    ct-fetch --static libfoo.cpp --tests test_foo.cpp app.cpp

SEE ALSO
========
``ct-magicflags`` (1) -- shows the ``//#GIT=`` (and other) magic
comments a file exports.
``ct-cake`` (1) -- the build orchestrator; runs this same fetch step
automatically during ``--auto`` builds.
