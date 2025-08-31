"""Tests for MMapOracleCache implementation."""

import hashlib
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from compiletools.file_analyzer import FileAnalysisResult
from compiletools.mmap_oracle_cache import MMapOracleCache
from compiletools.file_analyzer_cache import CACHE_FORMAT_VERSION
from compiletools.testhelper import samplesdir


def generate_test_hash(identifier: str) -> str:
    """Generate a valid SHA1 hash for testing purposes."""
    return hashlib.sha1(f"test_content_{identifier}".encode()).hexdigest()


class TestMMapOracleCacheBasics:
    """Test basic oracle cache functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        
    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        
    def create_test_result(self, content="test content", positions=None):
        """Create a test FileAnalysisResult with all structured fields."""
        if positions is None:
            positions = [0, 10, 20]
            
        return FileAnalysisResult(
            lines=content.split('\n') if content else [],
            line_byte_offsets=[0] if content else [],
            include_positions=positions,
            magic_positions=[5, 15],
            directive_positions={"include": positions, "define": [25]},
            directives=[],
            directive_by_line={},
            bytes_analyzed=len(content.encode('utf-8')) if content else 0,
            was_truncated=False,
            # Add structured fields that oracle cache now stores
            includes=[
                {'filename': 'test.h', 'line_num': 0, 'is_commented': False},
                {'filename': 'another.h', 'line_num': 1, 'is_commented': False}
            ],
            magic_flags=[
                {'flag': 'TEST_FLAG', 'line_num': 5, 'is_commented': False},
                {'flag': 'ANOTHER_FLAG', 'line_num': 15, 'is_commented': False}
            ],
            defines=[
                {'name': 'TEST_MACRO', 'value': '1', 'line_num': 25, 'is_commented': False}
            ],
            system_headers={'<iostream>', '<vector>'},
            quoted_headers={'"local.h"', '"config.h"'},
            content_hash="test_content_hash_1234567890abcdef"
        )
    
    def test_cache_creation_and_initialization(self):
        """Test that cache can be created and properly initialized."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        assert cache.cache_path.exists()
        assert cache.mmap_data is not None
        assert cache.fd is not None
        
        # Verify cache file structure
        assert cache.cache_path.stat().st_size == cache.cache_size
        
        # Test stats
        stats = cache.get_stats()
        assert stats['entries'] == 0
        assert stats['data_used'] == 0
        assert stats['version'] == int(CACHE_FORMAT_VERSION[:8], 16)
        
        cache.close()
    
    def test_basic_put_get_cycle(self):
        """Test basic put/get operations."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        # Create test data
        result = self.create_test_result("hello world\nline 2")
        filepath = "test.cpp"
        content_hash = "abcd1234567890abcdef1234567890abcdef1234"
        
        # Test cache miss
        assert cache.get(filepath, content_hash) is None
        
        # Store in cache
        cache.put(filepath, content_hash, result)
        
        # Clear the in-memory cache to force reading from mmap
        cache._result_cache.clear()
        
        # Test cache hit
        cached_result = cache.get(filepath, content_hash)
        assert cached_result is not None
        assert cached_result.include_positions == result.include_positions
        assert cached_result.magic_positions == result.magic_positions
        assert cached_result.directive_positions == result.directive_positions
        assert cached_result.line_byte_offsets == result.line_byte_offsets
        assert cached_result.bytes_analyzed == result.bytes_analyzed
        assert cached_result.was_truncated == result.was_truncated
        
        # Oracle cache now stores structured fields
        assert cached_result.includes == result.includes
        assert cached_result.magic_flags == result.magic_flags
        assert cached_result.defines == result.defines
        # Note: system_headers, quoted_headers, content_hash are not yet stored in oracle cache
        assert cached_result.system_headers == set()
        assert cached_result.quoted_headers == set()
        assert cached_result.content_hash == ""
        
        # Verify stats updated
        stats = cache.get_stats()
        assert stats['entries'] == 1
        assert stats['data_used'] > 0
        
        cache.close()
    
    def test_multiple_entries(self):
        """Test storing and retrieving multiple cache entries."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        # Store multiple entries
        entries = []
        for i in range(10):
            result = self.create_test_result(f"content {i}", [i*10, i*10+5])
            filepath = f"test{i}.cpp"
            content_hash = generate_test_hash(f"multiple_entries_{i}")
            
            cache.put(filepath, content_hash, result)
            entries.append((filepath, content_hash, result))
        
        # Verify all entries can be retrieved
        for filepath, content_hash, original_result in entries:
            cached_result = cache.get(filepath, content_hash)
            assert cached_result is not None
            assert cached_result.include_positions == original_result.include_positions
            assert cached_result.magic_positions == original_result.magic_positions
            assert cached_result.directive_positions == original_result.directive_positions
        
        # Verify stats
        stats = cache.get_stats()
        assert stats['entries'] == 10
        
        cache.close()
    
    def test_hash_collision_handling(self):
        """Test that hash collisions are handled correctly with linear probing."""
        cache = MMapOracleCache(cache_dir=self.temp_dir, cache_size=4*1024*1024)  # 4MB cache for testing
        
        # Create entries that will collide in hash table
        # Use hashes that will map to same bucket
        base_hash = "1234567890abcdef1234567890abcdef12345678"
        
        entries = []
        for i in range(5):
            # Modify last character to create different but colliding hashes
            content_hash = base_hash[:-1] + str(i)
            result = self.create_test_result(f"collision test {i}")
            filepath = f"collision{i}.cpp"
            
            cache.put(filepath, content_hash, result)
            entries.append((filepath, content_hash, result))
        
        # Verify all entries can be retrieved despite collisions
        for filepath, content_hash, original_result in entries:
            cached_result = cache.get(filepath, content_hash)
            assert cached_result is not None
            assert cached_result.include_positions == original_result.include_positions
        
        cache.close()
    
    def test_write_once_semantics(self):
        """Test that entries are not overwritten (write-once semantics)."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        filepath = "test.cpp"
        content_hash = "abcd1234567890abcdef1234567890abcdef1234"
        
        # Store first entry
        result1 = self.create_test_result("first content", [100])
        cache.put(filepath, content_hash, result1)
        
        # Try to store different entry with same key
        result2 = self.create_test_result("second content", [200])
        cache.put(filepath, content_hash, result2)
        
        # Should still get first entry
        cached_result = cache.get(filepath, content_hash)
        assert cached_result.include_positions == [100]  # First entry, not second
        
        cache.close()
    
    def test_empty_and_edge_case_data(self):
        """Test handling of empty and edge case data."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        # Test empty result
        empty_result = FileAnalysisResult(
            lines=[],
            line_byte_offsets=[],
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[],
            directive_by_line={},
            bytes_analyzed=0,
            was_truncated=False,
            includes=[],
            magic_flags=[],
            defines=[],
            system_headers=set(),
            quoted_headers=set(),
            content_hash=""
        )
        
        empty_hash = generate_test_hash("empty")
        cache.put("empty.cpp", empty_hash, empty_result)
        
        cached_empty = cache.get("empty.cpp", empty_hash)
        assert cached_empty is not None
        assert cached_empty.include_positions == []
        assert cached_empty.magic_positions == []
        assert cached_empty.directive_positions == {}
        # Verify empty structured fields
        assert cached_empty.includes == []
        assert cached_empty.magic_flags == []
        assert cached_empty.defines == []
        assert cached_empty.system_headers == set()
        assert cached_empty.quoted_headers == set()
        assert cached_empty.content_hash == ""
        
        # Test large position arrays
        large_positions = list(range(1000))
        large_result = self.create_test_result("large", large_positions)
        large_hash = generate_test_hash("large")
        cache.put("large.cpp", large_hash, large_result)
        
        cached_large = cache.get("large.cpp", large_hash)
        assert cached_large is not None
        assert cached_large.include_positions == large_positions
        
        cache.close()
    
    def test_cache_persistence_across_instances(self):
        """Test that cache persists across different instance creations."""
        filepath = "persist.cpp"
        content_hash = generate_test_hash("persistent_content")
        result = self.create_test_result("persistent content")
        
        # Store with first instance
        cache1 = MMapOracleCache(cache_dir=self.temp_dir)
        cache1.put(filepath, content_hash, result)
        cache1.close()
        
        # Retrieve with second instance
        cache2 = MMapOracleCache(cache_dir=self.temp_dir)
        cached_result = cache2.get(filepath, content_hash)
        
        assert cached_result is not None
        assert cached_result.include_positions == result.include_positions
        assert cached_result.bytes_analyzed == result.bytes_analyzed
        
        cache2.close()
    
    def test_clear_cache(self):
        """Test cache clearing functionality."""
        cache = MMapOracleCache(cache_dir=self.temp_dir)
        
        # Add some entries
        for i in range(3):
            result = self.create_test_result(f"clear test {i}")
            content_hash = generate_test_hash(f"clear_test_{i}")
            cache.put(f"clear{i}.cpp", content_hash, result)
        
        # Verify entries exist
        stats_before = cache.get_stats()
        assert stats_before['entries'] == 3
        
        # Clear cache
        cache.clear()
        
        # Verify cache is empty
        stats_after = cache.get_stats()
        assert stats_after['entries'] == 0
        
        # Verify entries are gone
        for i in range(3):
            content_hash = generate_test_hash(f"clear_test_{i}")
            cached_result = cache.get(f"clear{i}.cpp", content_hash)
            assert cached_result is None
        
        cache.close()


