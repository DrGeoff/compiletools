"""Global hash registry for efficient file content hashing.

This module provides a simple module-level cache that computes Git blob hashes 
for all files once on first use, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the 
git-sha-report functionality efficiently.
"""

from typing import Dict, Optional
import threading
import os
import subprocess
from functools import lru_cache
from compiletools import wrappedos

# Module-level cache: None = not loaded, Dict = loaded hashes
_HASHES: Optional[Dict[str, str]] = None
_REVERSE_HASHES: Optional[Dict[str, str]] = None  # hash -> filepath cache
_lock = threading.Lock()

# Hash operation counters
_hash_ops = {'registry_hits': 0, 'computed_hashes': 0}


def _compute_external_file_hash(filepath: str) -> str:
    """Compute git blob hash using git hash-object.

    Uses git subprocess to compute hash without reading file in Python.
    This avoids duplicate file reads when git registry isn't available.

    Args:
        filepath: Path to file

    Returns:
        Git blob hash (40-char hex string)

    Raises:
        FileNotFoundError: If filepath does not exist
        RuntimeError: If git hash-object fails
    """
    global _hash_ops
    _hash_ops['computed_hashes'] += 1

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    try:
        result = subprocess.run(
            ['git', 'hash-object', filepath],
            capture_output=True,
            text=True,
            check=True,
            timeout=5
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else "unknown error"
        raise RuntimeError(f"git hash-object failed for {filepath}: {stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git hash-object timed out for {filepath}")
    except FileNotFoundError:
        raise RuntimeError("git executable not found in PATH")


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

            # Convert Path keys to normalized string keys for consistent lookup across Python versions
            _HASHES = {wrappedos.realpath(str(path)): sha for path, sha in all_hashes.items()}

            # Build reverse lookup cache: hash -> filepath (also normalized)
            _REVERSE_HASHES = {sha: wrappedos.realpath(str(path)) for path, sha in all_hashes.items()}

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
    # Normalize path before caching to ensure cache hits for equivalent paths
    # E.g., "test.c", "./test.c", and "/abs/path/test.c" should all cache as same key
    abs_path = wrappedos.realpath(filepath)
    return _get_file_hash_impl(abs_path)


@lru_cache(maxsize=None)
def _get_file_hash_impl(abs_path: str) -> str:
    """Internal implementation that operates on normalized absolute paths.

    Args:
        abs_path: Absolute normalized path (from wrappedos.realpath)

    Returns:
        Git blob hash
    """
    # Ensure hashes are loaded
    if _HASHES is None:
        load_hashes()

    # Type narrowing: load_hashes() guarantees both are dicts
    assert _HASHES is not None
    assert _REVERSE_HASHES is not None

    # Lookup in registry using normalized path
    result = _HASHES.get(abs_path)

    if result is not None:
        global _hash_ops
        _hash_ops['registry_hits'] += 1
        return result

    # If not found in registry, compute hash on-demand using git hash-object
    # This raises FileNotFoundError or RuntimeError if it fails
    result = _compute_external_file_hash(abs_path)

    # Cache the computed hash for future lookups (both forward and reverse)
    _HASHES[abs_path] = result
    _REVERSE_HASHES[result] = abs_path

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
    """Clear the global registry (mainly for testing)."""
    global _HASHES, _REVERSE_HASHES
    with _lock:
        _HASHES = None
        _REVERSE_HASHES = None
        # Clear path cache to prevent CWD-dependent stale results in test environments
        wrappedos.realpath.cache_clear()


def get_filepath_by_hash(file_hash: str) -> Optional[str]:
    """Reverse lookup: get filepath from hash.

    Args:
        file_hash: Git blob hash

    Returns:
        Absolute realpath if found, None otherwise (already normalized in registry)
    """
    if _REVERSE_HASHES is None:
        load_hashes()

    # Paths in registry are already realpath from git_sha_report
    return _REVERSE_HASHES.get(file_hash)