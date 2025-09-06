"""Global hash registry for efficient file content hashing.

This module provides a simple module-level cache that computes Git blob hashes 
for all files once on first use, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the 
git-sha-report functionality efficiently.
"""

from typing import Dict, Optional
import threading
import os
import hashlib
from functools import lru_cache
from compiletools import wrappedos

# Module-level cache: None = not loaded, Dict = loaded hashes
_HASHES: Optional[Dict[str, str]] = None
_lock = threading.Lock()


def _compute_external_file_hash(filepath: str) -> Optional[str]:
    """Compute git blob hash for a file using git's algorithm."""
    try:
        with open(filepath, 'rb') as f:
            content = f.read()
        
        # Git blob hash: sha1("blob {size}\0{content}")
        blob_data = f"blob {len(content)}\0".encode() + content
        return hashlib.sha1(blob_data).hexdigest()
    except (OSError, IOError):
        return None


def load_hashes() -> None:
    """Load all file hashes once with thread safety."""
    global _HASHES
    
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
            
            print(f"GlobalHashRegistry: Loaded {len(_HASHES)} file hashes from git")
            
        except Exception as e:
            # Gracefully handle git failures (e.g., in test environments, non-git directories)
            print(f"GlobalHashRegistry: Git not available, using fallback mode: {e}")
            _HASHES = {}  # Empty hash registry - will compute hashes on demand


@lru_cache(maxsize=None)
def get_file_hash(filepath: str) -> Optional[str]:
    """Get hash for a file, loading hashes on first call.
    
    For files tracked in git, uses cached hashes from git registry.
    For external files (system libraries, etc.), computes git blob hash on-demand.
    
    Args:
        filepath: Path to file (absolute or relative)
        
    Returns:
        Git blob hash if file exists, None if file doesn't exist
    """
    # Ensure hashes are loaded
    if _HASHES is None:
        load_hashes()
    
    # Convert to absolute path for consistent lookup
    # If path is relative, first try relative to current directory
    abs_path = wrappedos.realpath(filepath)
    result = _HASHES.get(abs_path)
    
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
    if result is None and os.path.exists(abs_path):
        result = _compute_external_file_hash(abs_path)
        if result:
            # Cache the computed hash for future lookups
            _HASHES[abs_path] = result
    
    return result


# Public API functions for compatibility



def get_registry_stats() -> Dict[str, int]:
    """Get global registry statistics."""
    if _HASHES is None:
        return {'total_files': 0, 'is_loaded': False}
    return {'total_files': len(_HASHES), 'is_loaded': True}


def clear_global_registry() -> None:
    """Clear the global registry (mainly for testing)."""
    global _HASHES
    with _lock:
        _HASHES = None