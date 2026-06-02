import functools
import os
import re
import subprocess

import compiletools.utils
import compiletools.wrappedos

# NOTE: ``compiletools.apptools`` is imported *deferred* (in-function), not at
# module level, to break an import cycle. apptools now
# re-exports the CLI argument-registration layer from
# ``compiletools.apptools_argparse``, and that module imports
# ``compiletools.configutils`` -> ``compiletools.git_utils`` at load time. A
# top-level ``import compiletools.apptools`` here would, when apptools_argparse
# is imported first, re-enter apptools while it is still mid-way through its
# ``from compiletools.apptools_argparse import ...`` re-export block (the names
# aren't bound yet), raising ImportError. git_utils only ever touches apptools
# at call time (``_parser_has_option`` / ``create_parser`` below), so the
# deferred import is safe and keeps git_utils pointed at the apptools *facade*
# (NOT apptools_argparse), preserving the documented patch-target contract.

# Match a bare detached-HEAD SHA on the first line of `.git/HEAD`: 40 hex
# digits for sha1 repos, 64 for sha256 (`git init --object-format=sha256`).
_HEAD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# Cap how many bytes we read when content-validating `.git` markers. A real
# gitlink first line is ~80 chars; HEAD's first line is ~50 chars. 256 bytes
# is more than enough and keeps the cost negligible even when the walker
# calls us hundreds of times per process.
_GIT_MARKER_READ_CAP = 256

# When True, the fallback walker accepts a bare ``.git`` (file or empty dir)
# the way it always has — restoring legacy/dummy-marker behaviour for users
# who deliberately drop placeholder ``.git`` files to mark "the project root
# is here". When False (default), the walker requires a *real* repo marker:
# either ``.git`` as a regular file (worktree gitlink form) or ``.git`` as a
# directory containing a ``HEAD`` file. This guards against a stray empty
# ``/tmp/.git`` left by an unrelated user poisoning every test or build that
# happens to run under ``/tmp/...``.
_ALLOW_FAKE_GIT = False


def set_allow_fake_git(value: bool) -> None:
    """Enable/disable acceptance of bare/empty '.git' markers in the fallback walker.

    Clears the ``@functools.cache`` on ``_find_git_root`` whenever the value
    actually changes so previously cached strict/permissive answers don't
    leak across the toggle.
    """
    global _ALLOW_FAKE_GIT
    new_value = bool(value)
    if new_value != _ALLOW_FAKE_GIT:
        # Assign FIRST so a concurrent reader racing through the cache after
        # our clear sees the new value, not the stale one. The reverse order
        # leaves a window where a reader can repopulate the cache with the
        # pre-toggle answer.
        _ALLOW_FAKE_GIT = new_value
        clear_cache()


def get_allow_fake_git() -> bool:
    """Return the current value of the allow-fake-git toggle."""
    return _ALLOW_FAKE_GIT


def _read_first_line(path):
    """Read up to ``_GIT_MARKER_READ_CAP`` bytes from ``path`` and return the first line.

    Returns ``None`` on any read error (missing, permission denied, decode
    error, …) — callers treat ``None`` as "not a real marker".
    """
    try:
        with open(path, "rb") as fh:
            blob = fh.read(_GIT_MARKER_READ_CAP)
    except (OSError, ValueError):
        return None
    # First line only — split on the first newline, strip trailing \r if any.
    first = blob.split(b"\n", 1)[0]
    try:
        return first.decode("utf-8", errors="strict").rstrip("\r")
    except UnicodeDecodeError:
        return None


def is_real_git_marker(path):
    """Return True if ``<path>/.git`` looks like a genuine git marker.

    - ``.git`` as a regular file: worktree gitlink form. Accepted only when
      the first line begins with ``gitdir: `` (per ``git worktree add``).
      Empty or arbitrary-content regular files are rejected — guards against
      cross-user poisoning by a stray ``touch /tmp/.git``.
    - ``.git`` as a directory containing ``HEAD``: real repository. Accepted
      only when ``HEAD``'s first line starts with ``ref: `` or matches a
      40-hex (sha1) or 64-hex (sha256) SHA (detached HEAD form).
      Empty/garbage ``HEAD`` rejected.
    - ``.git`` absent or otherwise odd (symlink to nonexistent, etc.): not
      a marker.

    Reads are capped at ``_GIT_MARKER_READ_CAP`` bytes (first line only) and
    tolerant of read errors. The walker may call this hundreds of times per
    process so it must stay cheap; the per-directory result is already
    covered by ``_find_git_root``'s ``@functools.cache``.
    """
    git_path = os.path.join(path, ".git")
    if not os.path.exists(git_path):
        return False
    # ``.git`` as a regular file is the worktree gitlink form. Validate that
    # the first line begins with ``gitdir: `` to reject empty / adversarial
    # placeholders.
    if os.path.isfile(git_path):
        first = _read_first_line(git_path)
        return first is not None and first.startswith("gitdir: ")
    # Directory: must contain HEAD AND HEAD must look like a real ref or
    # detached-HEAD SHA. Empty / garbage HEAD is a fake/dummy.
    if os.path.isdir(git_path):
        head_path = os.path.join(git_path, "HEAD")
        if not os.path.isfile(head_path):
            return False
        first = _read_first_line(head_path)
        if first is None:
            return False
        if first.startswith("ref: "):
            return True
        return bool(_HEAD_SHA_RE.match(first))
    # Symlink-to-nonexistent or other oddity — not a real marker.
    return False


