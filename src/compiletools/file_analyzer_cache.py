"""Cache implementations for FileAnalyzer results.

This module provides multiple cache backends for FileAnalysisResult objects,
allowing efficient reuse of analysis results based on file content hashes.

Cache Location Patterns:
- DiskCache: <cache_base>/file_analyzer_cache_shared_v{VERSION}/<shard>/<filename>.pkl
- SQLiteCache: <cache_base>/file_analyzer_cache_shared_v{VERSION}.db  
- MemoryCache: In-memory only (no persistent storage)
- RedisCache: Redis server with versioned keys (external storage)
- NullCache: No storage (always miss)

LOCKLESS ARCHITECTURE:
====================

The persistent caches (DiskCache, SQLiteCache) use a lockless design that achieves
safe concurrent access without explicit file locking. This works because:

1. CONTENT-ADDRESSED FILENAMES:
   Cache files are named using both filepath hash AND content hash:
   {md5(filepath)}_{content_hash}.pkl
   
   Same input file + same content = identical filename = identical cache data
   Different content = different filename = separate cache files

2. ATOMIC RENAME OPERATION:
   Files are written to temporary names, then atomically renamed to final names.
   os.replace() provides POSIX atomic rename on all major filesystems:
   - XFS: Fully atomic, journaled
   - GPFS: Atomic within directory (safe for cache use)  
   - ext4, btrfs, APFS: Full atomic support
   
   Readers see either: no file (cache miss) OR complete file (cache hit)
   Never: partial/corrupted file

3. IDENTICAL CONTENT PROPERTY:
   Multiple processes analyzing the same file content produce bit-identical
   pickle data. Race conditions become harmless:
   - Multiple writers: All write identical data, last writer "wins" safely
   - Reader during write: Atomic rename ensures consistency
   - Cleanup during write: File gets recreated immediately if needed

4. WRITE-ONCE SEMANTICS:
   Cache files never change after creation. Content hash guarantees that
   file content + analysis code version = deterministic result.
   No update races, no corruption from partial writes.

5. VERSION ISOLATION:
   Cache format version is embedded in directory/database names, not individual
   files. Version incompatibility is handled at the path level:
   - /cache_v123/ vs /cache_v456/ are completely separate
   - No runtime version checking needed
   - Automatic cleanup of old versions possible

PERFORMANCE BENEFITS:
- Zero locking overhead on reads (most common operation)
- Zero lock contention delays
- True parallel access scales linearly
- Simplified code paths reduce CPU usage
- Works efficiently on network filesystems (GPFS, NFS)

SAFETY ANALYSIS:
This approach is safe because the combination of content-addressed naming,
atomic operations, and identical content eliminates all traditional race
conditions that require locking to resolve.

KEY INSIGHT: When multiple processes would write identical data to identical
filenames, race conditions become harmless - any winner produces the correct
result. This transforms a traditionally complex concurrency problem into a
simple, fast, lock-free design.
"""

