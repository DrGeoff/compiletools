"""Cache implementations for FileAnalyzer results.

This module provides multiple cache backends for FileAnalysisResult objects,
allowing efficient reuse of analysis results based on file content hashes.

Cache Location Patterns:
- DiskCache: <cache_base>/file_analyzer_cache/<shard>/<filename>.pkl
- SQLiteCache: <cache_base>/file_analyzer_cache.db
- MemoryCache: In-memory only (no persistent storage)
- RedisCache: Redis server (external storage)
- NullCache: No storage (always miss)

Note: DiskCache uses a subdirectory structure with sharding for performance,
while SQLiteCache stores everything in a single database file.
"""

import hashlib
import os
import pickle
import sqlite3
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, fields, is_dataclass, MISSING
from pathlib import Path
from typing import Dict, List, Optional

from compiletools.file_analyzer import FileAnalysisResult


def _compute_dataclass_hash(cls) -> str:
    """Compute a hash of the dataclass structure for automatic version detection.
    
    This creates a deterministic hash based on field names, types, and defaults.
    Any change to the dataclass structure will result in a different hash.
    
    Args:
        cls: Dataclass to hash
        
    Returns:
        Short hash string representing the class structure
    """
    if not is_dataclass(cls):
        raise ValueError(f"{cls} is not a dataclass")
    
    # Collect field information
    fields_info = []
    for field in fields(cls):
        field_info = (
            field.name,
            str(field.type),  # Convert type annotation to string
            field.default if field.default != MISSING else None,
            str(field.default_factory) if field.default_factory != MISSING else None
        )
        fields_info.append(field_info)
    
    # Sort by field name for deterministic ordering
    fields_info.sort(key=lambda x: x[0])
    
    # Create hash from field structure
    hash_input = str(fields_info).encode('utf-8')
    return hashlib.sha256(hash_input).hexdigest()[:12]  # 12 chars should be enough


# Compute cache format version automatically from FileAnalysisResult structure
CACHE_FORMAT_VERSION = _compute_dataclass_hash(FileAnalysisResult)


class FileAnalyzerCache(ABC):
    """Abstract base class for FileAnalyzer result caching."""
    
    def _serialize_result(self, result: FileAnalysisResult) -> bytes:
        """Serialize FileAnalysisResult with version info for cache storage.
        
        Args:
            result: FileAnalysisResult to serialize
            
        Returns:
            Pickled bytes containing versioned cache data
        """
        cache_data = {
            "version": CACHE_FORMAT_VERSION,
            "data": asdict(result)
        }
        return pickle.dumps(cache_data)
    
    def _deserialize_result(self, data: bytes) -> Optional[FileAnalysisResult]:
        """Deserialize cached data with version compatibility checking.
        
        Args:
            data: Pickled bytes from cache
            
        Returns:
            FileAnalysisResult if compatible, None if incompatible/corrupted
        """
        try:
            cache_data = pickle.loads(data)
            
            # Handle legacy format (direct FileAnalysisResult dict)
            if isinstance(cache_data, dict) and "version" not in cache_data:
                # Try to reconstruct from legacy format
                return self._try_legacy_format(cache_data)
            
            # Handle versioned format
            if not isinstance(cache_data, dict) or "version" not in cache_data:
                return None
                
            version = cache_data.get("version")
            if version != CACHE_FORMAT_VERSION:
                # Version mismatch - could add migration logic here in future
                return None
                
            result_data = cache_data.get("data")
            if not isinstance(result_data, dict):
                return None
                
            # Validate required fields exist and have correct types
            return self._validate_and_construct(result_data)
            
        except (pickle.UnpicklingError, TypeError, ValueError, KeyError):
            # Any deserialization error -> treat as cache miss
            return None
    
    def _try_legacy_format(self, data: dict) -> Optional[FileAnalysisResult]:
        """Try to reconstruct FileAnalysisResult from legacy cache format.
        
        Args:
            data: Dictionary that might be legacy FileAnalysisResult
            
        Returns:
            FileAnalysisResult if valid legacy format, None otherwise
        """
        try:
            return self._validate_and_construct(data)
        except (TypeError, ValueError, KeyError):
            return None
    
    def _validate_and_construct(self, data: dict) -> Optional[FileAnalysisResult]:
        """Validate data structure and construct FileAnalysisResult.
        
        Args:
            data: Dictionary with FileAnalysisResult fields
            
        Returns:
            FileAnalysisResult if data is valid, None otherwise
        """
        try:
            # Check required fields exist
            required_fields = {
                "text": str,
                "include_positions": list, 
                "magic_positions": list,
                "directive_positions": dict,
                "bytes_analyzed": int,
                "was_truncated": bool
            }
            
            for field, expected_type in required_fields.items():
                if field not in data:
                    return None
                if not isinstance(data[field], expected_type):
                    return None
            
            # Validate list contents
            if not all(isinstance(pos, int) for pos in data["include_positions"]):
                return None
            if not all(isinstance(pos, int) for pos in data["magic_positions"]):
                return None
            if not all(isinstance(v, list) and all(isinstance(p, int) for p in v) 
                      for v in data["directive_positions"].values()):
                return None
                
            # Construct FileAnalysisResult
            return FileAnalysisResult(**data)
            
        except (TypeError, ValueError):
            return None
    
    @abstractmethod
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve cached analysis result.
        
        Args:
            filepath: Path to the file (for cache organization)
            content_hash: Hash of file content
            
        Returns:
            Cached FileAnalysisResult or None if not found
        """
        pass
    
    @abstractmethod
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store analysis result in cache.
        
        Args:
            filepath: Path to the file (for cache organization) 
            content_hash: Hash of file content
            result: FileAnalysisResult to cache
        """
        pass
    
    @abstractmethod
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear cache entries.
        
        Args:
            filepath: If provided, clear only entries for this file.
                     Otherwise clear entire cache.
        """
        pass
    


class NullCache(FileAnalyzerCache):
    """No-op cache implementation that never caches anything."""
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Always returns None (no caching)."""
        return None
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Does nothing (no caching)."""
        pass
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Does nothing (no cache to clear)."""
        pass


