"""Global hash registry for efficient file content hashing.

This module provides a simple module-level cache that computes Git blob hashes 
for all files once on first use, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the 
git-sha-report functionality efficiently.
"""

import os
from typing import Dict, Optional
import threading

# Module-level cache: None = not loaded, Dict = loaded hashes
_HASHES: Optional[Dict[str, str]] = None
_lock = threading.Lock()


def load_hashes() -> None:
    """Load all file hashes once with re-entrancy guard."""
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
            print(f"GlobalHashRegistry: Failed to load git hashes: {e}")
            print("GlobalHashRegistry: Will fall back to hashlib for individual files")
            _HASHES = {}


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
    
    assert _HASHES is not None  # Should be loaded by now
        
    # Try exact path first
    if filepath in _HASHES:
        return _HASHES[filepath]
    
    # Try absolute path
    abs_path = os.path.abspath(filepath)
    if abs_path in _HASHES:
        return _HASHES[abs_path]
    
    # Try relative to current directory
    try:
        rel_path = os.path.relpath(filepath)
        if rel_path in _HASHES:
            return _HASHES[rel_path]
    except ValueError:
        pass  # Can happen with different drives on Windows
    
    return None


# Public API functions for compatibility
def initialize_global_hash_registry(use_git_hashes: bool = True) -> None:
    """Initialize the global hash registry.
    
    This is now optional - hashes will be loaded lazily on first use.
    Provided for explicit initialization at program startup if desired.
    
    Args:
        use_git_hashes: Currently ignored - always uses git hashes
    """
    if use_git_hashes:
        load_hashes()


def get_file_hash_from_registry(filepath: str) -> Optional[str]:
    """Get file hash from global registry.
    
    Args:
        filepath: Path to file
        
    Returns:
        Git blob hash if available, None if not in registry
    """
    return get_file_hash(filepath)


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