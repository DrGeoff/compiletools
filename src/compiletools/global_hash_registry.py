"""Global hash registry for efficient file content hashing.

This module provides a cache that computes Git blob hashes for all files
once on first use, then serves hash lookups for cache operations.

All state lives in a BuildContext instance — there is no module-level state.

DUPLICATE HASH DETECTION:

By design, compiletools treats duplicate SHA1 hashes as bugs that need fixing.
If two files have identical content (same SHA1), an error is raised when attempting
reverse lookup (get_filepath_by_hash). This is intentional and helps catch:

* Accidental file copies that should be removed
* Zero-byte placeholder files that need proper initialization
* Configuration mistakes or build artifacts that shouldn't be committed

Duplicate content often masks real issues in the build system or test setup.

WORKAROUND:

If you intentionally need multiple files with identical or near-identical content,
add a unique comment to each file explaining its purpose. For example:

    // Placeholder stub for test scenario A
    // See docs/test-scenarios.md for details

This makes each file's purpose explicit and ensures each has a unique hash.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

from compiletools import wrappedos

if TYPE_CHECKING:
    from compiletools.build_context import BuildContext


# ---- internal helpers ----


def _compute_external_file_hash(filepath: str, hash_ops: dict[str, int]) -> str | None:
    """Compute git blob hash for a file using git's algorithm."""
    hash_ops["computed_hashes"] += 1
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        blob_data = f"blob {len(content)}\0".encode() + content
        return hashlib.sha1(blob_data).hexdigest()
    except OSError:
        return None


def _load_hashes(verbose: int = 0, *, context: BuildContext) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Load file hashes from git and build forward/reverse lookup dicts."""
    try:
        from compiletools.git_sha_report import get_complete_working_directory_hashes

        all_hashes = get_complete_working_directory_hashes(context)
        hashes = {str(path): sha for path, sha in all_hashes.items()}

        reverse: dict[str, list[str]] = {}
        for path, sha in all_hashes.items():
            filepath = str(path)
            if sha not in reverse:
                reverse[sha] = []
            reverse[sha].append(filepath)

        if verbose >= 3:
            print(f"GlobalHashRegistry: Loaded {len(hashes)} file hashes from git")

        del all_hashes

    except Exception as e:
        # M-D8: log non-fatal git failures so users running outside a
        # repo (or with a broken git invocation) can see why hashes
        # came back empty. Bare-Exception catch is preserved because
        # we genuinely want to fall back regardless of failure mode.
        if verbose >= 1:
            print(
                f"GlobalHashRegistry: git unavailable, using empty-registry fallback ({type(e).__name__}: {e})",
                file=sys.stderr,
            )
        hashes = {}
        reverse = {}

    return hashes, reverse


def _get_file_hash_impl(
    filepath: str,
    hashes: dict[str, str],
    reverse_hashes: dict[str, list[str]],
    hash_ops: dict[str, int],
) -> str:
    """Core hash-lookup logic."""
    abs_path = wrappedos.realpath(filepath)
    result = hashes.get(abs_path)

    if result is not None:
        hash_ops["registry_hits"] += 1

    # If not found and path was relative, try relative to git root
    if result is None and not os.path.isabs(filepath):
        try:
            from compiletools.git_utils import find_git_root

            git_root = find_git_root()
            abs_git_path = wrappedos.realpath(os.path.join(git_root, filepath))
            result = hashes.get(abs_git_path)
        except Exception:
            pass

    # If still not found, compute hash on-demand
    if result is None:
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"global_hash_registry encountered File not found: {filepath}")

        result = _compute_external_file_hash(abs_path, hash_ops)
        if result:
            hashes[abs_path] = result
            if result not in reverse_hashes:
                reverse_hashes[result] = [abs_path]
        else:
            raise FileNotFoundError(f"global_hash_registry encountered Failed to compute hash for file: {filepath}")

    return result


def _get_filepath_by_hash_impl(
    file_hash: str,
    reverse_hashes: dict[str, list[str]],
) -> str:
    """Core reverse-lookup logic."""
    filepaths = reverse_hashes.get(file_hash)
    if filepaths is None:
        raise FileNotFoundError(
            f"File with hash {file_hash} not found in working directory. "
            f"File may have been deleted or moved outside git working tree."
        )
    if len(filepaths) > 1:
        raise RuntimeError(
            f"Hash {file_hash} maps to {len(filepaths)} files with identical content: "
            f"{', '.join(filepaths)}. Cannot determine which file to use."
        )
    return filepaths[0]


# ---- public API (BuildContext required) ----


def load_hashes(verbose: int = 0, *, context: BuildContext) -> None:
    """Load all file hashes into the given BuildContext."""
    if context.file_hashes is not None:
        return
    context.file_hashes, context.reverse_hashes = _load_hashes(verbose, context=context)


def get_file_hash(filepath: str, context: BuildContext) -> str:
    """Get hash for a file using the given BuildContext."""
    if context.file_hashes is None:
        load_hashes(context=context)
    assert context.file_hashes is not None and context.reverse_hashes is not None
    return _get_file_hash_impl(filepath, context.file_hashes, context.reverse_hashes, context.hash_ops)


def get_tracked_files(context: BuildContext) -> dict[str, str]:
    """Get all file paths and their hashes from the registry."""
    if context.file_hashes is None:
        load_hashes(context=context)
    assert context.file_hashes is not None
    return context.file_hashes


def get_registry_stats(context: BuildContext) -> dict:
    """Get registry statistics for the given BuildContext."""
    if context.file_hashes is None:
        return {"total_files": 0, "is_loaded": False}
    return {
        "total_files": len(context.file_hashes),
        "is_loaded": True,
        **context.hash_ops,
    }


def clear_global_registry(context: BuildContext) -> None:
    """Clear the registry (mainly for testing)."""
    context.file_hashes = None
    context.reverse_hashes = None
    context.hash_ops = {"registry_hits": 0, "computed_hashes": 0}


def get_filepath_by_hash(file_hash: str, context: BuildContext) -> str:
    """Reverse lookup: get filepath from hash."""
    if context.reverse_hashes is None:
        load_hashes(context=context)
    assert context.reverse_hashes is not None
    return _get_filepath_by_hash_impl(file_hash, context.reverse_hashes)