def find_git_root(filename=None):
    """Return the absolute path of .git for the given filename"""
    # Note: You can't functools.lru_cache(maxsize=None) this one since the None parameter will
    # return different results as the cwd changes
    if filename:
        directory = os.path.dirname(compiletools.wrappedos.realpath(filename))
    else:
        directory = os.getcwd()
    return _find_git_root(directory)


@functools.cache
def _find_git_root(directory):
    """Internal function to find the git root but cache it against the given directory"""
    # Define the git root of a project that isn't under version control to be the directory
    gitroot = directory
    git_succeeded = False
    try:
        # Use cwd parameter instead of os.chdir() to avoid concurrent access issues
        toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            cwd=directory,  # Run git command from the specified directory
        ).strip("\n")
        # An empty toplevel (rare git edge cases, e.g. some bare-repo / GIT_DIR
        # configurations) would silently propagate as "" and break callers that
        # use the return value as a path (subprocess cwd, os.path.join, …).
        # Treat it the same as a CalledProcessError so the fallback walker runs.
        if toplevel:
            gitroot = toplevel
            git_succeeded = True
    except (subprocess.CalledProcessError, OSError):
        pass

    if not git_succeeded:
        # A CalledProcessError exception means we aren't in a real git repository.
        # An OSError probably means git isn't installed on this machine.
        # But are we in a fake git repository? (i.e., there exists a dummy .git
        # file). By default we require a *real* git marker — a regular-file
        # ``.git`` (worktree gitlink) or a directory containing ``HEAD`` — so
        # a stray empty ``/tmp/.git`` left by another user doesn't silently
        # become the gitroot for every build running under ``/tmp/...``. Users
        # who deliberately drop bare ``.git`` placeholders can opt back into
        # the legacy permissive behaviour with ``--allow-fake-git`` /
        # ``set_allow_fake_git(True)``.
        trialgitroot = directory

        while trialgitroot != "/":
            git_path = os.path.join(trialgitroot, ".git")
            if os.path.exists(git_path):
                if _ALLOW_FAKE_GIT or is_real_git_marker(trialgitroot):
                    gitroot = trialgitroot
                    break
            trialgitroot = os.path.dirname(trialgitroot)

    return gitroot


@functools.cache
def strip_git_root(filename):
    size = len(find_git_root(filename)) + 1
    return filename[size:]


def clear_cache():
    _find_git_root.cache_clear()
    strip_git_root.cache_clear()


class Project:
    def __init__(self, args):
        self._args = args

    def pathname(self, filename):
        """Return the project part of the given filename"""
        if self._args.git_root:
            return strip_git_root(filename)
        else:
            return compiletools.utils.remove_mount(filename)


class NameAdjuster:
    """Conditionally remove the git root from a given filename"""

    def __init__(self, args):
        self._args = args

    @staticmethod
    def add_arguments(cap):
        # Aliased import (not bare ``import compiletools.apptools``) so the
        # local ``compiletools`` name isn't shadowed -- the module-level
        # ``compiletools.utils`` reference below must keep resolving.
        import compiletools.apptools as _apptools  # deferred: see module-top note

        if _apptools._parser_has_option(cap, "--shorten"):
            return
        compiletools.utils.add_flag_argument(
            cap,
            "shorten",
            "strip_git_root",
            default=False,
            help="Strip the git root from the filenames",
        )

    def adjust(self, name):
        if self._args.strip_git_root:
            return strip_git_root(name)
        else:
            return name


def main(argv=None):
    import compiletools.apptools as _apptools  # deferred: see module-top note

    cap = _apptools.create_parser("Find git repository root", argv=argv, include_config=False)
    cap.parse_args(args=argv)
    print(find_git_root())
    return 0