class TestMMapOracleCacheIntegration:
    """Test oracle cache integration with file analyzer cache system."""
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to simple existing C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_factory_function_creates_oracle_cache(self):
        """Test that factory function correctly creates oracle cache."""
        from compiletools.file_analyzer_cache import create_cache
        
        # Test both 'mmap' and 'oracle' aliases
        for cache_type in ['mmap', 'oracle']:
            with tempfile.TemporaryDirectory() as temp_dir:
                cache = create_cache(cache_type, cache_dir=temp_dir)
                assert isinstance(cache, MMapOracleCache)
                cache.close()
    
    def test_oracle_cache_with_file_analyzer(self, simple_cpp_file):
        """Test oracle cache integration with file analyzer."""
        from compiletools.file_analyzer import create_file_analyzer
        from compiletools.file_analyzer_cache import create_cache
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create cache and analyzer separately
            cache = create_cache('oracle', cache_dir=temp_dir)
            analyzer = create_file_analyzer(simple_cpp_file, cache=cache)
            
            # Analyze file (should be cache miss)
            result1 = analyzer.analyze()
            assert result1 is not None
            assert result1.bytes_analyzed > 0
            
            # Analyze again (should be cache hit)
            result2 = analyzer.analyze()
            assert result2 is not None
            
            # Results should be identical
            assert result1.include_positions == result2.include_positions
            assert result1.magic_positions == result2.magic_positions
            assert result1.directive_positions == result2.directive_positions
            assert result1.bytes_analyzed == result2.bytes_analyzed
    
    def test_cache_with_different_content_hashes(self, simple_cpp_file):
        """Test that different content hashes for same file work correctly."""
        from compiletools.file_analyzer import create_file_analyzer
        from compiletools.file_analyzer_cache import create_cache
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Mock different content hashes to simulate file changes
            with patch('compiletools.global_hash_registry.get_file_hash') as mock_hash:
                # Create cache once
                cache = create_cache('oracle', cache_dir=temp_dir)
                
                # First analysis with hash 'abc123'
                mock_hash.return_value = generate_test_hash('first_content')
                analyzer1 = create_file_analyzer(simple_cpp_file, cache=cache)
                result1 = analyzer1.analyze()
                
                # Second analysis with different hash 'def456' (simulating file change)
                mock_hash.return_value = generate_test_hash('second_content')
                analyzer2 = create_file_analyzer(simple_cpp_file, cache=cache)
                result2 = analyzer2.analyze()
                
                # Both should succeed (different cache keys due to different hashes)
                assert result1 is not None
                assert result2 is not None
                # Both results should have same file structure (same actual file) but cache treats them separately
                assert result1.bytes_analyzed == result2.bytes_analyzed
                # Note: Oracle cache stores lines=[] so we can't compare text directly,
                # but we can verify the structured analysis results are consistent


