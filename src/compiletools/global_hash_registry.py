"""Global hash registry for efficient file content hashing.

This module provides a simple module-level cache that computes Git blob hashes 
for all files once on first use, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the 
git-sha-report functionality efficiently.
"""

from typing import Dict, Optional
import threading
import os
from compiletools import wrappedos

# Module-level cache: None = not loaded, Dict = loaded hashes
_HASHES: Optional[Dict[str, str]] = None
_lock = threading.Lock()


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
            raise RuntimeError(f"GlobalHashRegistry: Failed to load git hashes: {e}") from e


def get_file_hash(filepath: str) -> Optional[str]:
    """Get hash for a file, loading hashes on first call.
    
    Args:
        filepath: Path to file (absolute or relative)
        
    Returns:
        Git blob hash if available, None if not in registry
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