class MemoryCache(FileAnalyzerCache):
    """In-memory cache implementation using a dictionary."""
    
    def __init__(self, max_entries: int = 1000):
        """Initialize memory cache.
        
        Args:
            max_entries: Maximum number of entries to cache
        """
        self._cache: Dict[str, FileAnalysisResult] = {}
        self._max_entries = max_entries
        self._access_order: List[str] = []
    
    def _make_key(self, filepath: str, content_hash: str) -> str:
        """Create cache key from filepath and content hash."""
        return f"{filepath}:{content_hash}"
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from memory cache."""
        key = self._make_key(filepath, content_hash)
        result = self._cache.get(key)
        
        if result:
            # Update LRU order
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)
            
        return result
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in memory cache with LRU eviction."""
        key = self._make_key(filepath, content_hash)
        
        # Evict oldest if at capacity
        if len(self._cache) >= self._max_entries and key not in self._cache:
            if self._access_order:
                oldest_key = self._access_order.pop(0)
                del self._cache[oldest_key]
        
        self._cache[key] = result
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear memory cache."""
        if filepath:
            # Clear only entries for specific file
            keys_to_remove = [k for k in self._cache if k.startswith(f"{filepath}:")]
            for key in keys_to_remove:
                del self._cache[key]
                if key in self._access_order:
                    self._access_order.remove(key)
        else:
            # Clear entire cache
            self._cache.clear()
            self._access_order.clear()


class DiskCache(FileAnalyzerCache):
    """Disk-based cache using pickle files in a directory structure.
    
    Storage pattern: <cache_base>/file_analyzer_cache/<shard>/<filename>.pkl
    Uses first 2 chars of content hash for shard subdirectory to avoid
    too many files in a single directory.
    """
    
    def __init__(self, cache_dir: Optional[str] = None):
        """Initialize disk cache.
        
        Args:
            cache_dir: Directory for cache files. If None, uses dirnamer-style location.
        """
        if cache_dir is None:
            # Use dirnamer-style cache directory
            import compiletools.dirnamer
            cache_base = compiletools.dirnamer.user_cache_dir()
            if cache_base == "None":
                # Caching disabled, use temp directory
                import tempfile
                cache_base = tempfile.gettempdir()
            self._cache_dir = Path(cache_base) / "file_analyzer_cache"
        else:
            self._cache_dir = Path(cache_dir)
        
        # Create cache directory if needed
        self._cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_cache_path(self, filepath: str, content_hash: str) -> Path:
        """Get cache file path for given file and hash."""
        # Use first 2 chars of hash for subdirectory to avoid too many files in one dir
        subdir = content_hash[:2] if content_hash else "00"
        filename = f"{hashlib.md5(filepath.encode()).hexdigest()}_{content_hash}.pkl"
        return self._cache_dir / subdir / filename
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from disk cache."""
        cache_path = self._get_cache_path(filepath, content_hash)
        
        if cache_path.exists():
            try:
                with open(cache_path, 'rb') as f:
                    data = f.read()
                    result = self._deserialize_result(data)
                    
                    if result is None:
                        # Incompatible/corrupted data, remove cache file
                        try:
                            cache_path.unlink()
                        except OSError:
                            pass
                    
                    return result
            except (IOError, OSError):
                # Cache file read error, remove it
                try:
                    cache_path.unlink()
                except OSError:
                    pass
                    
        return None
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in disk cache."""
        cache_path = self._get_cache_path(filepath, content_hash)
        
        # Create subdirectory if needed
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Serialize with version info
            serialized_data = self._serialize_result(result)
            
            # Write to temp file first then rename for atomicity
            with tempfile.NamedTemporaryFile(mode='wb', dir=cache_path.parent, delete=False) as f:
                temp_path = f.name
                f.write(serialized_data)
            
            # Atomic rename
            os.replace(temp_path, cache_path)
            
        except (IOError, OSError):
            # Clean up temp file if it exists
            try:
                if 'temp_path' in locals():
                    os.unlink(temp_path)
            except OSError:
                pass
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear disk cache."""
        if filepath:
            # Clear only entries for specific file
            filepath_hash = hashlib.md5(filepath.encode()).hexdigest()
            for subdir in self._cache_dir.iterdir():
                if subdir.is_dir():
                    for cache_file in subdir.glob(f"{filepath_hash}_*.pkl"):
                        try:
                            cache_file.unlink()
                        except OSError:
                            pass
        else:
            # Clear entire cache directory
            import shutil
            try:
                shutil.rmtree(self._cache_dir)
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


