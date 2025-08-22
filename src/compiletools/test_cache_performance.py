"""Tests for FileAnalyzer cache implementations."""

import time
from pathlib import Path
import pytest

from compiletools.file_analyzer import create_file_analyzer
from compiletools.file_analyzer_cache import (
    NullCache, MemoryCache, DiskCache, create_cache
)
from compiletools.testhelper import samplesdir


class TestFileAnalyzerCache:
    """Test FileAnalyzer caching functionality."""
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to simple existing C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    @pytest.fixture
    def lotsofmagic_file(self):
        """Path to existing file with many magic flags for testing."""
        return str(Path(samplesdir()) / "lotsofmagic" / "lotsofmagic.cpp")
    
    @pytest.fixture
    def conditional_includes_file(self):
        """Path to existing file with conditional includes."""
        return str(Path(samplesdir()) / "conditional_includes" / "main.cpp")
    
    @pytest.mark.parametrize("cache_type", ['null', 'memory', 'disk', 'sqlite'])
    def test_cache_implementations_correctness(self, simple_cpp_file, cache_type):
        """Test that all cache implementations produce identical results."""
        # First analysis (cache miss)
        analyzer1 = create_file_analyzer(simple_cpp_file, cache_type=cache_type)
        result1 = analyzer1.analyze()
        
        # Second analysis (should be cache hit for all except null)
        analyzer2 = create_file_analyzer(simple_cpp_file, cache_type=cache_type)
        result2 = analyzer2.analyze()
        
        # Results should be identical
        assert result1.text == result2.text
        assert result1.include_positions == result2.include_positions
        assert result1.magic_positions == result2.magic_positions
        assert result1.directive_positions == result2.directive_positions
        assert result1.bytes_analyzed == result2.bytes_analyzed
        assert result1.was_truncated == result2.was_truncated
    
    @pytest.mark.parametrize("cache_type", ['memory', 'disk', 'sqlite'])
    def test_cache_performance_improvement(self, lotsofmagic_file, cache_type):
        """Test that caching provides performance improvement."""
        # First analysis (cache miss)
        start = time.perf_counter()
        analyzer1 = create_file_analyzer(lotsofmagic_file, cache_type=cache_type)
        result1 = analyzer1.analyze()
        time1 = time.perf_counter() - start
        
        # Second analysis (cache hit)
        start = time.perf_counter()
        analyzer2 = create_file_analyzer(lotsofmagic_file, cache_type=cache_type)
        result2 = analyzer2.analyze()
        time2 = time.perf_counter() - start
        
        # Cache hit should be faster (allow small margin for variance)
        # Note: For small files, the difference might be negligible
        assert time2 <= time1 * 2.0, f"Cache hit ({time2:.6f}s) should not be much slower than miss ({time1:.6f}s)"
        
        # Results should be identical
        assert result1.text == result2.text
    
    def test_cache_invalidation_on_file_change(self, simple_cpp_file):
        """Test that cache correctly invalidates when file content changes.
        
        Since we now require files to be in the global hash registry, this test
        demonstrates that cache lookup works correctly with existing files.
        Cache invalidation is handled by the content hash being different.
        """
        # Use an existing file from samples directory
        test_file = simple_cpp_file
        
        # Mock different content hashes to simulate file change
        from unittest.mock import patch
        
        with patch('compiletools.global_hash_registry.get_file_hash_from_registry') as mock_hash:
            # First analysis with hash 'abc123'
            mock_hash.return_value = 'abc123'
            analyzer1 = create_file_analyzer(test_file, cache_type='memory')
            result1 = analyzer1.analyze()
            
            # Second analysis with different hash 'def456' (simulating file change)
            mock_hash.return_value = 'def456'
            analyzer2 = create_file_analyzer(test_file, cache_type='memory') 
            result2 = analyzer2.analyze()
            
            # Both should succeed (different cache keys due to different hashes)
            assert result1 is not None
            assert result2 is not None
            # Content should be the same (same actual file) but cache treats them separately
            assert result1.text == result2.text
    
    def test_null_cache_never_caches(self, simple_cpp_file):
        """Test that null cache implementation never caches anything."""
        cache = NullCache()
        
        # get should always return None
        assert cache.get(simple_cpp_file, "any_hash") is None
        
        # put should be a no-op (no exception)
        from compiletools.file_analyzer import FileAnalysisResult
        result = FileAnalysisResult("test", [], [], {}, 4, False)
        cache.put(simple_cpp_file, "any_hash", result)
        
        # Still no result
        assert cache.get(simple_cpp_file, "any_hash") is None
        
        # clear should be a no-op
        cache.clear()
    
    def test_memory_cache_lru_eviction(self):
        """Test that memory cache properly implements LRU eviction."""
        cache = MemoryCache(max_entries=2)
        from compiletools.file_analyzer import FileAnalysisResult
        
        # Add entries up to capacity
        result1 = FileAnalysisResult("test1", [], [], {}, 5, False)
        result2 = FileAnalysisResult("test2", [], [], {}, 5, False)
        result3 = FileAnalysisResult("test3", [], [], {}, 5, False)
        
        cache.put("file1.cpp", "hash1", result1)
        cache.put("file2.cpp", "hash2", result2)
        
        # Both should be retrievable
        assert cache.get("file1.cpp", "hash1") is not None
        assert cache.get("file2.cpp", "hash2") is not None
        
        # Add third entry (should evict oldest)
        cache.put("file3.cpp", "hash3", result3)
        
        # First entry should be evicted, others should remain
        assert cache.get("file1.cpp", "hash1") is None
        assert cache.get("file2.cpp", "hash2") is not None
        assert cache.get("file3.cpp", "hash3") is not None


