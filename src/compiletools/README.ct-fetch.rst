========
ct-fetch
========

----------------------------------------------------------------------------------
Clone, update, or report the //#GIT= external repositories a build depends on
----------------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-07-01
:Version: 11.0.0
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

A URL that ends in ``/`` (empty basename) is rejected, as is any URL whose
basename would be an unsafe directory name (``.``, ``..``, a dot-leading
name, or one containing a path separator) — such a name could otherwise
escape the externals directory. This derived name is the key used by
``--git-path`` and ``CT_GIT_PATH_<NAME>`` overrides (matched
case-insensitively).

Externals directory
-------------------
By default externals are cloned as **siblings of the project's git
root** — each external ``<name>`` lands at ``../<name>`` relative to the
gitroot. Override the location with ``--externals-dir`` (or the
``CT_EXTERNALS_DIR`` environment variable).

Because externals live OUTSIDE the gitroot, they are not part of the
project's own content-addressable cache identity — consistent with
compiletools' per-workspace caching model.

One backend cannot consume the sibling default: **bazel**. Its hermetic
sandbox rejects include paths outside the workspace, so a
``ct-cake --backend=bazel`` build cannot see an external cloned at
``../<name>`` and the consumer compile fails with
``<name>/...: No such file or directory``. For bazel builds, point
``--externals-dir`` (or ``CT_EXTERNALS_DIR``) at a directory INSIDE the
gitroot — e.g. ``--externals-dir=externals`` run from the gitroot (a
relative value resolves against the current directory) — so the clone
becomes part of the workspace bazel sandboxes. All other backends
(make, ninja, cmake, shake) work with the sibling default. See
ct-backends(7).

Authentication (private and enterprise hosts)
---------------------------------------------
compiletools performs no authentication of its own: it runs plain ``git``
against your ``//#GIT=`` URLs and **honours your ambient git
configuration**. Whatever lets you ``git clone`` the URL by hand — an
``https`` credential helper, an ssh key/agent, a ``url.*.insteadOf``
rewrite, or an HTTP(S) proxy configured in ``~/.gitconfig`` /
``/etc/gitconfig`` — is exactly what the fetch step uses. A ``//#GIT=``
pointing at a private or corporate host therefore "just works" once you
have authenticated; no extra flags are required.

Two guardrails are applied on top of your configuration:

* **Fail fast, never hang.** Git runs with ``GIT_TERMINAL_PROMPT=0`` and
  (for the ssh transport) ``-o BatchMode=yes``, so an external you cannot
  authenticate to fails with a clear error instead of blocking the build
  on an interactive username/password or host-key prompt. If you have
  already set ``GIT_SSH_COMMAND``, your value is preserved.
* **No ambient-repo hijack.** Any inherited ``GIT_DIR`` /
  ``GIT_WORK_TREE`` / ``GIT_INDEX_FILE`` / ``GIT_OBJECT_DIRECTORY`` /
  ``GIT_COMMON_DIR`` / ``GIT_NAMESPACE`` is dropped before operating on an
  external, so a run inside a git hook or CI step cannot accidentally act
  on the enclosing repository instead of the external.

Transport protocol restriction
------------------------------
Because a ``//#GIT=`` URL is untrusted input read from source files (including
the headers of transitively-fetched externals), fetch runs git with
``GIT_ALLOW_PROTOCOL=file:git:ssh:http:https`` by default. This deliberately
excludes git's ``ext::`` remote-helper protocol, which would otherwise execute
an arbitrary shell command at fetch time (``//#GIT=ext::<cmd> ...``) — before
you have reviewed anything that was fetched.

If a project genuinely needs a wider protocol set, declare it with a
``//#GIT_ALLOW_PROTOCOL=`` magic comment whose value is a colon-separated git
protocol list::

    //#GIT_ALLOW_PROTOCOL=file:git:ssh:http:https:ext
    //#GIT=ext::...

The named protocols are added to (not substituted for) the default set, and
only declarations in your **own** project's sources are honored — a
``//#GIT_ALLOW_PROTOCOL`` found in a fetched external's headers is ignored, so a
dependency cannot widen the transport set on your behalf. A ``GIT_ALLOW_PROTOCOL``
you export in your own environment always wins over both the default and any
declaration.

Transitive externals
---------------------
Discovery iterates to a fixpoint: an external's own headers may declare
further ``//#GIT=`` externals, and those are fetched too (deps-of-deps).
Each round widens the include search into the externals fetched so far,
so a chain of externals is resolved in a handful of rounds.

Preprocessor conditionals
-------------------------
``//#GIT=`` declarations are discovered from the raw source text and are **not**
filtered by ``#if`` / ``#ifdef`` state (unlike ``//#CPPFLAGS=`` and the other
magic flags). Correctly evaluating a conditional can require headers that live
inside an external that has not been fetched yet, so discovery cannot depend on
the conditional's outcome. Consequences:

* a ``//#GIT=`` inside a dead ``#if 0`` (or an inactive platform branch) is
  still fetched;
* pinning the **same** external to **different** refs in mutually-exclusive
  ``#if`` branches is a hard error (conflicting refs), not a per-branch choice.

Keep per-configuration externals in separate source files, or pin a single ref,
if you need conditional selection.

Safety
------
* ``--update`` refuses to clobber a **dirty** working tree: if a managed
  external has **tracked** uncommitted changes, the update is a hard
  error rather than a forced checkout. Untracked files (build artifacts,
  editor scratch) do NOT count as dirty and never block an update.
* An immutable ref (tag or SHA) is only checked out when the tree is
  clean; a no-op when HEAD already matches. A ref that is both a tag and
  a branch name is treated as the (immutable) tag, with a warning.
* A clone or checkout that fails partway leaves **no** partial checkout
  behind: an external is staged in a temporary sibling directory and
  renamed into place only on full success.
* A non-git directory that already sits at a managed location is used
  as-is (never cloned over) with a warning — except under ``--update``,
  where compiletools cannot manage a non-git directory and reports a
  hard error.
* A **linked git worktree** (``git worktree add``) at a managed location
  is never mutated: the sibling-directory default is exactly where users
  keep their own worktrees, and a checkout or pull there would move the
  HEAD of a checkout being actively worked in (and write refs into its
  main repository). A worktree that already satisfies the declaration
  (unpinned, or sitting at the requested tag/SHA) is used as-is; a
  differing ref or any ``--update`` is a hard error suggesting
  ``--git-path`` or ``--externals-dir``.
* Two ``//#GIT=`` declarations of the **same name** are a hard error when
  they disagree — whether on the **URL** or on the **ref** — and the
  error names both declaring files. Two externals whose derived names
  collide only in case (e.g. ``mylib`` and ``MyLib``) are also a hard
  error, because overrides key on the lowercased name. (In ``--status``
  mode a URL/ref conflict only warns — a status report never fails.)
* A malformed value is rejected up front: a URL or ref that begins with
  ``-`` (which git would misread as an option) and a URL with no ``/``
  or ``:`` separator are refused with a clear message.
* A ``//#GIT=`` URL runs through git with a restricted
  ``GIT_ALLOW_PROTOCOL`` (``file:git:ssh:http:https``) so an ``ext::``
  remote-helper URL cannot execute an arbitrary command at fetch time. Widen it
  per-project with a ``//#GIT_ALLOW_PROTOCOL=`` comment when required.

OPTIONS
=======
``filename [filename ...]``
    Target source file(s) to scan for ``//#GIT=`` declarations. Combine
    with ``--static`` / ``--dynamic`` / ``--tests`` to scan library and
    test targets too. Files that do not exist on disk are ignored; if no
    target files exist, ``ct-fetch`` prints a note and exits 0.

``--no-fetch``
    Offline: error if a ``//#GIT`` external is missing; never clone or
    fetch. ``--no-fetch`` is the unconditional offline guarantee and takes
    precedence over ``--update``: a branch or unpinned external that would need
    a network fetch/fast-forward to update is a **hard error** under
    ``--no-fetch`` rather than being silently skipped. Present, already-current
    externals are still used. Use to verify that every declared external is
    already present.

``--update``
    Pull / fast-forward branch and unpinned externals to their latest
    tip before reporting. Immutable (tag/SHA) externals are already
    deterministic and are left as-is. Refuses on a dirty tree, on a
    managed location that is a non-git directory, and on a branch
    external whose HEAD is detached (the checkout was pinned and then
    unpinned) — the last with a clear message rather than git's opaque
    "not currently on a branch". Note that **without** ``--update`` a
    branch external is compared against its possibly-stale
    remote-tracking tip, so an upstream force-push is not detected until
    the next ``--update``. Combining ``--update`` with ``--no-fetch`` is a
    hard error for any branch or unpinned external (the fast-forward needs
    the network); pinned immutable refs already present are unaffected.

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
    checkout (e.g. one you are actively editing). The ``PATH`` must be an
    existing directory; a missing path or a non-directory is a hard
    error. ``NAME`` is matched case-insensitively against the URL-derived
    external name.

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
    refused by ``--update``, a ``--git-path`` target that is missing or
    is not a directory, conflicting URLs **or** refs for the same
    external name, two names that collide only in case, a non-git
    directory or a detached-HEAD branch external under ``--update``, a
    malformed ``//#GIT=`` value (leading ``-`` or missing separator), or
    a non-converging fixpoint. The message names the offending external
    and its URL; no traceback is printed.
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

When ``ct-cake --filelist`` (a read-only source listing) drives this fetch
step, it runs **offline** (as if ``--no-fetch``): already-present externals are
folded into the list, but a not-yet-cloned external fails fast rather than
triggering a network clone as a side effect of a query. Run ``ct-fetch`` (or a
plain ``ct-cake`` build) first to populate externals, then re-query.