class SQLiteCache(FileAnalyzerCache):
    """SQLite-based cache for persistent storage with efficient queries.
    
    Storage pattern: <cache_base>/file_analyzer_cache.db
    Stores all cache entries in a single SQLite database file with
    indexed queries for efficient lookup.
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize SQLite cache.
        
        Args:
            db_path: Path to SQLite database. If None, uses dirnamer-style location.
        """
        if db_path is None:
            import compiletools.dirnamer
            cache_base = compiletools.dirnamer.user_cache_dir()
            if cache_base == "None":
                # Caching disabled, use temp directory
                import tempfile
                cache_base = tempfile.gettempdir()
            db_dir = Path(cache_base)
            db_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = db_dir / "file_analyzer_cache.db"
        else:
            self._db_path = Path(db_path)
        
        # Initialize database and connection
        self._conn = None
        self._init_db()

    def __del__(self):
        """Close database connection on object destruction."""
        self.close()

    def close(self):
        """Explicitly close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection, creating if it doesn't exist."""
        if self._conn is None or self._is_connection_closed():
            if self._conn:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = sqlite3.connect(self._db_path)
        return self._conn
    
    def _is_connection_closed(self) -> bool:
        """Check if the current connection is closed or invalid."""
        if self._conn is None:
            return True
        try:
            # Try a simple query to check if connection is valid
            self._conn.execute("SELECT 1")
            return False
        except sqlite3.Error:
            return True
    
    def _init_db(self):
        """Initialize SQLite database schema."""
        conn = self._get_conn()
        # Enable WAL mode to reduce journal file operations
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                filepath TEXT,
                content_hash TEXT,
                result BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (filepath, content_hash)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_filepath ON cache(filepath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON cache(content_hash)")
        conn.commit()
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from SQLite cache."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT result FROM cache WHERE filepath = ? AND content_hash = ?",
            (filepath, content_hash)
        )
        row = cursor.fetchone()
        
        if row:
            result = self._deserialize_result(row[0])
            if result is None:
                # Incompatible/corrupted entry, delete it
                conn.execute(
                    "DELETE FROM cache WHERE filepath = ? AND content_hash = ?",
                    (filepath, content_hash)
                )
                conn.commit()
            return result
                
        return None
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in SQLite cache."""
        conn = self._get_conn()
        # Serialize with version info
        data = self._serialize_result(result)
        
        conn.execute(
            "INSERT OR REPLACE INTO cache (filepath, content_hash, result) VALUES (?, ?, ?)",
            (filepath, content_hash, data)
        )
        conn.commit()
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear SQLite cache."""
        conn = self._get_conn()
        if filepath:
            conn.execute("DELETE FROM cache WHERE filepath = ?", (filepath,))
        else:
            conn.execute("DELETE FROM cache")
        conn.commit()


