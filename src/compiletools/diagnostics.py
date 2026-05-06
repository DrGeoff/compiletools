"""Per-invocation diagnostic-output coordination.

Both ct-cake (timing JSON) and the slurm backend (job logs) consume these
helpers so that all diagnostic artifacts produced by a single ct-cake
invocation land in one shared, easily-located subdirectory.
"""

from __future__ import annotations

import os
import time

_invocation_id: str | None = None


def invocation_id() -> str:
    """Return this process's diagnostic invocation id.

    Format: ``YYYYMMDDTHHMMSS-PID`` (e.g. ``20260506T143022-12345``).
    Sortable lexicographically across reboots; pid disambiguates
    simultaneous launches in the same wall-clock second.

    Cached at module level -- repeated calls within one process return
    the same value, so all diagnostic files for one ct-cake invocation
    share a single id.
    """
    global _invocation_id
    if _invocation_id is None:
        _invocation_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    return _invocation_id


def resolve_diagnostics_dir(args) -> str:
    """Return the per-invocation diagnostics directory, creating it if missing.

    Resolution order for the parent directory:
      1. ``args.diagnostics_dir`` if truthy.
      2. ``<args.bindir>/diagnostics`` otherwise.

    A subdirectory named with ``invocation_id()`` is then appended to the
    parent, and the full path is created with ``os.makedirs(exist_ok=True)``.
    Returns the absolute-or-as-given path of the per-invocation subdir.

    Raises RuntimeError if neither ``args.diagnostics_dir`` nor
    ``args.bindir`` is set (or both are empty/falsy).
    """
    explicit = getattr(args, "diagnostics_dir", None)
    if explicit:
        parent = explicit
    else:
        bindir = getattr(args, "bindir", None)
        if not bindir:
            raise RuntimeError("resolve_diagnostics_dir requires either args.diagnostics_dir or args.bindir to be set")
        parent = os.path.join(bindir, "diagnostics")

    path = os.path.join(parent, invocation_id())
    os.makedirs(path, exist_ok=True)
    return path


def _reset_for_tests() -> None:
    """Clear the cached invocation id. Test-only."""
    global _invocation_id
    _invocation_id = None
