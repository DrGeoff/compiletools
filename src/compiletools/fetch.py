"""Parsing and resolution layer for //#GIT=<url>[@<ref>] declarations.

The module has two distinct halves:

* **Parsing layer** (side-effect-free): :class:`GitExternal`,
  :func:`parse_git_value`, :func:`derive_name`,
  :func:`parse_git_declaration`.  No git operations, no filesystem
  access, no network calls.

* **Resolver layer**: :func:`resolve_external` ensures a declared
  external is present on disk at the correct ref and returns a
  :class:`ResolvedExternal`.  It shells out to ``git`` via
  ``subprocess`` (always with ``cwd=``, never ``os.chdir``), following
  the established pattern in ``git_utils.py``.  Every failure raises
  :class:`FetchError` with a message that names the external and its
  URL.

Known v1 limitation
-------------------
Branch refs containing a ``/`` (e.g. ``feature/foo``) are **not
supported** in the inline ``@`` form.  The separator heuristic locates
the URL/ref boundary by finding the ``@`` that appears after the
rightmost ``/`` or ``:``.  A branch name like ``feature/foo`` would
defeat that heuristic because the trailing ``/`` pushes the detected
separator past the ``@`` that precedes the branch name.  Users who need
such refs should pin to a tag or a commit SHA instead.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ExternalStatus",
    "FetchError",
    "GitExternal",
    "ResolvedExternal",
    "collect_target_files",
    "derive_name",
    "extract_git_externals",
    "fetch_externals",
    "gather_external_status",
    "main",
    "parse_git_declaration",
    "parse_git_path_overrides",
    "parse_git_value",
    "resolve_external",
    "resolve_externals_dir",
]

# Prefix of the per-external location-override environment variable. The
# external's name (lowercased) is appended: ``CT_GIT_PATH_<NAME>``.
_ENV_OVERRIDE_PREFIX = "CT_GIT_PATH_"


class FetchError(Exception):
    """A named failure while resolving a //#GIT= external.

    Every message identifies the offending external by name and URL so a
    user can map the failure back to the ``//#GIT=`` declaration that
    produced it.
    """


@dataclass(frozen=True)
class GitExternal:
    """A parsed //#GIT= declaration.

    Attributes:
        name: Directory name derived from the URL basename (no ``.git``
              suffix).
        url:  The git remote URL (scheme, host, and path, without the
              trailing ``@<ref>`` if one was present).
        ref:  Branch, tag, or commit SHA to check out, or ``None`` when
              the declaration carries no explicit ref.
    """

    name: str
    url: str
    ref: str | None


def parse_git_value(value: str) -> tuple[str, str | None]:
    """Parse the value of a ``//#GIT=`` flag into ``(url, ref)``.

    The *value* is the string that follows ``//#GIT=`` in the source
    file.  It may optionally carry a trailing ``@<ref>`` suffix.

    Separator heuristic
    ~~~~~~~~~~~~~~~~~~~
    ``sep = max(value.rfind('/'), value.rfind(':'))``

    An ``'@'`` character at an index **strictly greater than** *sep*
    is treated as the URL/ref separator.  This handles:

    * ``https://…/path.git@v1`` — rightmost separator is ``/`` before
      ``path.git``; the ``@v1`` falls after it.
    * ``git@host:path.git@v1`` — rightmost separator is ``:``
      (scp shorthand); the trailing ``@v1`` falls after it.
    * ``git@host:path.git`` — no ``@`` after the rightmost ``:``, so
      ref is ``None``.

    Args:
        value: Raw flag value; leading/trailing whitespace is stripped.

    Returns:
        A ``(url, ref)`` tuple.  *ref* is ``None`` when no ``@<ref>``
        suffix is present.

    Raises:
        ValueError: If *value* is empty or whitespace-only, if it lacks
                    both a ``/`` and a ``:`` separator (not a valid git
                    URL), if a trailing ``@`` is present with an empty ref,
                    or if the url/ref begins with ``-`` (a git-option
                    injection guard; git-check-ref-format forbids a leading
                    ``-`` in refs and no git URL begins with one).
    """
    value = value.strip()
    if not value:
        raise ValueError("GIT flag value is empty or whitespace-only")

    sep = max(value.rfind("/"), value.rfind(":"))
    if sep == -1:
        # No '/' and no ':' — not a valid git URL. Rejecting here avoids
        # the degenerate mis-split of e.g. 'git@host' into ('git', 'host'),
        # since find('@', 0) would otherwise treat the user/host '@' as the
        # ref separator.
        raise ValueError(f"GIT flag value '{value}' is not a valid git URL: it has no '/' or ':' separator")
    at_idx = value.find("@", sep + 1)

    if at_idx == -1:
        url, ref = value, None
    else:
        url = value[:at_idx]
        ref = value[at_idx + 1 :]
        if not ref:
            raise ValueError(
                f"GIT flag value '{value}' has a trailing '@' with an empty ref; "
                "specify a branch, tag, or commit SHA after '@'"
            )

    # Option-injection guard: a url or ref beginning with '-' would be
    # interpreted by git as an option (e.g. a ref '--upload-pack=<cmd>' is a
    # known RCE vector). No legitimate git URL starts with '-', and
    # git-check-ref-format forbids a leading '-' in a ref, so reject both here
    # with a clear message (belt; the _run_git argv also uses
    # '--end-of-options' as suspenders).
    if url.startswith("-"):
        raise ValueError(f"GIT flag value '{value}': url '{url}' may not begin with '-'")
    if ref is not None:
        if ref.startswith("-"):
            raise ValueError(f"GIT flag value '{value}': ref '{ref}' may not begin with '-'")
        if ":" in ref:
            # git refnames cannot contain ':'; a ':' here means the separator
            # heuristic mis-split an unusual value. Surface it clearly rather
            # than letting a garbage url/ref reach git.
            raise ValueError(
                f"GIT flag value '{value}': ref '{ref}' contains ':' (invalid ref); check the //#GIT= url@ref syntax"
            )

    return url, ref


def derive_name(url: str) -> str:
    """Derive the external's directory name from a git URL.

    Takes the substring after the rightmost ``/`` or ``:`` (whichever
    appears last), then strips a single trailing ``.git`` suffix if
    present.

    Args:
        url: A git remote URL (already stripped of any ``@<ref>``
             suffix).

    Returns:
        The directory name to use for the cloned external.

    Raises:
        ValueError: If the derived name is empty (e.g. the URL ends
                    with ``/``), or is unsafe as a directory name — ``.``,
                    ``..``, a name beginning with ``.``, or one containing a
                    path separator. Such a name would let ``os.path.join``
                    escape the externals dir (e.g. a URL ending ``.../..``
                    yields ``..``), so it is rejected here.

    Examples:
        ``git@github.com:me/mylib.git`` → ``mylib``
        ``https://github.com/me/mylib.git`` → ``mylib``
        ``file:///tmp/x/mylib`` → ``mylib``
        ``git@host:mylib.git`` → ``mylib``
    """
    sep = max(url.rfind("/"), url.rfind(":"))
    basename = url[sep + 1 :]
    if basename.endswith(".git"):
        basename = basename[: -len(".git")]
    if not basename:
        raise ValueError(f"Cannot derive a name from URL '{url}': the basename is empty")
    # Reject names that would escape externals_dir or resolve to it: '.'/'..',
    # any name with a path separator, and dot-leading names (hidden / unusual,
    # and '..'-family). derive_name's basename never contains '/' because sep is
    # the rightmost '/', but a ':'-derived scp path or a '\' could, so guard
    # both separators explicitly.
    if basename in (".", "..") or basename.startswith(".") or "/" in basename or os.sep in basename:
        raise ValueError(f"Cannot derive a safe directory name from URL '{url}': derived name '{basename}' is unsafe")
    return basename


def parse_git_declaration(value: str) -> GitExternal:
    """Parse a ``//#GIT=`` flag value into a :class:`GitExternal`.

    Convenience wrapper around :func:`parse_git_value` and
    :func:`derive_name`.

    Args:
        value: Raw flag value (the string after ``//#GIT=``).

    Returns:
        A :class:`GitExternal` with *name*, *url*, and *ref* populated.

    Raises:
        ValueError: Propagated from :func:`parse_git_value` or
                    :func:`derive_name`.
    """
    url, ref = parse_git_value(value)
    name = derive_name(url)
    return GitExternal(name=name, url=url, ref=ref)


def parse_git_path_overrides(git_paths: list[str], environ=None) -> dict[str, str]:
    """Build a ``name -> absolute path`` override map for //#GIT externals.

    Two contributing sources, in increasing precedence:

    * **Environment** — any variable named ``CT_GIT_PATH_<NAME>`` contributes
      ``<name>`` lowercased -> ``os.path.abspath(value)``.  The suffix after
      the prefix is lowercased so ``CT_GIT_PATH_FOO`` and ``CT_GIT_PATH_foo``
      both map the override onto the external whose derived name is ``foo``.
    * **CLI** — each *git_paths* entry is ``"NAME=PATH"``; it contributes
      ``NAME`` -> ``os.path.abspath(PATH)``.  A CLI entry OVERRIDES an env
      entry for the same name.

    Name normalization
    ~~~~~~~~~~~~~~~~~~
    Override names are matched **case-insensitively** against the URL-derived
    external name.  Both halves normalize the key the same way: **lowercased**.
    Env suffixes are lowercased (env var names are conventionally upper-case)
    and CLI names are lowercased to match, so ``--git-path Foo=/p`` and
    ``CT_GIT_PATH_FOO`` both target the external ``foo``.  :func:`derive_name`
    lower-cases nothing, so an external whose URL basename is mixed-case (e.g.
    ``MyLib``) is matched by an override key given in any case; the consumer
    (:func:`fetch_externals`) lowercases ``ext.name`` at the lookup site so the
    two sides agree.

    Args:
        git_paths: The ``args.git_paths`` list (each ``"NAME=PATH"``); may be
                   empty.
        environ:   Environment mapping to read ``CT_GIT_PATH_*`` from; defaults
                   to :data:`os.environ`.  Pass an explicit dict in tests to
                   avoid mutating the real environment.

    Returns:
        A ``name -> absolute path`` dict.

    Empty-value asymmetry (A14)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    An empty ``CT_GIT_PATH_<NAME>`` env value (or empty suffix) is silently
    **skipped** — an exported-but-empty env var is a common shell accident and
    must not abort a build. An empty CLI ``NAME=`` / ``=PATH`` instead **raises**:
    a CLI flag is a deliberate act, so a malformed one is surfaced immediately.

    Raises:
        FetchError: If a CLI entry lacks ``=``, or has an empty name or empty
                    path. (Empty env entries are skipped, not raised — see above.)
    """
    if environ is None:
        environ = os.environ

    overrides: dict[str, str] = {}

    # Env first (lowest precedence), so a later CLI entry for the same name wins.
    for key, value in environ.items():
        if not key.startswith(_ENV_OVERRIDE_PREFIX):
            continue
        name = key[len(_ENV_OVERRIDE_PREFIX) :].lower()
        if not name or not value:
            continue
        overrides[name] = os.path.abspath(value)

    for entry in git_paths:
        if "=" not in entry:
            raise FetchError(f"--git-path entry '{entry}' is malformed; expected NAME=PATH")
        name, _, path = entry.partition("=")
        name = name.strip().lower()
        path = path.strip()
        if not name:
            raise FetchError(f"--git-path entry '{entry}' has an empty NAME; expected NAME=PATH")
        if not path:
            raise FetchError(f"--git-path entry '{entry}' has an empty PATH; expected NAME=PATH")
        overrides[name] = os.path.abspath(path)

    return overrides


def resolve_externals_dir(explicit: str | None, gitroot: str) -> str:
    """Decide the directory under which //#GIT externals are cloned.

    Args:
        explicit: An explicit ``--externals-dir`` / ``CT_EXTERNALS_DIR`` value,
                  or ``None`` when the user supplied nothing.
        gitroot:  The project's git root (used only when *explicit* is falsy).

    Returns:
        An absolute path.  When *explicit* is truthy it is returned as
        ``os.path.abspath(explicit)``; otherwise the default is the **parent
        directory of the git root** — the "sibling" layout where each external
        ``<name>`` lives next to the project as ``../<name>``.
    """
    if explicit:
        return os.path.abspath(explicit)
    return os.path.dirname(os.path.abspath(gitroot))


# ===========================================================================
# Resolver layer
# ===========================================================================


@dataclass(frozen=True)
class ResolvedExternal:
    """The on-disk outcome of resolving a :class:`GitExternal`.

    Attributes:
        name:        The external's directory name (from ``GitExternal``).
        url:         The git remote URL (from ``GitExternal``).
        ref:         The requested ref, or ``None``.
        path:        Absolute on-disk path where the external lives.
        source:      ``"managed"`` if compiletools owns the checkout under
                     ``externals_dir``; ``"override"`` if the user pointed
                     at an existing checkout via ``override_path``.
        on_disk_ref: The commit SHA currently checked out (best-effort).
                     ``None`` when *path* is not a git work tree or the SHA
                     could not be resolved.
    """

    name: str
    url: str
    ref: str | None
    path: str
    source: Literal["managed", "override"]
    on_disk_ref: str | None


def _warn(message: str) -> None:
    """Emit a non-fatal warning to stderr."""
    print(f"ct-fetch: warning: {message}", file=sys.stderr)


def _git_env() -> dict[str, str]:
    """Return the environment for git operations on externals.

    Design: **honour the user's ambient git configuration.** The user's
    ``~/.gitconfig`` and the system ``/etc/gitconfig`` carry the settings that
    make private/enterprise hosts reachable — ``url.*.insteadOf`` rewrites,
    HTTP(S) proxies, and credential helpers. An earlier version wiped both
    layers (``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_NOSYSTEM=1``) for
    cross-machine determinism, but that broke exactly the enterprise-auth path
    the feature is meant to support: a ``//#GIT=`` URL pointing at a corporate
    host should "just work" once the user has authenticated, with no extra
    flags. We accept that resolution now depends on the user's git config —
    that is their explicit choice, in keeping with the feature's philosophy of
    supporting the environment the user already has.

    We still adjust two families of variables:

    * **Fail-fast, never hang.** ``GIT_TERMINAL_PROMPT=0`` turns an
      unauthenticated/private external into an immediate clear failure instead
      of a build that blocks forever on an interactive username/password or
      host-key prompt. ``GIT_SSH_COMMAND`` gets ``-o BatchMode=yes`` for the
      ssh transport — but only via ``setdefault`` so a user who has already set
      ``GIT_SSH_COMMAND`` keeps their value.
    * **No ambient-repo hijack.** git honours ``GIT_DIR`` / ``GIT_WORK_TREE`` /
      etc. over ``cwd=``, so if ct-fetch runs inside a git hook or a CI step
      that exports them, every ``_run_git(cwd=target)`` would silently operate
      on the *enclosing* repo instead of the external. We drop that family so
      ``cwd=`` is authoritative.
    """
    env = dict(os.environ)
    # Fail fast rather than hang on an interactive prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    # Never let an ambient repo pointer override our explicit cwd=target.
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    ):
        env.pop(var, None)
    return env


def _run_git(args: list[str], *, cwd: str | None, ext: GitExternal) -> subprocess.CompletedProcess:
    """Run ``git <args>`` (capturing output), raising :class:`FetchError` on failure.

    Mirrors ``git_utils.py``'s subprocess discipline: ``cwd=`` is passed
    explicitly (never ``os.chdir``), and both ``CalledProcessError`` and
    ``OSError`` (``git`` not installed) are caught and re-raised as a named
    :class:`FetchError`.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise FetchError(f"external '{ext.name}' ({ext.url}): 'git' is not installed or not on PATH") from exc
    except OSError as exc:
        raise FetchError(f"external '{ext.name}' ({ext.url}): failed to execute git {' '.join(args)}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip()
        raise FetchError(f"external '{ext.name}' ({ext.url}): git {' '.join(args)} failed:\n{output}") from exc


def _is_git_work_tree(path: str) -> bool:
    """Return True only if *path* is itself the **top level** of a git work tree.

    Uses ``git rev-parse --show-toplevel`` and requires it to equal
    ``realpath(path)``. ``--is-inside-work-tree`` is deliberately NOT used: it
    returns true for *any* directory nested inside an enclosing work tree even
    when the directory has no ``.git`` of its own. If a managed external target
    happened to sit under the host project's work tree (a non-default
    ``--externals-dir``), that laxer check would make ``_handle_present`` treat
    a plain subdirectory as a managed checkout and run fetch/checkout/merge with
    ``cwd=target`` — git would walk up to the host ``.git`` and mutate the host
    repo. Requiring the toplevel to be *this* path closes that hijack.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    toplevel = result.stdout.strip()
    if not toplevel:
        return False
    # NOT cached: this is a live host-repo-hijack guard (A19). A stale cached
    # realpath could let a checkout git just created (or a swapped symlink) pass
    # as a work-tree root when it is not -- the safety check must read live state.
    return os.path.realpath(toplevel) == os.path.realpath(path)


def _current_commit(path: str) -> str | None:
    """Return the HEAD commit SHA of the repo at *path*, best-effort."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _is_dirty(ext: GitExternal, path: str) -> bool:
    """Return True if the work tree at *path* has uncommitted changes.

    Only *tracked* modifications count as dirty: ``--untracked-files=no`` is
    passed so build artifacts / IDE files dropped into the external's checkout
    do not wedge ``--update`` (A9). A tracked-file modification still blocks,
    protecting real local edits from being clobbered.

    Raises :class:`FetchError` (naming the external) if ``git status`` cannot
    be run — otherwise a raw traceback would escape without identifying which
    external failed.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise FetchError(f"external '{ext.name}' ({ext.url}): 'git' is not installed or not on PATH") from exc
    except OSError as exc:
        raise FetchError(f"external '{ext.name}' ({ext.url}): failed to run git status in '{path}': {exc}") from exc
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip()
        raise FetchError(f"external '{ext.name}' ({ext.url}): git status in '{path}' failed:\n{output}") from exc
    return bool(result.stdout.strip())


def _rev_parse_verify(path: str, ref: str) -> str | None:
    """Resolve *ref* to a commit SHA within the repo at *path*.

    Returns the SHA, or ``None`` if the ref is not resolvable locally.
    Uses ``{ref}^{{commit}}`` so a tag is peeled to its commit (matching
    what a detached checkout records as HEAD).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _current_branch(path: str) -> str | None:
    """Return the checked-out branch name, or ``None`` if HEAD is detached.

    Uses ``git symbolic-ref -q --short HEAD``: exit 0 with the short branch
    name on a branch, non-zero (empty) on a detached HEAD.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "-q", "--short", "HEAD"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    name = result.stdout.strip()
    return name or None


def _is_branch(path: str, ref: str) -> bool:
    """Return True if *ref* names a local or remote-tracking branch."""
    for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}"):
        if _rev_parse_verify(path, candidate) is not None:
            return True
    return False


def _is_tag(path: str, ref: str) -> bool:
    """Return True if *ref* names a tag (checked under ``refs/tags/``)."""
    return _rev_parse_verify(path, f"refs/tags/{ref}") is not None


def _resolve_override(ext: GitExternal, override_path: str) -> ResolvedExternal:
    """Handle the ``override_path`` case: use verbatim, never mutate."""
    # Must be a directory: os.path.exists would also accept a regular file,
    # silently mis-configuring the include search into a non-checkout (A5).
    # NOT cached: the user-owned override path is read live (a pre-probe cached
    # "missing"/"file" answer could be stale by the time we validate it).
    if not os.path.isdir(override_path):
        if os.path.exists(override_path):
            raise FetchError(
                f"external '{ext.name}' ({ext.url}): --git-path target '{override_path}' is not a directory."
            )
        raise FetchError(f"external '{ext.name}' ({ext.url}): --git-path target missing: '{override_path}'")
    on_disk_ref = _current_commit(override_path) if _is_git_work_tree(override_path) else None
    return ResolvedExternal(
        name=ext.name,
        url=ext.url,
        ref=ext.ref,
        path=os.path.abspath(override_path),
        source="override",
        on_disk_ref=on_disk_ref,
    )


def _clone_missing(ext: GitExternal, target: str, *, no_fetch: bool, verbose: int) -> ResolvedExternal:
    """Clone *ext* into *target* (which does not yet exist)."""
    if no_fetch:
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): not present at '{target}' and "
            f"--no-fetch was given (offline). To fetch it manually run:\n"
            f"    git clone {ext.url} {target}\n"
            f"or point at an existing local checkout with "
            f"--git-path {ext.name}=<path>."
        )
    if verbose:
        print(f"ct-fetch: cloning external '{ext.name}' from {ext.url} into {target}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # A15: clone + ref-checkout into a temp sibling, then atomically rename to
    # target ONLY on full success. A clone that succeeds but whose ref
    # fetch/checkout then fails would otherwise leave a partial checkout at
    # target that a later run treats as "present" and never repairs. The temp
    # is a sibling (same dir → rename is atomic, no cross-device copy) tagged
    # with the pid so concurrent peers (already serialized by the caller's
    # FileLock) never share it. Any failure removes the temp and re-raises.
    tmp = f"{target}.ct-fetch.tmp.{os.getpid()}"
    if os.path.lexists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        # '--end-of-options' guards the untrusted url positional against option
        # injection (git >= 2.24). parse_git_value already rejects a leading-dash
        # url/ref; this is defense-in-depth.
        _run_git(["clone", "--end-of-options", ext.url, tmp], cwd=None, ext=ext)
        if ext.ref is not None:
            # The ref may live on the remote but not be checked out by a plain
            # clone (e.g. a non-default branch or a bare SHA on another branch).
            if _rev_parse_verify(tmp, ext.ref) is None:
                _run_git(["fetch", "origin", "--end-of-options", ext.ref], cwd=tmp, ext=ext)
            _run_git(["checkout", "--end-of-options", ext.ref], cwd=tmp, ext=ext)
        os.rename(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return _managed_result(ext, target)


def _managed_result(ext: GitExternal, target: str) -> ResolvedExternal:
    """Build a ``source="managed"`` result for a git work tree at *target*."""
    return ResolvedExternal(
        name=ext.name,
        url=ext.url,
        ref=ext.ref,
        path=os.path.abspath(target),
        source="managed",
        on_disk_ref=_current_commit(target),
    )


def _checkout_immutable(ext: GitExternal, target: str, *, no_fetch: bool, verbose: int) -> None:
    """Ensure an immutable ref (SHA or tag) is checked out at *target*.

    No-op if HEAD already matches. Fetches first if the ref is not local
    (unless ``no_fetch``). Refuses on a dirty tree.

    Caller guarantees ``ext.ref`` is non-None (immutable refs only).
    """
    assert ext.ref is not None
    ref = ext.ref
    resolved = _rev_parse_verify(target, ref)
    if resolved is not None and resolved == _current_commit(target):
        return  # Already at the requested ref; no network.

    if _is_dirty(ext, target):
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): work tree at '{target}' has "
            f"uncommitted changes; refusing to check out '{ref}' and clobber them."
        )

    if resolved is None:
        if no_fetch:
            raise FetchError(
                f"external '{ext.name}' ({ext.url}): ref '{ref}' is not "
                f"available locally and --no-fetch was given (offline)."
            )
        if verbose:
            print(f"ct-fetch: fetching ref '{ref}' for external '{ext.name}'")
        _run_git(["fetch", "origin", "--end-of-options", ref], cwd=target, ext=ext)

    _run_git(["checkout", "--end-of-options", ref], cwd=target, ext=ext)


def _handle_branch(ext: GitExternal, target: str, *, update: bool, verbose: int) -> None:
    """Handle a branch ref on a present work tree.

    Without ``update``: if HEAD differs from the branch tip, leave as-is
    and warn. With ``update``: refuse on dirty, else fetch + fast-forward.

    Caller guarantees ``ext.ref`` is non-None (a branch name).
    """
    assert ext.ref is not None
    ref = ext.ref
    if not update:
        tip = _rev_parse_verify(target, f"refs/heads/{ref}")
        if tip is None:
            tip = _rev_parse_verify(target, f"refs/remotes/origin/{ref}")
        head = _current_commit(target)
        if tip is not None and head is not None and tip != head:
            _warn(
                f"external '{ext.name}' ({ext.url}): on-disk checkout at "
                f"'{target}' is at {head} but branch '{ref}' tip is {tip}; "
                f"leaving as-is (pass --update to fast-forward)."
            )
        return

    if _is_dirty(ext, target):
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): work tree at '{target}' has "
            f"uncommitted changes; refusing to update branch '{ref}'."
        )
    if verbose:
        print(f"ct-fetch: updating branch '{ref}' for external '{ext.name}'")
    _run_git(["fetch", "origin", "--end-of-options", ref], cwd=target, ext=ext)
    _run_git(["checkout", "--end-of-options", ref], cwd=target, ext=ext)
    _run_git(["merge", "--ff-only", "--end-of-options", f"origin/{ref}"], cwd=target, ext=ext)


def _handle_no_ref(ext: GitExternal, target: str, *, update: bool, verbose: int) -> None:
    """Handle ``ref is None`` on a present work tree: pull current branch on --update."""
    if not update:
        return
    # A detached HEAD has no upstream branch to pull; `git pull --ff-only` would
    # fail with git's opaque "You are not currently on a branch." Detect it and
    # explain the pin/unpin situation instead (A21).
    if _current_branch(target) is None:
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): work tree at '{target}' is on a "
            f"detached HEAD (no branch to fast-forward). It was likely pinned to a "
            f"specific commit or tag; pin the //#GIT= declaration to that ref, or "
            f"check out a branch in '{target}' manually before running --update."
        )
    if _is_dirty(ext, target):
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): work tree at '{target}' has uncommitted changes; refusing to pull."
        )
    if verbose:
        print(f"ct-fetch: pulling current branch for external '{ext.name}'")
    _run_git(["pull", "--ff-only"], cwd=target, ext=ext)


def _handle_present(ext: GitExternal, target: str, *, no_fetch: bool, update: bool, verbose: int) -> ResolvedExternal:
    """Handle a managed target that already exists on disk."""
    if not _is_git_work_tree(target):
        # A6: under --update the user explicitly asked to update this location,
        # but compiletools can't manage a non-git directory — surface that as a
        # hard error rather than silently doing nothing.
        if update:
            raise FetchError(
                f"external '{ext.name}' ({ext.url}): --update was requested but "
                f"the managed location '{target}' is not a git work tree; "
                f"compiletools cannot update it. Remove it, or point "
                f"--git-path {ext.name}=<path> at a real checkout."
            )
        # User-placed (or otherwise non-git) directory. Never clobber it.
        _warn(
            f"external '{ext.name}' ({ext.url}): a non-git directory already "
            f"exists at the managed location '{target}'; using it as-is "
            f"(not cloning over it)."
        )
        return ResolvedExternal(
            name=ext.name,
            url=ext.url,
            ref=ext.ref,
            path=os.path.abspath(target),
            source="managed",
            on_disk_ref=None,
        )

    if ext.ref is None:
        _handle_no_ref(ext, target, update=update, verbose=verbose)
    elif _is_tag(target, ext.ref):
        # A tag is immutable — route it to _checkout_immutable BEFORE the branch
        # check (A22). Testing tag first means a name that exists as both a tag
        # and a branch resolves to the tag (deterministic pin) rather than being
        # fast-forwarded like a branch; warn so the collision is visible.
        if _is_branch(target, ext.ref):
            _warn(
                f"external '{ext.name}' ({ext.url}): ref '{ext.ref}' is both a "
                f"tag and a branch; treating it as the (immutable) tag."
            )
        _checkout_immutable(ext, target, no_fetch=no_fetch, verbose=verbose)
    elif _is_branch(target, ext.ref):
        _handle_branch(ext, target, update=update, verbose=verbose)
    else:
        # Bare SHA (or an unknown ref that is neither branch, tag, nor
        # resolvable SHA — the latter surfaces as a named checkout failure).
        _checkout_immutable(ext, target, no_fetch=no_fetch, verbose=verbose)

    return _managed_result(ext, target)


def resolve_external(
    ext: GitExternal,
    *,
    externals_dir: str,
    no_fetch: bool = False,
    update: bool = False,
    override_path: str | None = None,
    verbose: int = 0,
) -> ResolvedExternal:
    """Ensure *ext* is present on disk at the correct ref; describe where.

    Args:
        ext:           The parsed external to resolve.
        externals_dir: Absolute directory under which ``<ext.name>`` lives;
                       the managed target is ``externals_dir/ext.name``.
        no_fetch:      Offline mode — never hit the network. A managed
                       external that is missing (or a ref not present
                       locally) is a hard error.
        update:        For branch refs (and the no-ref case), pull/fast
                       forward to the latest tip. Ignored for immutable
                       (SHA/tag) refs, which are already deterministic.
        override_path: If given, use this existing checkout verbatim and
                       never clone/fetch/checkout into it. The managed
                       location is left untouched.
        verbose:       Verbosity level; ``>= 1`` prints progress to stdout.

    Returns:
        A :class:`ResolvedExternal` describing the on-disk checkout.

    Raises:
        FetchError: For any named failure (git missing, clone/checkout
                    failure, offline-and-absent, dirty-tree clobber,
                    missing override path, …).
    """
    assert os.path.isabs(externals_dir), f"externals_dir must be absolute, got '{externals_dir}'"
    if override_path is not None:
        return _resolve_override(ext, override_path)

    target = os.path.join(externals_dir, ext.name)
    # Defense-in-depth against a name that escapes externals_dir (derive_name
    # already rejects '.'/'..'/separators, so this should be unreachable for a
    # parsed external, but a hand-built GitExternal could bypass that).
    # NOT cached: this is a security containment boundary (N1); it must resolve
    # symlinks against live state, not a possibly-stale cached realpath.
    anchor = os.path.realpath(externals_dir)
    resolved_target = os.path.realpath(target)  # NOT cached: see above (N1 boundary)
    if resolved_target != anchor and not resolved_target.startswith(anchor + os.sep):
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): resolved target '{resolved_target}' "
            f"escapes the externals directory '{anchor}'; refusing to proceed."
        )
    # A broken symlink at the managed location: os.path.exists follows the link
    # and reports False, so we would otherwise enter _clone_missing and git
    # would fail with an opaque exit-128 that never mentions the symlink.
    if os.path.islink(target) and not os.path.exists(target):
        raise FetchError(
            f"external '{ext.name}' ({ext.url}): a broken symlink exists at the "
            f"managed location '{target}'; remove it (or point --git-path {ext.name}=<path> "
            f"at a real checkout) and retry."
        )
    if not os.path.exists(target):
        return _clone_missing(ext, target, no_fetch=no_fetch, verbose=verbose)
    return _handle_present(ext, target, no_fetch=no_fetch, update=update, verbose=verbose)


# ===========================================================================
# Source-scanning + fixpoint driver
# ===========================================================================
#
# These functions discover //#GIT= declarations in a target's reachable
# on-disk sources and resolve each one, iterating to a fixpoint so that an
# external whose OWN sources declare further //#GIT= externals are fetched
# too (deps-of-deps).  They are pure orchestration over the parsing layer,
# the resolver layer, and the file_analyzer / headerdeps machinery; no
# argparse / CLI wiring lives here (that is a later task).

# Bound the fixpoint loop. A correct run terminates after at most one round
# per distinct external (each round must discover a NEW name or stop), so a
# realistic dependency graph converges in a handful of rounds. The cap turns
# a hypothetical non-converging cycle (e.g. a bug that keeps re-deriving the
# same name as "new") into a clear error rather than an infinite loop.
_MAX_FIXPOINT_ROUNDS = 50

# Upper bound on the thread pool used to resolve (clone/fetch) the externals
# newly discovered in a single fixpoint round. The externals in a round are
# independent (unique names, per-target FileLock sidecars, separate git
# endpoints, each writing only its own result), so they resolve concurrently.
# Capped small: fetch work is network/disk-bound and the count per round is
# typically tiny; the effective worker count is min(len(new), this cap).
_MAX_FETCH_WORKERS = 8


def extract_git_externals(filepath: str, args, context) -> list[GitExternal]:
    """Return a :class:`GitExternal` for every ``//#GIT=`` flag in *filepath*.

    Analyzes a single file via the file_analyzer machinery and parses each
    ``//#GIT=`` magic flag into a :class:`GitExternal`.  Non-GIT magic flags
    are ignored.

    Error policy:
        * A failure to *analyze* the file at all (missing from the registry,
          unreadable, …) is tolerated: it is logged at high verbosity and an
          empty list is returned, so one malformed file cannot abort a whole
          scan.
        * A malformed ``//#GIT=`` *value* is NOT tolerated — the
          :class:`ValueError` from :func:`parse_git_declaration` is wrapped in
          a :class:`FetchError` that names the declaring file, so the user sees
          exactly which declaration is broken.

    Args:
        filepath: Path to the source file to scan.
        args:     The parsed args namespace (file-analyzer attributes such as
                  ``exemarkers`` / ``max_read_size`` must be present;
                  ``set_analyzer_args`` is expected to have been called).
        context:  The :class:`~compiletools.build_context.BuildContext`.

    Returns:
        A list of :class:`GitExternal`, one per ``//#GIT=`` declaration, in
        source order.
    """
    from compiletools.file_analyzer import analyze_file, set_analyzer_args
    from compiletools.global_hash_registry import get_file_hash

    # analyze_file requires analyzer args on the context. fetch_externals sets
    # them via headerdeps construction, but a standalone caller may not have, so
    # set them once here if absent (idempotent for the common shared-context case).
    if context.analyzer_args is None:
        set_analyzer_args(args, context)

    try:
        content_hash = get_file_hash(filepath, context)
        result = analyze_file(content_hash, context)
    except Exception as exc:
        verbose = getattr(args, "verbose", 0)
        if verbose >= 2:
            print(f"ct-fetch: warning: could not analyze '{filepath}' for //#GIT= flags: {exc}", file=sys.stderr)
        return []

    externals: list[GitExternal] = []
    for magic_flag in result.magic_flags:
        if str(magic_flag["key"]) != "GIT":
            continue
        value = str(magic_flag["value"])
        try:
            externals.append(parse_git_declaration(value))
        except ValueError as exc:
            raise FetchError(f"{filepath}: malformed //#GIT= declaration '{value}': {exc}") from exc
    return externals


def _augmented_headerdeps(args, context, *, externals_dir: str, resolved_roots: list[str]):
    """Build a headerdeps instance whose include search reaches into externals.

    Returns ``compiletools.headerdeps.create`` with the external include dirs
    passed through the ``extra_include_dirs`` parameter so the dependency walker
    can traverse INTO already-fetched externals (to discover their transitive
    ``#include`` graph and the further ``//#GIT=`` declarations it reaches).

    The caller's *args* (and its frozen ``args.flags``) are never mutated and
    never copied — the extra dirs are threaded straight into the headerdeps
    instance's include list (``_initialize_includes_and_macros`` for
    DirectHeaderDeps) / preprocessor command (CppHeaderDeps), not back into
    ``CPPFLAGS``. This is why ``BuildContext`` no longer needs a
    ``__deepcopy__``: nothing here deep-copies an args namespace that would drag
    the live context along.
    """
    import compiletools.headerdeps

    include_dirs = [externals_dir]
    for root in resolved_roots:
        include_dirs.append(root)
        include_dirs.append(os.path.join(root, "include"))

    return compiletools.headerdeps.create(args, context=context, extra_include_dirs=include_dirs)


def _reachable_sources(target_files: list[str], headerdeps, args) -> list[str]:
    """Enumerate the reachable on-disk source set for *target_files*.

    For each target, collect the file itself plus every header headerdeps can
    resolve from it.  ``headerdeps.process`` tolerates includes that do not
    resolve on disk (external headers that have not been fetched yet), so a
    not-yet-present include simply contributes nothing this round.  A target
    that cannot be processed at all is skipped with a high-verbosity warning.

    Returns a de-duplicated list in stable discovery order.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            ordered.append(path)

    verbose = getattr(args, "verbose", 0)
    for target in target_files:
        _add(target)
        try:
            headers = headerdeps.process(target, frozenset())
        except Exception as exc:
            if verbose >= 2:
                print(f"ct-fetch: warning: header scan of '{target}' failed: {exc}", file=sys.stderr)
            continue
        for header in headers:
            _add(header)
    return ordered


def _fixpoint_scan(
    target_files: list[str],
    args,
    context,
    *,
    externals_dir: str,
    root_selector,
    scan_round,
    on_not_converged,
) -> None:
    """Shared bounded fixpoint driver for the two ``//#GIT=`` scanners.

    :func:`fetch_externals` and :func:`gather_external_status` share the same
    round loop: build an include-augmented headerdeps that reaches into the
    already-known roots, enumerate the reachable on-disk sources, discover
    ``//#GIT=`` declarations, act on the newly-seen ones, and clear
    ``wrappedos``' path cache between rounds so freshly-fetched sources become
    visible to the next round's ``_find_include``. The bound
    (:data:`_MAX_FIXPOINT_ROUNDS`) turns a hypothetical non-converging cycle
    into a clear error instead of an infinite loop.

    The three callbacks carry the ONLY behaviour that differs between callers
    (this is why the two loops were previously hand-synced duplicates):

    * ``root_selector() -> list[str]`` — which already-known roots to widen the
      header search into. fetch widens into every resolved/cloned root; status
      widens only into roots ALREADY present on disk (it never fetches to expand
      the graph). Evaluated fresh each round.
    * ``scan_round(reachable_sources) -> bool`` — record the round's
      newly-declared externals (deduping by name) and perform the per-mode
      action: fetch resolves the new externals (cloning them in parallel) and
      RAISES :class:`FetchError` on a conflicting URL/ref; status only records
      and WARNS on a conflict, never raising (``--status`` is a report). Returns
      True while a round discovered new work (loop continues) and False once the
      scan converges (loop breaks BEFORE the cache clear, matching the original
      hand-written loops).
    * ``on_not_converged() -> FetchError`` — build the mode-specific
      runaway-loop error, raised when the round bound is hit.

    ``HeaderDepsBase.__init__`` (invoked via ``_augmented_headerdeps`` →
    ``headerdeps.create``) calls ``set_analyzer_args(args, context)``, which
    mutates ``context.analyzer_args`` to the real *args*. The try/finally
    restores the caller's prior value even on error, so a caller reading
    ``context.analyzer_args`` after the scan sees whatever it held before.
    """
    prior_analyzer_args = context.analyzer_args
    try:
        for _round in range(_MAX_FIXPOINT_ROUNDS):
            headerdeps = _augmented_headerdeps(
                args,
                context,
                externals_dir=externals_dir,
                resolved_roots=root_selector(),
            )
            reachable = _reachable_sources(target_files, headerdeps, args)
            if not scan_round(reachable):
                break

            # Fetching (or merely declaring a possibly-present external) can
            # change the filesystem under externals_dir. wrappedos' stat-like
            # queries are globally @functools.cache'd by path, so a "file
            # missing" answer cached while an external header did not yet exist
            # would otherwise stick — blinding the NEXT round's _find_include to
            # the freshly-available sources and breaking transitive discovery.
            # Drop those caches so the next round re-stats from disk.
            import compiletools.wrappedos

            compiletools.wrappedos.clear_cache()
        else:
            raise on_not_converged()
    finally:
        context.analyzer_args = prior_analyzer_args


def fetch_externals(
    target_files: list[str],
    args,
    context,
    *,
    externals_dir: str,
    overrides: dict[str, str] | None = None,
    no_fetch: bool = False,
    update: bool = False,
    verbose: int = 0,
) -> list[ResolvedExternal]:
    """Discover and resolve every ``//#GIT=`` external reachable from *target_files*.

    Iterates to a fixpoint: each round enumerates the current reachable source
    set, scans it for ``//#GIT=`` declarations, and resolves any newly-seen
    external.  Because resolving an external places its sources on disk and the
    include search is widened to reach into it, a subsequent round can discover
    ``//#GIT=`` declarations in that external's own sources (deps-of-deps).  The
    loop ends when a round adds no new external name.

    Args:
        target_files:  Absolute paths of the build target sources to scan.
        args:          Parsed args namespace (file-analyzer + headerdeps
                       attributes present).  Never mutated.
        context:       The :class:`~compiletools.build_context.BuildContext`.
        externals_dir: Absolute directory under which each ``<name>`` lives
                       (typically the parent of the gitroot; the caller
                       computes it).
        overrides:     Optional ``name -> local path`` map (from ``--git-path``
                       / ``CT_GIT_PATH_<name>``); a matched name is used
                       verbatim instead of being cloned.
        no_fetch:      Offline mode — a missing managed external is a hard error.
        update:        Pull/fast-forward branch (and no-ref) externals.
        verbose:       Verbosity level passed through to :func:`resolve_external`.

    Returns:
        A list of :class:`ResolvedExternal` in discovery order.

    Raises:
        FetchError: For a duplicate name with conflicting URLs, a malformed
                    ``//#GIT=`` value, a runaway fixpoint, or any failure
                    propagated from :func:`resolve_external`.
    """
    assert os.path.isabs(externals_dir), f"externals_dir must be absolute, got '{externals_dir}'"
    overrides = overrides or {}

    resolved: dict[str, ResolvedExternal] = {}
    declared: dict[str, GitExternal] = {}
    declared_files: dict[str, str] = {}  # name -> first declaring file (best-effort diagnostics)
    declared_lower: dict[str, str] = {}  # lowercased name -> first-declared original-cased name (N3)

    # --- fetch-mode axes for the shared _fixpoint_scan driver ---------------
    # See _fixpoint_scan for the shared skeleton these three callbacks plug into.
    # fetch differs from status by: widening the search into every RESOLVED root,
    # RAISING on any conflict, and cloning newly-discovered externals (here, in
    # parallel).

    def _root_selector() -> list[str]:
        # fetch reaches into every already-resolved/cloned root.
        return [r.path for r in resolved.values()]

    def _resolve_one(ext: GitExternal) -> ResolvedExternal:
        # Override keys are normalized to lowercase in parse_git_path_overrides;
        # ext.name (from derive_name) preserves the URL-basename case, so
        # lowercase it here to match case-insensitively.
        override_path = overrides.get(ext.name.lower())
        if override_path is not None:
            # User-owned checkout: never cloned/mutated here, so no lock.
            return resolve_external(
                ext,
                externals_dir=externals_dir,
                override_path=override_path,
                no_fetch=no_fetch,
                update=update,
                verbose=verbose,
            )
        # A1: serialize the exists->clone/checkout path against concurrent
        # ct-cake/ct-fetch peers cloning into the same managed dir. Lock a
        # SIDECAR (<target>.lock), never the target itself — locking the target
        # would create an empty dir a peer make treats as up-to-date. No-op
        # unless --file-locking is enabled. The lock lives INSIDE the worker so
        # each parallel external acquires its own sidecar independently.
        import compiletools.locking

        target = os.path.join(externals_dir, ext.name)
        with compiletools.locking.FileLock(target + ".lock", args):
            return resolve_external(
                ext,
                externals_dir=externals_dir,
                override_path=None,
                no_fetch=no_fetch,
                update=update,
                verbose=verbose,
            )

    def _resolve_new(new_names: list[GitExternal]) -> None:
        # The new externals in a round are independent, so resolve them in a
        # bounded thread pool. Determinism is preserved two ways: results are
        # assigned into `resolved` in declaration order (so list(resolved) keeps
        # discovery order), and on failure the FIRST error in declaration order
        # is re-raised (so a conflicting/broken external surfaces the same
        # FetchError it would have sequentially). Every worker is awaited, so no
        # exception is swallowed.
        max_workers = min(len(new_names), _MAX_FETCH_WORKERS)
        if max_workers <= 1:
            for ext in new_names:
                resolved[ext.name] = _resolve_one(ext)
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            submitted = [(ext, executor.submit(_resolve_one, ext)) for ext in new_names]
            computed: dict[str, ResolvedExternal] = {}
            first_error: BaseException | None = None
            for ext, future in submitted:
                try:
                    computed[ext.name] = future.result()
                except BaseException as exc:  # re-raised below, deterministically
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
        for ext in new_names:
            resolved[ext.name] = computed[ext.name]

    def _scan_round(reachable: list[str]) -> bool:
        new_names: list[GitExternal] = []
        for source_file in reachable:
            for ext in extract_git_externals(source_file, args, context):
                prior = declared.get(ext.name)
                if prior is None:
                    # N3: overrides key on the lowercased name, so two names that
                    # differ only in case would silently share one override (and
                    # one on-disk dir on a case-insensitive FS). Reject the
                    # collision up front, naming both files.
                    lower = ext.name.lower()
                    clash = declared_lower.get(lower)
                    if clash is not None and clash != ext.name:
                        raise FetchError(
                            f"case-colliding //#GIT= external names '{clash}' "
                            f"(in {declared_files.get(clash, '?')}) vs '{ext.name}' "
                            f"(in {source_file}); names must be unique case-insensitively "
                            "because --git-path / CT_GIT_PATH_* overrides key on the "
                            "lowercased name."
                        )
                    declared[ext.name] = ext
                    declared_files[ext.name] = source_file
                    declared_lower[lower] = ext.name
                    if ext.name not in resolved:
                        new_names.append(ext)
                    continue
                # Same name seen before — must agree on URL.
                if prior.url != ext.url:
                    raise FetchError(
                        f"conflicting //#GIT= declarations for external '{ext.name}': "
                        f"'{prior.url}' (in {declared_files.get(ext.name, '?')}) vs "
                        f"'{ext.url}' (in {source_file})"
                    )
                # Same name + same URL but a differing ref: hard error, naming
                # both declaring files (N2). Symmetric with the conflicting-URL
                # raise above — a build must not silently pick one of two
                # requested refs. (gather_external_status stays a tolerant warn:
                # --status is a report and never raises.)
                if prior.ref != ext.ref:
                    raise FetchError(
                        f"conflicting //#GIT= refs for external '{ext.name}' ({ext.url}): "
                        f"'{prior.ref}' (in {declared_files.get(ext.name, '?')}) vs "
                        f"'{ext.ref}' (in {source_file})"
                    )
                # Otherwise an exact duplicate — silently deduped.

        if not new_names:
            return False
        _resolve_new(new_names)
        return True

    def _not_converged() -> FetchError:
        return FetchError(
            f"//#GIT= resolution did not converge after {_MAX_FIXPOINT_ROUNDS} rounds; "
            f"resolved so far: {sorted(resolved)}. Declaring files: "
            f"{ {n: declared_files.get(n, '?') for n in sorted(declared)} }"
        )

    _fixpoint_scan(
        target_files,
        args,
        context,
        externals_dir=externals_dir,
        root_selector=_root_selector,
        scan_round=_scan_round,
        on_not_converged=_not_converged,
    )
    return list(resolved.values())


# ===========================================================================
# Report-only status (never clones/updates, never raises on a missing external)
# ===========================================================================


@dataclass(frozen=True)
class ExternalStatus:
    """The report-only, tolerant state of a declared ``//#GIT=`` external.

    Attributes:
        name:        The external's directory name (from :class:`GitExternal`).
        url:         The git remote URL.
        ref:         The requested ref, or ``None`` when unpinned.
        state:       ``"present"`` (an on-disk git work tree exists),
                     ``"dirty"`` (present but with uncommitted changes), or
                     ``"missing"`` (nothing usable on disk). A missing external
                     is NOT an error here — it is a reported state.
                     ``"present"`` means only that an external checkout exists
                     on disk; it does NOT guarantee the checked-out
                     ``on_disk_ref`` matches the requested ``ref``. Compare the
                     two columns to detect divergence. Detecting/flagging that
                     divergence as a distinct ``"stale"`` state is a documented
                     future enhancement.
        path:        Absolute on-disk path where the external is expected to
                     live (an override path, or ``externals_dir/name``).
        source:      ``"managed"`` (compiletools owns the location) or
                     ``"override"`` (user pointed at it via ``--git-path``).
        on_disk_ref: The commit SHA currently checked out, or ``None`` when the
                     external is missing / not a git work tree.
    """

    name: str
    url: str
    ref: str | None
    state: Literal["present", "missing", "dirty"]
    path: str
    source: Literal["managed", "override"]
    on_disk_ref: str | None


def _status_for(ext: GitExternal, *, externals_dir: str, override_path: str | None) -> ExternalStatus:
    """Compute the tolerant on-disk :class:`ExternalStatus` of a single external.

    Never clones, fetches, or checks out. Never raises on a missing external —
    it is reported as ``state="missing"``. Reuses the same git helpers the
    resolver layer uses (:func:`_is_git_work_tree`, :func:`_current_commit`,
    :func:`_is_dirty`).
    """
    if override_path is not None:
        path = os.path.abspath(override_path)
        source: Literal["managed", "override"] = "override"
    else:
        path = os.path.join(externals_dir, ext.name)
        source = "managed"

    if not os.path.exists(path) or not _is_git_work_tree(path):
        return ExternalStatus(
            name=ext.name,
            url=ext.url,
            ref=ext.ref,
            state="missing",
            path=path,
            source=source,
            on_disk_ref=None,
        )

    on_disk_ref = _current_commit(path)
    # _is_dirty raises FetchError only when git status itself fails; in the
    # report path a present-but-unqueryable tree is still "present".
    try:
        dirty = _is_dirty(ext, path)
    except FetchError:
        dirty = False
    return ExternalStatus(
        name=ext.name,
        url=ext.url,
        ref=ext.ref,
        state="dirty" if dirty else "present",
        path=path,
        source=source,
        on_disk_ref=on_disk_ref,
    )


def gather_external_status(
    target_files: list[str],
    args,
    context,
    *,
    externals_dir: str,
    overrides: dict[str, str] | None = None,
) -> list[ExternalStatus]:
    """Report the tolerant on-disk state of every reachable ``//#GIT=`` external.

    Unlike :func:`fetch_externals`, this NEVER clones, fetches, or checks out,
    and NEVER raises on a missing external — a missing external is reported as
    ``state="missing"``. It enumerates declared externals to a fixpoint the
    same way :func:`fetch_externals` does, but only transitively reaches into
    externals that are ALREADY present on disk (it does not fetch to expand the
    graph). A malformed ``//#GIT=`` value still raises :class:`FetchError`
    (via :func:`extract_git_externals`), since that is a source-code defect the
    user must see.

    In report mode, conflicting declarations of the same external are tolerated
    (first declaration wins) and reported as a warning to stderr, rather than
    raising as they do in :func:`fetch_externals`.

    Args:
        target_files:  Absolute paths of the build target sources to scan.
        args:          Parsed args namespace. Never mutated.
        context:       The :class:`~compiletools.build_context.BuildContext`.
        externals_dir: Absolute directory under which each ``<name>`` lives.
        overrides:     Optional ``name -> local path`` map.

    Returns:
        A list of :class:`ExternalStatus` in discovery order (deduped by name).
    """
    assert os.path.isabs(externals_dir), f"externals_dir must be absolute, got '{externals_dir}'"
    overrides = overrides or {}

    declared: dict[str, GitExternal] = {}
    declared_files: dict[str, str] = {}  # name -> first declaring file (best-effort diagnostics)
    ordered_names: list[str] = []
    # status is report-only and never mutates the filesystem, so an external's
    # on-disk state cannot change mid-run. Compute each ExternalStatus at most
    # once and reuse it across rounds (root selection) and in the final pass,
    # instead of re-running its three git subprocesses N*rounds times.
    status_cache: dict[str, ExternalStatus] = {}

    def _status_cached(ext: GitExternal) -> ExternalStatus:
        st = status_cache.get(ext.name)
        if st is None:
            st = _status_for(ext, externals_dir=externals_dir, override_path=overrides.get(ext.name.lower()))
            status_cache[ext.name] = st
        return st

    # --- status-mode axes for the shared _fixpoint_scan driver --------------
    # See _fixpoint_scan for the shared skeleton these callbacks plug into.
    # status differs from fetch by: widening the search only into roots ALREADY
    # PRESENT on disk (it never fetches to expand the graph), and only WARNING on
    # a conflict rather than raising (--status is a report).

    def _root_selector() -> list[str]:
        # Reach into whatever externals are already present on disk (never fetch
        # to widen the graph). A present external's own sources can declare
        # further externals we should report.
        roots: list[str] = []
        for ext in declared.values():
            st = _status_cached(ext)
            if st.state != "missing":
                roots.append(st.path)
        return roots

    def _scan_round(reachable: list[str]) -> bool:
        new_found = False
        for source_file in reachable:
            for ext in extract_git_externals(source_file, args, context):
                prior = declared.get(ext.name)
                if prior is None:
                    declared[ext.name] = ext
                    declared_files[ext.name] = source_file
                    ordered_names.append(ext.name)
                    new_found = True
                    continue
                # Same name seen before. In report mode we never raise on a
                # conflict (mirrors fetch_externals' wording, but tolerant):
                # first declaration wins and the conflict is surfaced as a
                # warning so --status does not silently hide it.
                if prior.url != ext.url:
                    _warn(
                        f"conflicting //#GIT= declarations for external '{ext.name}': "
                        f"'{prior.url}' (in {declared_files.get(ext.name, '?')}) vs "
                        f"'{ext.url}' (in {source_file}); keeping '{prior.url}'."
                    )
                elif prior.ref != ext.ref:
                    _warn(
                        f"external '{ext.name}' ({ext.url}): conflicting refs "
                        f"'{prior.ref}' (in {declared_files.get(ext.name, '?')}) vs "
                        f"'{ext.ref}' (in {source_file}); keeping '{prior.ref}'."
                    )
                # Otherwise an exact duplicate — silently deduped.
        return new_found

    def _not_converged() -> FetchError:
        return FetchError(
            f"//#GIT= status enumeration did not converge after {_MAX_FIXPOINT_ROUNDS} rounds; "
            f"found so far: {sorted(declared)}. Declaring files: "
            f"{ {n: declared_files.get(n, '?') for n in sorted(declared)} }"
        )

    _fixpoint_scan(
        target_files,
        args,
        context,
        externals_dir=externals_dir,
        root_selector=_root_selector,
        scan_round=_scan_round,
        on_not_converged=_not_converged,
    )
    return [_status_cached(declared[name]) for name in ordered_names]


# ===========================================================================
# CLI entry point
# ===========================================================================


def collect_target_files(args) -> list[str]:
    """Flatten existing on-disk source files from the target arg groups.

    Single source of truth for the "reachable targets" set, shared verbatim by
    :func:`main` and ``Cake._fetch_and_register_externals``: de-duplicates
    across ``filename`` / ``static`` / ``dynamic`` / ``tests`` and drops any
    entry that is falsy or not an on-disk file. Both call sites MUST agree on
    this definition, so they call this one function rather than reimplementing
    the loop.

    Note: the ``isfile`` check reads ``wrappedos``' path cache. In a single CLI
    run this is correct (the cache is fresh). Only an in-process re-entry (tests
    calling ``main`` repeatedly, or a caller that created a target file mid-run)
    could observe a stale "missing" answer — ``main`` and the test harness clear
    the cache between runs, so this is a test/re-entry concern only (A13).
    """
    import compiletools.wrappedos

    target_files: list[str] = []
    seen: set[str] = set()
    for group in (args.filename, args.static, args.dynamic, args.tests):
        for path in group or []:
            if path and path not in seen and compiletools.wrappedos.isfile(path):
                seen.add(path)
                target_files.append(path)
    return target_files


def _print_status_report(statuses: list[ExternalStatus]) -> None:
    """Print a stable, greppable one-line-per-external status report to stdout."""
    if not statuses:
        print("ct-fetch: no //#GIT= externals declared by the given targets.")
        return
    for st in statuses:
        ref = st.ref if st.ref is not None else "-"
        on_disk = st.on_disk_ref if st.on_disk_ref is not None else "-"
        print(f"{st.name}\t{ref}\t{st.state}\t{on_disk}\t{st.path}")


def _print_resolved_summary(resolved: list[ResolvedExternal]) -> None:
    """Print a concise one-line-per-external summary of resolved externals."""
    if not resolved:
        print("ct-fetch: no //#GIT= externals declared by the given targets.")
        return
    for r in resolved:
        ref = r.ref if r.ref is not None else "-"
        print(f"{r.name}\t{ref}\t{r.source}\t{r.path}")


def signal_handler(_signum, _frame):
    """Exit cleanly on SIGINT/SIGPIPE (mirrors ``cake.signal_handler``)."""
    sys.exit(0)


def main(argv=None) -> int:
    """Entry point for ``ct-fetch``.

    Clones/updates/reports the ``//#GIT=`` externals reachable from the given
    target source files WITHOUT running a build.

    Modes (mutually-exclusive precedence, highest first):
        * ``--status``   — report-only; never clones/updates and never fails on
                           a missing external (reported as ``missing``).
        * ``--no-fetch`` — verify presence offline; a missing external is a
                           hard error (:class:`FetchError`).
        * ``--update``   — clone missing externals and pull/fast-forward branch
                           (and unpinned) externals to their latest tip.
        * default        — clone any missing external; leave present ones as-is.
    """
    import compiletools.apptools
    import compiletools.git_utils
    import compiletools.headerdeps
    import compiletools.utils
    import compiletools.wrappedos
    from compiletools.build_context import BuildContext

    cap = compiletools.apptools.create_parser(
        "Clone/update/report //#GIT= external git repos without running a build",
        argv=argv,
    )
    # headerdeps.add_arguments pulls in add_common_arguments (verbose, CXX,
    # CPPFLAGS, ...) and file_analyzer.add_arguments (exemarkers, max_read_size)
    # — exactly what fetch_externals' headerdeps walk and extract_git_externals'
    # analyze_file require. Target arguments supply the source files to scan;
    # the fetch-control flags (--no-fetch/--update/--externals-dir/--git-path)
    # come from the shared apptools registrar. magicflags/hunter are NOT needed:
    # fetch's discovery uses headerdeps + file_analyzer directly.
    compiletools.apptools.add_target_arguments(cap)
    compiletools.headerdeps.add_arguments(cap)
    compiletools.apptools.add_fetch_arguments(cap)
    # Registers --file-locking so fetch_externals' FileLock around the managed
    # clone/checkout path is active (A1). Without it args.file_locking is absent
    # and FileLock silently no-ops. ct-cake gets this via its backend parser.
    compiletools.apptools.add_locking_arguments(cap)
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="status",
        dest="status",
        default=False,
        help="Report the on-disk state of each //#GIT external (present/missing/dirty); never clone or update.",
    )

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)

    # Install graceful SIGINT/SIGPIPE handlers for the duration of the run
    # (A11 / CLAUDE.md signal rule): a Ctrl-C during a clone exits cleanly
    # instead of dumping a traceback, and the FileLock's own signal forwarding
    # tears the child git down. Mirrors cake.main()'s wrapper.
    with compiletools.apptools.graceful_shutdown(signal_handler, signal.SIGINT, signal.SIGPIPE):
        try:
            target_files = collect_target_files(args)
            if not target_files:
                print(
                    "ct-fetch: no target source files given (or none exist on disk); nothing to do.",
                    file=sys.stderr,
                )
                return 0

            gitroot = compiletools.git_utils.find_git_root()
            externals_dir = resolve_externals_dir(getattr(args, "externals_dir", None), gitroot)
            overrides = parse_git_path_overrides(getattr(args, "git_paths", []) or [])

            if getattr(args, "status", False):
                # Report-only: never clones/updates, never raises on a missing external.
                statuses = gather_external_status(
                    target_files,
                    args,
                    context,
                    externals_dir=externals_dir,
                    overrides=overrides,
                )
                _print_status_report(statuses)
                return 0

            resolved = fetch_externals(
                target_files,
                args,
                context,
                externals_dir=externals_dir,
                overrides=overrides,
                no_fetch=getattr(args, "no_fetch", False),
                update=getattr(args, "update", False),
                verbose=args.verbose,
            )
            _print_resolved_summary(resolved)
            return 0
        except FetchError as err:
            # Match cake.main()'s FetchError handler: plain "Error:" prefix, stderr,
            # non-zero exit, no traceback. FetchError messages already name the
            # offending external and its URL.
            print(f"Error: {err}", file=sys.stderr)
            return 1
        finally:
            # Clear memcaches so repeated in-process main() calls in tests don't
            # cross-contaminate. fetch_externals / gather_external_status own their
            # headerdeps internally, so there is no headerdeps cache to clear here.
            compiletools.wrappedos.clear_cache()
            compiletools.utils.clear_cache()
            compiletools.git_utils.clear_cache()
