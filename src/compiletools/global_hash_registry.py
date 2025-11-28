"""Global hash registry for efficient file content hashing.

This module provides a simple module-level cache that computes Git blob hashes
for all files once on first use, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the
git-sha-report functionality efficiently.

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

from typing import Dict, Optional, List
import threading
import os
import hashlib
from functools import lru_cache
from compiletools import wrappedos

# Module-level cache: None = not loaded, Dict = loaded hashes
_HASHES: Optional[Dict[str, str]] = None
_REVERSE_HASHES: Optional[Dict[str, List[str]]] = None  # hash -> list of filepaths cache
_lock = threading.Lock()

# Hash operation counters
_hash_ops = {'registry_hits': 0, 'computed_hashes': 0}


def _compute_external_file_hash(filepath: str) -> Optional[str]:
    """Compute git blob hash for a file using git's algorithm."""
    global _hash_ops
    _hash_ops['computed_hashes'] += 1
    try:
        with open(filepath, 'rb') as f:
            content = f.read()

        # Git blob hash: sha1("blob {size}\0{content}")
        blob_data = f"blob {len(content)}\0".encode() + content
        return hashlib.sha1(blob_data).hexdigest()
    except (OSError, IOError):
        return None


def load_hashes(verbose: int = 0) -> None:
    """Load all file hashes once with thread safety.

    Args:
        verbose: Verbosity level (0 = silent, higher = more output)
    """
    import gc
    global _HASHES, _REVERSE_HASHES

    if _HASHES is not None:
        return  # Already loaded

    with _lock:
        if _HASHES is not None:
            return  # Double-check after acquiring lock

        try:
            from compiletools.git_sha_report import get_complete_working_directory_hashes

            # Single call to get all file hashes
            all_hashes = get_complete_working_directory_hashes()

            # Convert Path keys to string keys for easier lookup
            _HASHES = {str(path): sha for path, sha in all_hashes.items()}

            # Build reverse lookup cache: hash -> list of filepaths
            _REVERSE_HASHES = {}
            for path, sha in all_hashes.items():
                filepath = str(path)
                if sha not in _REVERSE_HASHES:
                    _REVERSE_HASHES[sha] = []
                _REVERSE_HASHES[sha].append(filepath)

            if verbose >= 3:
                print(f"GlobalHashRegistry: Loaded {len(_HASHES)} file hashes from git")

            # Explicitly clean up Path objects and force garbage collection
            # to ensure file descriptors are released
            del all_hashes
            gc.collect()

        except Exception as e:
            # Gracefully handle git failures (e.g., in test environments, non-git directories)
            if verbose >= 3:
                print(f"GlobalHashRegistry: Git not available, using fallback mode: {e}")
            _HASHES = {}  # Empty hash registry - will compute hashes on demand
            _REVERSE_HASHES = {}


@lru_cache(maxsize=None)
def get_file_hash(filepath: str) -> str:
    """Get hash for a file, loading hashes on first call.

    For files tracked in git, uses cached hashes from git registry.
    For external files (system libraries, etc.), computes git blob hash on-demand.

    Args:
        filepath: Path to file (absolute or relative)

    Returns:
        Git blob hash

    Raises:
        FileNotFoundError: If filepath does not exist
    """
    # Ensure hashes are loaded
    if _HASHES is None:
        load_hashes()

    # Convert to absolute path for consistent lookup
    # If path is relative, first try relative to current directory
    abs_path = wrappedos.realpath(filepath)
    result = _HASHES.get(abs_path)

    if result is not None:
        global _hash_ops
        _hash_ops['registry_hits'] += 1

    # If not found and path was relative, try relative to git root
    if result is None and not os.path.isabs(filepath):
        try:
            from compiletools.git_utils import find_git_root
            git_root = find_git_root()
            git_relative_path = os.path.join(git_root, filepath)
            abs_git_path = wrappedos.realpath(git_relative_path)
            result = _HASHES.get(abs_git_path)
        except Exception:
            pass  # Git root not available, stick with original result

    # If still not found, check if file exists and compute hash on-demand
    if result is None:
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"global_hash_registry encountered File not found: {filepath}")

        result = _compute_external_file_hash(abs_path)
        if result:
            # Cache the computed hash for future lookups (both forward and reverse)
            _HASHES[abs_path] = result
            # _REVERSE_HASHES is guaranteed to be initialized by load_hashes()
            assert _REVERSE_HASHES is not None
            if result not in _REVERSE_HASHES:
                _REVERSE_HASHES[result] = []
            _REVERSE_HASHES[result].append(abs_path)
        else:
            raise FileNotFoundError(f"global_hash_registry encountered Failed to compute hash for file: {filepath}")

    return result


# Public API functions for compatibility



def get_registry_stats() -> Dict[str, int]:
    """Get global registry statistics."""
    if _HASHES is None:
        return {'total_files': 0, 'is_loaded': False}
    return {
        'total_files': len(_HASHES),
        'is_loaded': True,
        'registry_hits': _hash_ops['registry_hits'],
        'computed_hashes': _hash_ops['computed_hashes']
    }


def clear_global_registry() -> None:
    """Clear the global registry (mainly for testing).

    Also clears the LRU cache on get_file_hash() to ensure consistency.
    When the registry is cleared, cached hash values would be stale because
    they wouldn't be in _REVERSE_HASHES for reverse lookups.
    """
    global _HASHES, _REVERSE_HASHES
    with _lock:
        _HASHES = None
        _REVERSE_HASHES = None
    # Clear the LRU cache on get_file_hash() to prevent stale hash lookups
    # Without this, get_file_hash() returns cached hashes that aren't in _REVERSE_HASHES
    get_file_hash.cache_clear()


def get_filepath_by_hash(file_hash: str) -> str:
    """Reverse lookup: get filepath from hash.

    Args:
        file_hash: Git blob hash

    Returns:
        Absolute realpath (already normalized in registry)

    Raises:
        FileNotFoundError: If hash not found in registry (file deleted/moved outside git)
        RuntimeError: If hash maps to multiple files (duplicate content detected)
    """
    if _REVERSE_HASHES is None:
        load_hashes()

    # load_hashes() always sets _REVERSE_HASHES to a dict (empty or populated)
    assert _REVERSE_HASHES is not None
    filepaths = _REVERSE_HASHES.get(file_hash)
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