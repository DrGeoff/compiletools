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

import copy
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ExternalStatus",
    "FetchError",
    "GitExternal",
    "ResolvedExternal",
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

    Raises:
        FetchError: If a CLI entry lacks ``=``, or has an empty name or empty
                    path.
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

    Returns ``compiletools.headerdeps.create`` over a deep copy of *args* with
    extra ``-I`` flags appended to ``CPPFLAGS`` so the dependency walker can
    traverse INTO already-fetched externals (to discover their transitive
    ``#include`` graph and the further ``//#GIT=`` declarations it reaches).

    The caller's *args* (and its frozen ``args.flags``) are never mutated — the
    augmentation lives only on the deep copy, strictly local to the scan.
    DirectHeaderDeps derives its project include paths from ``CPPFLAGS`` (see
    ``headerdeps._initialize_includes_and_macros``), so appending ``-I`` tokens
    there is the supported way to widen the search path.
    """
    import compiletools.headerdeps

    scan_args = copy.deepcopy(args)
    include_dirs = [externals_dir]
    for root in resolved_roots:
        include_dirs.append(root)
        include_dirs.append(os.path.join(root, "include"))

    extra = " ".join(f"-I{shlex.quote(d)}" for d in include_dirs)
    existing = getattr(scan_args, "CPPFLAGS", "") or ""
    scan_args.CPPFLAGS = (existing + " " + extra).strip() if existing else extra

    return compiletools.headerdeps.create(scan_args, context=context)


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

    # Each round builds an _augmented_headerdeps over a deepcopy of args, and
    # HeaderDepsBase.__init__ stashes that throwaway deepcopy into
    # context.analyzer_args. Capture the caller's prior value (possibly None)
    # and restore it on exit (even on FetchError) so a caller reading
    # context.analyzer_args after fetch_externals returns sees its original
    # args, not the last round's throwaway deepcopy.
    prior_analyzer_args = context.analyzer_args
    try:
        for _round in range(_MAX_FIXPOINT_ROUNDS):
            headerdeps = _augmented_headerdeps(
                args,
                context,
                externals_dir=externals_dir,
                resolved_roots=[r.path for r in resolved.values()],
            )
            reachable = _reachable_sources(target_files, headerdeps, args)

            new_names: list[GitExternal] = []
            for source_file in reachable:
                for ext in extract_git_externals(source_file, args, context):
                    prior = declared.get(ext.name)
                    if prior is None:
                        declared[ext.name] = ext
                        declared_files[ext.name] = source_file
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
                    # Same name + same URL but a differing ref: first declaration wins.
                    if prior.ref != ext.ref:
                        _warn(
                            f"external '{ext.name}' ({ext.url}): conflicting refs "
                            f"'{prior.ref}' (in {declared_files.get(ext.name, '?')}) vs "
                            f"'{ext.ref}' (in {source_file}); keeping '{prior.ref}'."
                        )
                    # Otherwise an exact duplicate — silently deduped.

            if not new_names:
                break

            for ext in new_names:
                resolved[ext.name] = resolve_external(
                    ext,
                    externals_dir=externals_dir,
                    # Override keys are normalized to lowercase in
                    # parse_git_path_overrides; ext.name (from derive_name)
                    # preserves the URL-basename case, so lowercase it here to
                    # match case-insensitively.
                    override_path=overrides.get(ext.name.lower()),
                    no_fetch=no_fetch,
                    update=update,
                    verbose=verbose,
                )

            # Fetching just changed the filesystem under externals_dir. wrappedos'
            # stat-like queries are globally @functools.cache'd by path, so a
            # "file missing" answer cached while an external header did not yet
            # exist would otherwise stick — making the NEXT round's _find_include
            # blind to the freshly-cloned sources and breaking transitive
            # discovery. Drop those caches so the next round re-stats from disk.
            import compiletools.wrappedos

            compiletools.wrappedos.clear_cache()
        else:
            raise FetchError(
                f"//#GIT= resolution did not converge after {_MAX_FIXPOINT_ROUNDS} rounds; "
                f"resolved so far: {sorted(resolved)}"
            )

        return list(resolved.values())
    finally:
        context.analyzer_args = prior_analyzer_args


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
    ordered_names: list[str] = []

    prior_analyzer_args = context.analyzer_args
    try:
        for _round in range(_MAX_FIXPOINT_ROUNDS):
            # Reach into whatever externals are already present on disk (never
            # fetch to widen the graph). A present external's own sources can
            # declare further externals we should report.
            present_roots = [
                s.path
                for s in (
                    _status_for(ext, externals_dir=externals_dir, override_path=overrides.get(ext.name.lower()))
                    for ext in declared.values()
                )
                if s.state != "missing"
            ]
            headerdeps = _augmented_headerdeps(
                args,
                context,
                externals_dir=externals_dir,
                resolved_roots=present_roots,
            )
            reachable = _reachable_sources(target_files, headerdeps, args)

            new_found = False
            for source_file in reachable:
                for ext in extract_git_externals(source_file, args, context):
                    if ext.name in declared:
                        continue
                    declared[ext.name] = ext
                    ordered_names.append(ext.name)
                    new_found = True

            if not new_found:
                break

            # A freshly-declared external might already be present on disk; drop
            # wrappedos' cached "missing" answers so the next round's header scan
            # can reach into it (mirrors fetch_externals' cache discipline).
            import compiletools.wrappedos

            compiletools.wrappedos.clear_cache()
        else:
            raise FetchError(
                f"//#GIT= status enumeration did not converge after {_MAX_FIXPOINT_ROUNDS} rounds; "
                f"found so far: {sorted(declared)}"
            )

        return [
            _status_for(declared[name], externals_dir=externals_dir, override_path=overrides.get(name.lower()))
            for name in ordered_names
        ]
    finally:
        context.analyzer_args = prior_analyzer_args


# ===========================================================================
# CLI entry point
# ===========================================================================


def _collect_target_files(args) -> list[str]:
    """Flatten existing on-disk source files from the target arg groups.

    Mirrors ``Cake._fetch_and_register_externals``: de-duplicates across
    ``filename`` / ``static`` / ``dynamic`` / ``tests`` and drops any entry
    that is falsy or not an on-disk file.
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
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="status",
        dest="status",
        default=False,
        help="Report the on-disk state of each //#GIT external (present/missing/dirty); never clone or update.",
    )

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)

    headerdeps = None
    try:
        target_files = _collect_target_files(args)
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
        # cross-contaminate (mirrors filelist.main).
        compiletools.wrappedos.clear_cache()
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        if headerdeps is not None:
            headerdeps.clear_cache()