class RedisCache(FileAnalyzerCache):
    """Redis-based cache for distributed caching."""
    
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0, 
                 ttl: int = 3600, key_prefix: str = 'ct_file_analyzer:'):
        """Initialize Redis cache.
        
        Args:
            host: Redis server host
            port: Redis server port
            db: Redis database number
            ttl: Time-to-live for cache entries in seconds
            key_prefix: Prefix for cache keys
        """
        try:
            import redis
        except ImportError:
            self._available = False
            return
            
        try:
            self._redis = redis.Redis(host=host, port=port, db=db, decode_responses=False)
            self._ttl = ttl
            self._key_prefix = key_prefix
            # Test connection
            self._redis.ping()
            self._available = True
        except redis.ConnectionError:
            self._available = False
    
    def _make_key(self, filepath: str, content_hash: str) -> str:
        """Create Redis key from filepath and content hash."""
        return f"{self._key_prefix}{filepath}:{content_hash}"
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from Redis cache."""
        if not self._available:
            return None
            
        key = self._make_key(filepath, content_hash)
        
        try:
            data = self._redis.get(key)
            if data:
                return self._deserialize_result(data)
        except Exception:
            # Redis error or deserialization error
            pass
            
        return None
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in Redis cache with TTL."""
        if not self._available:
            return
            
        key = self._make_key(filepath, content_hash)
        
        try:
            data = self._serialize_result(result)
            self._redis.setex(key, self._ttl, data)
        except Exception:
            # Redis error
            pass
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear Redis cache."""
        if not self._available:
            return
            
        try:
            if filepath:
                # Clear only entries for specific file
                pattern = f"{self._key_prefix}{filepath}:*"
                for key in self._redis.scan_iter(match=pattern):
                    self._redis.delete(key)
            else:
                # Clear all entries with our prefix
                pattern = f"{self._key_prefix}*"
                for key in self._redis.scan_iter(match=pattern):
                    self._redis.delete(key)
        except Exception:
            # Redis error
            pass


def create_cache(cache_type: str = 'disk', **kwargs) -> FileAnalyzerCache:
    """Factory function to create cache instance.
    
    Args:
        cache_type: Type of cache ('null', 'memory', 'disk', 'sqlite', 'redis')
        **kwargs: Additional arguments for cache constructor
        
    Returns:
        FileAnalyzerCache instance
    """
    cache_types = {
        'null': NullCache,
        'memory': MemoryCache,
        'disk': DiskCache,
        'sqlite': SQLiteCache,
        'redis': RedisCache,
    }
    
    cache_class = cache_types.get(cache_type.lower())
    if not cache_class:
        raise ValueError(f"Unknown cache type: {cache_type}")
    
    return cache_class(**kwargs)


def batch_analyze_files(filepaths: list[str], cache_type: str = 'disk', **cache_kwargs) -> dict[str, 'FileAnalysisResult']:
    """Efficiently analyze multiple files with optimal batching.
    
    This function optimizes performance by computing all file hashes in a single
    Git call, then analyzing only files that aren't cached.
    
    Args:
        filepaths: List of file paths to analyze
        cache_type: Type of cache to use ('null', 'memory', 'disk', 'sqlite', 'redis')
        **cache_kwargs: Additional arguments for cache constructor
        
    Returns:
        Dictionary mapping filepath to FileAnalysisResult
    """
    from compiletools.file_analyzer import create_file_analyzer
    
    if not filepaths:
        return {}
    
    # Create cache
    cache = create_cache(cache_type, **cache_kwargs)
    
    # Get all file hashes from global registry
    from compiletools.global_hash_registry import get_file_hash_from_registry
    
    results = {}
    files_to_analyze = []
    
    # Check cache for each file
    for filepath in filepaths:
        content_hash = get_file_hash_from_registry(filepath)
        if not content_hash:
            # File not in registry - this is an error condition
            raise RuntimeError(f"File not found in global hash registry: {filepath}. "
                              "This indicates the file was not present during startup or "
                              "the global hash registry was not properly initialized.")
        
        cached_result = cache.get(filepath, content_hash)
        if cached_result is not None:
            results[filepath] = cached_result
        else:
            files_to_analyze.append((filepath, content_hash))
    
    # Analyze uncached files
    for filepath, content_hash in files_to_analyze:
        analyzer = create_file_analyzer(filepath)
        result = analyzer.analyze()
        results[filepath] = result
        
        # Cache the result (content_hash is guaranteed to exist)
        cache.put(filepath, content_hash, result)
    
    return results