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

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "FetchError",
    "GitExternal",
    "ResolvedExternal",
    "derive_name",
    "parse_git_declaration",
    "parse_git_value",
    "resolve_external",
]


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
                    URL), or if a trailing ``@`` is present with an empty
                    ref.
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
        return value, None

    url = value[:at_idx]
    ref = value[at_idx + 1 :]
    if not ref:
        raise ValueError(
            f"GIT flag value '{value}' has a trailing '@' with an empty ref; "
            "specify a branch, tag, or commit SHA after '@'"
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
                    with ``/``).

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
    """Return an environment that neutralises ambient git configuration.

    A user's ``~/.gitconfig`` or a machine's ``/etc/gitconfig`` can carry
    settings (``commit.gpgsign``, ``transfer.fsckObjects``, custom
    ``url.*.insteadOf`` rewrites, …) that change clone/fetch/checkout
    behaviour and would make external resolution non-deterministic across
    machines. We disable both the system and global config layers so a
    resolve depends only on the remote and the per-repo config a clone
    creates. ``GIT_CONFIG_GLOBAL`` requires git >= 2.32.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
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
    """Return True if *path* is the top level of a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_git_env(),
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return result.stdout.strip() == "true"


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

    Raises :class:`FetchError` (naming the external) if ``git status`` cannot
    be run — otherwise a raw traceback would escape without identifying which
    external failed.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
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


def _is_branch(path: str, ref: str) -> bool:
    """Return True if *ref* names a local or remote-tracking branch."""
    for candidate in (f"refs/heads/{ref}", f"refs/remotes/origin/{ref}"):
        if _rev_parse_verify(path, candidate) is not None:
            return True
    return False


def _resolve_override(ext: GitExternal, override_path: str) -> ResolvedExternal:
    """Handle the ``override_path`` case: use verbatim, never mutate."""
    if not os.path.exists(override_path):
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
    _run_git(["clone", ext.url, target], cwd=None, ext=ext)
    if ext.ref is not None:
        # The ref may live on the remote but not be checked out by a plain
        # clone (e.g. a non-default branch or a bare SHA on another branch).
        if _rev_parse_verify(target, ext.ref) is None:
            _run_git(["fetch", "origin", ext.ref], cwd=target, ext=ext)
        _run_git(["checkout", ext.ref], cwd=target, ext=ext)
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
        _run_git(["fetch", "origin", ref], cwd=target, ext=ext)

    _run_git(["checkout", ref], cwd=target, ext=ext)


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
    _run_git(["fetch", "origin", ref], cwd=target, ext=ext)
    _run_git(["checkout", ref], cwd=target, ext=ext)
    _run_git(["merge", "--ff-only", f"origin/{ref}"], cwd=target, ext=ext)


def _handle_no_ref(ext: GitExternal, target: str, *, update: bool, verbose: int) -> None:
    """Handle ``ref is None`` on a present work tree: pull current branch on --update."""
    if not update:
        return
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
    elif _is_branch(target, ext.ref):
        _handle_branch(ext, target, update=update, verbose=verbose)
    else:
        # SHA or tag — both immutable. (An unknown ref that is neither a
        # branch, tag, nor resolvable SHA falls here and surfaces as a
        # named checkout failure.)
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
    if not os.path.exists(target):
        return _clone_missing(ext, target, no_fetch=no_fetch, verbose=verbose)
    return _handle_present(ext, target, no_fetch=no_fetch, update=update, verbose=verbose)