class TestMMapOracleCachePerformance:
    """Test oracle cache performance characteristics."""
    
    def test_performance_with_many_entries(self):
        """Test performance with many cache entries."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = MMapOracleCache(cache_dir=temp_dir)
            
            # Store many entries
            num_entries = 1000
            start_time = time.perf_counter()
            
            for i in range(num_entries):
                result = FileAnalysisResult(
                    lines=[f"content {i}"],
                    line_byte_offsets=[0],
                    include_positions=[i],
                    magic_positions=[i+1000],
                    directive_positions={"include": [i], "define": [i+2000]},
                    directives=[],
                    directive_by_line={},
                    bytes_analyzed=20,
                    was_truncated=False,
                    includes=[{'filename': f'test{i}.h', 'line_num': 0, 'is_commented': False}],
                    magic_flags=[{'flag': f'FLAG{i}', 'line_num': 1, 'is_commented': False}],
                    defines=[{'name': f'MACRO{i}', 'value': str(i), 'line_num': 2, 'is_commented': False}],
                    system_headers=set(),
                    quoted_headers=set(),
                    content_hash=f"hash_{i}"
                )
                content_hash = generate_test_hash(f"perf_{i}")
                cache.put(f"perf{i}.cpp", content_hash, result)
            
            write_time = time.perf_counter() - start_time
            
            # Read all entries
            start_time = time.perf_counter()
            
            for i in range(num_entries):
                content_hash = generate_test_hash(f"perf_{i}")
                cached_result = cache.get(f"perf{i}.cpp", content_hash)
                assert cached_result is not None
                assert cached_result.include_positions == [i]
            
            read_time = time.perf_counter() - start_time
            
            # Performance assertions (these are loose bounds)
            assert write_time < 2.0, f"Write time too slow: {write_time:.3f}s for {num_entries} entries"
            assert read_time < 0.5, f"Read time too slow: {read_time:.3f}s for {num_entries} entries"
            
            # Verify final stats
            stats = cache.get_stats()
            assert stats['entries'] == num_entries
            assert stats['utilization'] > 0
            
            cache.close()
    
    def test_memory_usage_efficiency(self):
        """Test that cache memory usage is efficient."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = MMapOracleCache(cache_dir=temp_dir, cache_size=1024*1024)  # 1MB cache
            
            # Create entry with known size
            large_positions = list(range(100))  # 100 positions = 400 bytes
            result = FileAnalysisResult(
                lines=["test"],
                line_byte_offsets=[0],
                include_positions=large_positions,
                magic_positions=large_positions,
                directive_positions={"include": large_positions, "define": large_positions},
                directives=[],
                directive_by_line={},
                bytes_analyzed=100,
                was_truncated=False,
                includes=[{'filename': f'large{i}.h', 'line_num': i, 'is_commented': False} for i in range(10)],
                magic_flags=[{'flag': f'LARGE_FLAG{i}', 'line_num': i, 'is_commented': False} for i in range(10)],
                defines=[{'name': f'LARGE_MACRO{i}', 'value': str(i), 'line_num': i, 'is_commented': False} for i in range(10)],
                system_headers={'<large_header.h>'},
                quoted_headers={'"large_local.h"'},
                content_hash="large_test_hash"
            )
            
            cache.put("memory_test.cpp", "abcdef1234567890abcdef1234567890abcdef12", result)
            
            stats = cache.get_stats()
            
            # Memory usage should be reasonable (< 4KB for this entry with structured data)
            assert stats['data_used'] < 4096, f"Memory usage too high: {stats['data_used']} bytes"
            
            # Should be able to store many entries in 1MB
            assert stats['utilization'] < 0.01, f"Cache utilization too high for single entry: {stats['utilization']:.3f}"
            
            cache.close()