import functools
import hashlib
import os
import pickle
import random
import sqlite3
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import fields, is_dataclass, MISSING
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
    """Disk-based cache using pickle files in a versioned directory structure.
    
    Storage pattern: <cache_base>/file_analyzer_cache_shared_v{VERSION}/<shard>/<filename>.pkl
    Uses CACHE_FORMAT_VERSION in directory path for explicit version compatibility.
    Files are direct pickle dumps with no version wrapper for faster I/O.
    
    LOCKLESS CONCURRENT SAFETY:
    ===========================
    
    This implementation safely handles concurrent access from multiple processes
    without any file locking, using the following design principles:
    
    1. Content-Addressed Naming:
       Filename = md5(filepath) + "_" + content_hash + ".pkl"
       • Same file content always maps to same cache filename
       • Different content maps to different filenames (no conflicts)
       • Cache key includes both source file identity AND content hash
    
    2. Write-Once Semantics:
       • Cache files are never modified after creation
       • Content hash ensures same input → same output always
       • No need to handle update races or partial writes
    
    3. Atomic Write Pattern:
       • Write to temporary file: tempfile.NamedTemporaryFile()
       • Atomic rename: os.replace(temp_file, final_file)
       • POSIX guarantees: readers see complete file or no file
       • No locks needed because operation is atomic at OS level
    
    4. Identical Content Property:
       • Multiple processes analyzing same content produce identical pickle data
       • Race condition outcomes:
         * Process A writes file → Process B reads complete file ✓
         * Process A and B write simultaneously → Last writer wins, both contain identical data ✓
         * Process A reads while B writes → A sees old state or new state, never partial ✓
    
    5. Graceful Degradation:
       • File corruption/deletion: Regenerate cache entry automatically
       • Version mismatch: Handled by directory-level isolation
       • Disk full: Fails safely, falls back to direct analysis
    
    Performance characteristics:
    • Read operations: No locking overhead, scales linearly with CPU cores
    • Write operations: Only filesystem atomic rename cost
    • Concurrent safety: Zero contention, works on network filesystems
    """
    
    def __init__(self, cache_dir: Optional[str] = None):
        """Initialize disk cache with integrated LRU memory cache.
        
        Args:
            cache_dir: Directory for cache files. If None, uses dirnamer-style location.
        """
        if cache_dir is None:
            # Use dirnamer-style cache directory with version suffix
            import compiletools.dirnamer
            cache_base = compiletools.dirnamer.user_cache_dir()
            if cache_base == "None":
                # Caching disabled, use temp directory with unique names for parallel tests
                import tempfile
                import threading
                cache_base = tempfile.gettempdir()
                # Use unique names in temp directory for tests to prevent conflicts between parallel test runs
                unique_id = f"{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000000)}"
                cache_name = f"file_analyzer_cache_shared_v{CACHE_FORMAT_VERSION}_{unique_id}"
            else:
                cache_name = f"file_analyzer_cache_shared_v{CACHE_FORMAT_VERSION}"
            self._cache_dir = Path(cache_base) / cache_name
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
    
    @functools.lru_cache(maxsize=None)
    def _load_from_disk(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Load result from disk cache with infinite LRU caching for performance.
        
        This method is decorated with @lru_cache to provide fast memory caching
        of deserialized results, eliminating repeated disk I/O and pickle.loads()
        for the same (filepath, content_hash) combinations.
        """
        cache_path = self._get_cache_path(filepath, content_hash)
        
        if cache_path.exists():
            try:
                with cache_path.open('rb') as f:
                    data = f.read()
                    # Direct pickle load - version compatibility guaranteed by directory path
                    result = pickle.loads(data)
                    return result
            except (IOError, OSError, pickle.UnpicklingError, TypeError, ValueError, ModuleNotFoundError):
                # Cache file read/corruption error, remove it and regenerate
                try:
                    cache_path.unlink()
                except OSError:
                    pass  # Best effort cleanup
                    
        return None
    
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from disk cache with LRU memory caching for performance.
        
        Uses @lru_cache decorated _load_from_disk() method to provide fast
        memory access for repeated queries while maintaining lockless disk
        cache safety guarantees.
        """
        return self._load_from_disk(filepath, content_hash)
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in disk cache and populate LRU memory cache.
        
        LOCKLESS SAFETY EXPLANATION:
        This method is safe for concurrent execution because:
        
        1. Content-addressed filename ensures identical input → identical filename
        2. Identical input always produces identical pickle data  
        3. Atomic rename ensures readers never see partial files
        4. Race conditions have safe outcomes:
           • Multiple writers: All write identical data, any winner is correct
           • Reader during write: Sees old state or new state, never corrupted
           • Concurrent directory creation: mkdir(exist_ok=True) handles this
        
        No locking needed because filesystem atomicity + identical content = safety.
        """
        cache_path = self._get_cache_path(filepath, content_hash)
        
        # Skip if file already exists (write-once semantics)
        # Multiple processes may check this simultaneously - that's fine,
        # they'll all write identical content if they proceed
        if cache_path.exists():
            # File exists - populate LRU cache for future fast access
            self._load_from_disk(filepath, content_hash)
            return
            
        # Create subdirectory if needed
        # Multiple processes may create same directory - exist_ok=True handles this
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Direct pickle serialization - no version wrapper needed
            # Same input always produces bit-identical pickle data
            serialized_data = pickle.dumps(result)
            
            # ATOMIC WRITE PATTERN:
            # 1. Write to temporary file in same directory (same filesystem)
            # 2. Use atomic rename to final filename
            # This ensures readers see either: no file OR complete file (never partial)
            with tempfile.NamedTemporaryFile(mode='wb', dir=cache_path.parent, delete=False) as f:
                temp_path = f.name
                f.write(serialized_data)
            
            # ATOMIC RENAME: This is the critical operation that ensures safety
            # os.replace() is atomic on POSIX systems - readers will never see partial writes
            # If multiple processes rename to same target, last writer wins with identical data
            os.replace(temp_path, cache_path)
            
            # Populate LRU cache after successful write
            self._load_from_disk(filepath, content_hash)
            
        except (IOError, OSError):
            # Clean up temp file if rename failed
            # This is best-effort cleanup - temp files will be cleaned up by OS eventually
            try:
                if 'temp_path' in locals():
                    os.unlink(temp_path)
            except OSError:
                pass  # Ignore cleanup failures
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear disk cache entries and LRU memory cache."""
        # Clear LRU memory cache
        self._load_from_disk.cache_clear()
        
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
            # Clear entire cache directory and recreate
            import shutil
            try:
                shutil.rmtree(self._cache_dir)
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
    
    def cleanup_old_versions(self) -> None:
        """Remove cache directories from previous CACHE_FORMAT_VERSION values.
        
        This method identifies old version directories and removes them to free up disk space.
        Should be called periodically or after version changes.
        """
        if not self._cache_dir.parent.exists():
            return
            
        current_version_suffix = f"_v{CACHE_FORMAT_VERSION}"
        
        try:
            # Look for old cache directories in the same parent
            for item in self._cache_dir.parent.iterdir():
                if not item.is_dir():
                    continue
                    
                # Check if this looks like an old version cache directory
                item_name = item.name
                if (item_name.startswith("file_analyzer_cache") and 
                    "_v" in item_name and 
                    not item_name.endswith(current_version_suffix) and
                    item != self._cache_dir):
                    
                    # Remove old version directory
                    import shutil
                    try:
                        shutil.rmtree(item)
                    except OSError:
                        pass  # Ignore errors, best effort cleanup
                        
        except OSError:
            pass  # Ignore errors during cleanup


class SQLiteCache(FileAnalyzerCache):
    """SQLite-based cache for persistent storage with efficient queries.
    
    Storage pattern: <cache_base>/file_analyzer_cache_v{VERSION}.db
    Uses CACHE_FORMAT_VERSION in database filename for explicit version compatibility.
    Stores direct pickle dumps with no version wrapper for faster serialization.
    
    CONCURRENCY SAFETY:
    ==================
    
    This implementation relies on SQLite's built-in concurrency control rather than
    explicit file locking:
    
    1. SQLite WAL Mode:
       • Enables concurrent readers with single writer
       • Writers don't block readers
       • Built-in busy timeout handles contention
    
    2. Content Deduplication:
       • Same (filepath, content_hash) always produces identical data
       • INSERT OR REPLACE is idempotent for identical content
       • Race conditions result in same final state
    
    3. Database-Level Atomicity:
       • SQLite provides ACID guarantees
       • Transactions are atomic and isolated
       • No partial reads/writes at database level
    
    4. Version Isolation:
       • Different cache versions use different database files
       • No cross-version compatibility issues
       • Clean upgrade path by database replacement
    
    Performance: SQLite handles multi-process access efficiently with minimal
    overhead compared to file-level locking approaches.
    """
    
    def __init__(self, db_path: Optional[str] = None, batch_size: int = 1000):
        """Initialize SQLite cache.
        
        Args:
            db_path: Path to SQLite database. If None, uses dirnamer-style location.
            batch_size: Number of operations to batch before committing (1-10000)
        """
        # Clamp batch size to reasonable range
        self._batch_size = max(1, min(batch_size, 10000))
        
        if db_path is None:
            import compiletools.dirnamer
            cache_base = compiletools.dirnamer.user_cache_dir()
            if cache_base == "None":
                # Caching disabled, use temp directory with unique name for parallel tests
                import tempfile
                import threading
                cache_base = tempfile.gettempdir()
                # Use unique names in temp directory for tests to prevent conflicts between parallel test runs
                unique_id = f"{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000000)}"
                db_name = f"file_analyzer_cache_shared_v{CACHE_FORMAT_VERSION}_{unique_id}.db"
            else:
                db_name = f"file_analyzer_cache_shared_v{CACHE_FORMAT_VERSION}.db"
            db_dir = Path(cache_base)
            db_dir.mkdir(parents=True, exist_ok=True)
            self._db_path = db_dir / db_name
        else:
            self._db_path = Path(db_path)
        
        # Initialize database and connection
        self._conn = None
        self._pending_operations = 0
        self._init_db()

    def __del__(self):
        """Close database connection on object destruction."""
        self.close()

    def close(self):
        """Explicitly close the database connection."""
        if self._conn:
            # Flush any pending operations before closing
            self.flush()
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
            # Add timeout to prevent indefinite locking
            self._conn = sqlite3.connect(self._db_path, timeout=10.0)
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
    
    
    def _execute_with_retry(self, conn, sql: str, params=None, max_retries: int = 3):
        """Execute SQL with retry logic for database busy errors."""
        for attempt in range(max_retries + 1):
            try:
                if params is None:
                    return conn.execute(sql)
                else:
                    return conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) or "database is busy" in str(e):
                    if attempt < max_retries:
                        # Exponential backoff with jitter
                        delay = (0.01 * (2 ** attempt)) + random.uniform(0, 0.005)
                        time.sleep(delay)
                        continue
                raise
    
    def _init_db(self):
        """Initialize SQLite database schema for concurrent access."""
        conn = self._get_conn()
        # Use WAL mode for better concurrent access, but optimize for speed
        self._execute_with_retry(conn, 'PRAGMA journal_mode=WAL;')     # Better for concurrent access
        self._execute_with_retry(conn, 'PRAGMA synchronous=NORMAL;')   # Balance durability and speed
        self._execute_with_retry(conn, 'PRAGMA temp_store=MEMORY;')    # Keep temp tables in memory
        self._execute_with_retry(conn, 'PRAGMA cache_size=10000;')     # Larger cache for performance
        self._execute_with_retry(conn, 'PRAGMA busy_timeout=5000;')    # 5 second timeout for concurrent access
        
        self._execute_with_retry(conn, """
            CREATE TABLE IF NOT EXISTS cache (
                filepath TEXT,
                content_hash TEXT,
                result BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (filepath, content_hash)
            )
        """)
        self._execute_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_filepath ON cache(filepath)")
        self._execute_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_hash ON cache(content_hash)")
        conn.commit()
    
    @functools.lru_cache(maxsize=None)
    def _load_from_db(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Load result from SQLite database with infinite LRU caching for performance.
        
        This method is decorated with @lru_cache to provide fast memory caching
        of deserialized results, eliminating repeated SQL queries and pickle.loads()
        for the same (filepath, content_hash) combinations.
        """
        conn = self._get_conn()
        cursor = self._execute_with_retry(
            conn,
            "SELECT result FROM cache WHERE filepath = ? AND content_hash = ?",
            (filepath, content_hash)
        )
        row = cursor.fetchone()
        
        if row:
            try:
                # Direct pickle load - version compatibility guaranteed by database filename
                result = pickle.loads(row[0])
                return result
            except (pickle.UnpicklingError, TypeError, ValueError):
                # Corrupted entry, delete it
                self._execute_with_retry(
                    conn,
                    "DELETE FROM cache WHERE filepath = ? AND content_hash = ?",
                    (filepath, content_hash)
                )
                conn.commit()
                return None
                
        return None
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from SQLite cache with LRU memory caching for performance.
        
        Uses @lru_cache decorated _load_from_db() method to provide fast
        memory access for repeated queries while maintaining SQLite
        concurrency safety guarantees.
        """
        return self._load_from_db(filepath, content_hash)
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in SQLite cache and populate LRU memory cache."""
        conn = self._get_conn()
        # Direct pickle serialization - no version wrapper needed
        data = pickle.dumps(result)
        
        self._execute_with_retry(
            conn,
            "INSERT OR REPLACE INTO cache (filepath, content_hash, result) VALUES (?, ?, ?)",
            (filepath, content_hash, data)
        )
        
        self._pending_operations += 1
        
        # Commit when batch is full
        if self._pending_operations >= self._batch_size:
            self._flush_internal(conn)
        
        # Populate LRU cache for future fast access
        self._load_from_db(filepath, content_hash)
    
    def _flush_internal(self, conn) -> None:
        """Internal flush method for batched operations."""
        if self._pending_operations > 0:
            conn.commit()
            self._pending_operations = 0
    
    def flush(self) -> None:
        """Commit any pending operations to database."""
        if self._conn and self._pending_operations > 0:
            self._flush_internal(self._conn)
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear SQLite cache and LRU memory cache."""
        # Clear LRU memory cache
        self._load_from_db.cache_clear()
        
        # Flush any pending operations first
        if self._conn and self._pending_operations > 0:
            self._flush_internal(self._conn)
        
        conn = self._get_conn()
        if filepath:
            self._execute_with_retry(conn, "DELETE FROM cache WHERE filepath = ?", (filepath,))
        else:
            self._execute_with_retry(conn, "DELETE FROM cache")
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
            key_prefix: Prefix for cache keys (version will be appended)
        """
        try:
            import redis
        except ImportError:
            self._available = False
            return
            
        try:
            self._redis = redis.Redis(host=host, port=port, db=db, decode_responses=False)
            self._ttl = ttl
            # Include version in key prefix for cache isolation
            self._key_prefix = f"{key_prefix}v{CACHE_FORMAT_VERSION}:"
            # Test connection
            self._redis.ping()
            self._available = True
        except redis.ConnectionError:
            self._available = False
    
    def _make_key(self, filepath: str, content_hash: str) -> str:
        """Create Redis key from filepath and content hash."""
        return f"{self._key_prefix}{filepath}:{content_hash}"
    
    @functools.lru_cache(maxsize=None)
    def _load_from_redis(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Load result from Redis cache with infinite LRU caching for performance.
        
        This method is decorated with @lru_cache to provide fast memory caching
        of deserialized results, eliminating repeated Redis queries and pickle.loads()
        for the same (filepath, content_hash) combinations.
        """
        if not self._available:
            return None
            
        key = self._make_key(filepath, content_hash)
        
        try:
            data = self._redis.get(key)
            if data:
                # Direct pickle load - version compatibility handled by key versioning
                return pickle.loads(data)
        except Exception:
            # Redis error or deserialization error
            pass
            
        return None
    
    def get(self, filepath: str, content_hash: str) -> Optional[FileAnalysisResult]:
        """Retrieve from Redis cache with LRU memory caching for performance.
        
        Uses @lru_cache decorated _load_from_redis() method to provide fast
        memory access for repeated queries while maintaining Redis functionality.
        """
        return self._load_from_redis(filepath, content_hash)
    
    def put(self, filepath: str, content_hash: str, result: FileAnalysisResult) -> None:
        """Store in Redis cache and populate LRU memory cache."""
        if not self._available:
            return
            
        key = self._make_key(filepath, content_hash)
        
        try:
            # Direct pickle serialization - no version wrapper needed
            data = pickle.dumps(result)
            self._redis.setex(key, self._ttl, data)
            
            # Populate LRU cache for future fast access
            self._load_from_redis(filepath, content_hash)
        except Exception:
            # Redis error
            pass
    
    def clear(self, filepath: Optional[str] = None) -> None:
        """Clear Redis cache and LRU memory cache."""
        # Clear LRU memory cache
        self._load_from_redis.cache_clear()
        
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
    
    All persistent cache types (disk, sqlite, redis, mmap) now have integrated
    infinite LRU memory caching for optimal performance.
    
    Args:
        cache_type: Type of cache ('null', 'memory', 'disk', 'sqlite', 'redis', 'mmap', 'oracle')
        **kwargs: Additional arguments for cache constructor
        
    Returns:
        FileAnalyzerCache instance with LRU caching built-in for persistent types
    """
    # Import oracle cache here to avoid circular imports
    from compiletools.mmap_oracle_cache import MMapOracleCache
    
    cache_types = {
        'null': NullCache,
        'memory': MemoryCache,
        'disk': DiskCache,
        'sqlite': SQLiteCache,
        'redis': RedisCache,
        'mmap': MMapOracleCache,
        'oracle': MMapOracleCache,  # Alias for mmap
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
    from compiletools.global_hash_registry import get_file_hash
    
    results = {}
    files_to_analyze = []
    
    # Check cache for each file
    for filepath in filepaths:
        content_hash = get_file_hash(filepath)
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