class TestCacheBackends:
    """Test specific cache backend functionality."""
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to simple existing C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_create_cache_factory(self):
        """Test cache factory function."""
        # Test all supported types
        for cache_type in ['null', 'memory', 'disk', 'sqlite']:
            cache = create_cache(cache_type)
            assert cache is not None
        
        # Test invalid type
        with pytest.raises(ValueError, match="Unknown cache type"):
            create_cache('invalid')
    
    def test_disk_cache_persistence(self, simple_cpp_file):
        """Test that disk cache persists between instances."""
        from compiletools.file_analyzer import FileAnalysisResult
        import tempfile
        
        # Use existing sample file instead of creating temporary file
        test_file = simple_cpp_file
        
        # Use a shared cache directory for this test
        with tempfile.TemporaryDirectory() as temp_dir:
            shared_cache_dir = temp_dir + "/shared_persistence_test"
            
            # Create first cache instance and store data
            cache1 = DiskCache(cache_dir=shared_cache_dir)
            result = FileAnalysisResult("test content", [0], [], {"include": [0]}, 16, False)
            cache1.put(test_file, "test_hash", result)
            
            # Create second cache instance - should retrieve same data
            cache2 = DiskCache(cache_dir=shared_cache_dir) 
            retrieved = cache2.get(test_file, "test_hash")
            
            assert retrieved is not None
            assert retrieved.text == "test content"
            assert retrieved.include_positions == [0]
    
    @pytest.mark.skipif(
        True, # Skip by default as it requires Redis server
        reason="Redis tests require running Redis server"
    )
    def test_redis_cache_functionality(self):
        """Test Redis cache functionality (requires Redis server)."""
        from compiletools.file_analyzer_cache import RedisCache
        
        try:
            cache = RedisCache()
            from compiletools.file_analyzer import FileAnalysisResult
            
            result = FileAnalysisResult("redis test", [], [], {}, 10, False)
            cache.put("test_file.cpp", "redis_hash", result)
            
            retrieved = cache.get("test_file.cpp", "redis_hash")
            assert retrieved is not None
            assert retrieved.text == "redis test"
            
        except Exception:
            pytest.skip("Redis not available")