class TestMMapOracleCacheErrorHandling:
    """Test oracle cache error handling and edge cases."""
    
    def test_corrupted_cache_file_handling(self):
        """Test handling of corrupted cache files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / f'ct-oracle-v{CACHE_FORMAT_VERSION}.mmap'
            
            # Create corrupted cache file
            with open(cache_path, 'wb') as f:
                f.write(b"corrupted data" * 100)
            
            # Should raise error when trying to open corrupted file
            with pytest.raises(ValueError, match="Invalid cache file magic"):
                cache = MMapOracleCache(cache_dir=temp_dir)  # noqa: F841
    
    def test_version_mismatch_handling(self):
        """Test handling of cache files with wrong version."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create cache with current version
            cache1 = MMapOracleCache(cache_dir=temp_dir)
            cache1.close()
            
            # Manually corrupt the version in the cache file to simulate version mismatch
            cache_path = cache1.cache_path
            with open(cache_path, 'r+b') as f:
                # Read header
                f.seek(0)
                header_data = bytearray(f.read(cache1.HEADER_SIZE))
                
                # Corrupt the version field (at offset 8, 4 bytes)
                import struct
                struct.pack_into('=I', header_data, 8, 0xDEADBEEF)  # Invalid version
                
                # Write back corrupted header
                f.seek(0)
                f.write(header_data)
            
            # Should raise version mismatch error
            with pytest.raises(ValueError, match="Cache version mismatch"):
                cache2 = MMapOracleCache(cache_dir=temp_dir)  # noqa: F841
    
    def test_insufficient_space_handling(self):
        """Test handling when cache runs out of space."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create small cache that will fill up quickly
            cache = MMapOracleCache(cache_dir=temp_dir, cache_size=32768)  # 32KB - small but enough for a few entries
            
            # Fill cache with large entries until it's full
            large_positions = list(range(1000))  # Large position array
            
            entries_stored = 0
            for i in range(100):  # Try to store many entries
                result = FileAnalysisResult(
                    lines=[f"large content {i}"],
                    line_byte_offsets=[0],
                    include_positions=large_positions,
                    magic_positions=large_positions,
                    directive_positions={"include": large_positions},
                    directives=[],
                    directive_by_line={},
                    bytes_analyzed=100,
                    was_truncated=False,
                    includes=[{'filename': f'large{j}.h', 'line_num': j, 'is_commented': False} for j in range(50)],
                    magic_flags=[{'flag': f'LARGE_FLAG{j}', 'line_num': j, 'is_commented': False} for j in range(50)],
                    defines=[{'name': f'LARGE_MACRO{j}', 'value': str(j), 'line_num': j, 'is_commented': False} for j in range(50)],
                    system_headers=set([f'<large{j}.h>' for j in range(10)]),
                    quoted_headers=set([f'"large{j}.h"' for j in range(10)]),
                    content_hash=f"large_hash_{i}"
                )
                
                content_hash = generate_test_hash(f"insufficient_space_{i}")
                cache.put(f"large{i}.cpp", content_hash, result)
                
                # Check if entry was actually stored
                if cache.get(f"large{i}.cpp", content_hash) is not None:
                    entries_stored += 1
                else:
                    break  # Cache full
            
            # Should have stored at least one entry before running out of space
            assert entries_stored > 0
            # Should not have stored all 100 entries (cache should be full)
            assert entries_stored < 100
            
            cache.close()
    
    def test_invalid_hash_formats(self):
        """Test handling of invalid hash formats."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = MMapOracleCache(cache_dir=temp_dir)
            
            result = FileAnalysisResult(
                lines=["test"],
                line_byte_offsets=[0],
                include_positions=[0],
                magic_positions=[],
                directive_positions={},
                directives=[],
                directive_by_line={},
                bytes_analyzed=4,
                was_truncated=False,
                includes=[],
                magic_flags=[],
                defines=[],
                system_headers=set(),
                quoted_headers=set(),
                content_hash="test_hash"
            )
            
            # Test with invalid hash formats
            invalid_hashes = [
                "short",  # Too short
                "not_hex_1234567890abcdef1234567890abcdef",  # Invalid hex
                "",  # Empty
                "z" * 40,  # Invalid hex characters
            ]
            
            for invalid_hash in invalid_hashes:
                # Should not crash, but may not work as expected
                try:
                    cache.put("test.cpp", invalid_hash, result)
                    cached = cache.get("test.cpp", invalid_hash)  # noqa: F841
                    # Either works or returns None - should not crash
                except ValueError:
                    # Expected for some invalid formats
                    pass
            
            cache.close()


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])