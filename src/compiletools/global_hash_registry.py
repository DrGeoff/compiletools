"""Global hash registry for efficient file content hashing.

This module provides a singleton registry that computes Git blob hashes for all
files once at program startup, then serves hash lookups for cache operations.
This eliminates the need for individual hashlib calls and leverages the 
git-sha-report functionality efficiently.
"""

import os
from pathlib import Path
from typing import Dict, Optional
import threading


class GlobalHashRegistry:
    """Singleton registry for file content hashes."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._hash_map: Dict[str, str] = {}
            self._is_loaded = False
            self._initialized = True
    
    def initialize(self, use_git_hashes: bool = True) -> None:
        """Initialize the hash registry with file hashes.
        
        Args:
            use_git_hashes: If True, use git-sha-report for comprehensive hashing.
                          If False, leave registry empty (fallback to hashlib).
        """
        if self._is_loaded:
            return  # Already initialized
        
        if use_git_hashes:
            try:
                from compiletools.git_sha_report import get_complete_working_directory_hashes
                
                # Single call to get all file hashes
                all_hashes = get_complete_working_directory_hashes()
                
                # Convert Path keys to string keys for easier lookup
                self._hash_map = {str(path): sha for path, sha in all_hashes.items()}
                
                print(f"GlobalHashRegistry: Loaded {len(self._hash_map)} file hashes from git")
                
            except Exception as e:
                print(f"GlobalHashRegistry: Failed to load git hashes: {e}")
                print("GlobalHashRegistry: Will fall back to hashlib for individual files")
                self._hash_map = {}
        
        self._is_loaded = True
    
    def get_file_hash(self, filepath: str) -> Optional[str]:
        """Get hash for a file from the registry.
        
        Args:
            filepath: Path to file (absolute or relative)
            
        Returns:
            Git blob hash if available, None if not in registry
        """
        if not self._is_loaded:
            return None
        
        # Try exact path first
        if filepath in self._hash_map:
            return self._hash_map[filepath]
        
        # Try absolute path
        abs_path = os.path.abspath(filepath)
        if abs_path in self._hash_map:
            return self._hash_map[abs_path]
        
        # Try relative to current directory
        try:
            rel_path = os.path.relpath(filepath)
            if rel_path in self._hash_map:
                return self._hash_map[rel_path]
        except ValueError:
            pass  # Can happen with different drives on Windows
        
        return None
    
    def get_stats(self) -> Dict[str, int]:
        """Get registry statistics."""
        return {
            'total_files': len(self._hash_map),
            'is_loaded': self._is_loaded
        }
    
    def clear(self) -> None:
        """Clear the registry (mainly for testing)."""
        self._hash_map.clear()
        self._is_loaded = False


# Singleton instance
_registry = GlobalHashRegistry()


def initialize_global_hash_registry(use_git_hashes: bool = True) -> None:
    """Initialize the global hash registry.
    
    This should be called once at program startup.
    
    Args:
        use_git_hashes: If True, use git-sha-report for comprehensive hashing.
                      If False, leave registry empty (fallback to hashlib).
    """
    _registry.initialize(use_git_hashes)


def get_file_hash_from_registry(filepath: str) -> Optional[str]:
    """Get file hash from global registry.
    
    Args:
        filepath: Path to file
        
    Returns:
        Git blob hash if available, None if not in registry
    """
    return _registry.get_file_hash(filepath)


def get_registry_stats() -> Dict[str, int]:
    """Get global registry statistics."""
    return _registry.get_stats()


def clear_global_registry() -> None:
    """Clear the global registry (mainly for testing)."""
    _registry.clear()