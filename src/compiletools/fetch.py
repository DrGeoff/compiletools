"""Parsing layer for //#GIT=<url>[@<ref>] magic-flag declarations.

This module is intentionally side-effect-free: no git operations, no
filesystem access, no network calls.  It provides three parsing
primitives and the ``GitExternal`` dataclass they produce.

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

from dataclasses import dataclass

__all__ = [
    "GitExternal",
    "derive_name",
    "parse_git_declaration",
    "parse_git_value",
]


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
