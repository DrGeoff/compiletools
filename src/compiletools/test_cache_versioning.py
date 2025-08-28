"""Tests for cache versioning and compatibility handling."""

import pickle
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import List
import pytest

from compiletools.file_analyzer import FileAnalysisResult
from compiletools.file_analyzer_cache import (
    _compute_dataclass_hash, CACHE_FORMAT_VERSION, DiskCache, MemoryCache
)
from compiletools.testhelper import samplesdir


def get_text_from_result(result):
    """Helper function to reconstruct text from FileAnalysisResult lines for testing."""
    return '\n'.join(result.lines)


class TestCacheVersioning:
    """Test automatic cache versioning based on dataclass structure."""
    
    def test_dataclass_hash_computation(self):
        """Test that dataclass hash is computed correctly."""
        # Should compute a hash for FileAnalysisResult
        hash_value = _compute_dataclass_hash(FileAnalysisResult)
        
        # Should be a 12-character hex string
        assert isinstance(hash_value, str)
        assert len(hash_value) == 12
        assert all(c in '0123456789abcdef' for c in hash_value.lower())
        
        # Should be deterministic
        hash_value2 = _compute_dataclass_hash(FileAnalysisResult)
        assert hash_value == hash_value2
    
    def test_dataclass_hash_changes_with_structure(self):
        """Test that hash changes when dataclass structure changes."""
        # Create a different dataclass to compare
        @dataclass
        class TestResult:
            text: str
            positions: List[int]
            was_truncated: bool
        
        hash1 = _compute_dataclass_hash(FileAnalysisResult)
        hash2 = _compute_dataclass_hash(TestResult)
        
        # Should have different hashes
        assert hash1 != hash2
    
    def test_cache_format_version_is_computed(self):
        """Test that cache format version is automatically computed."""
        # Should be a valid hash string
        assert isinstance(CACHE_FORMAT_VERSION, str)
        assert len(CACHE_FORMAT_VERSION) == 12
        
        # Should match what we compute directly
        expected = _compute_dataclass_hash(FileAnalysisResult)
        assert CACHE_FORMAT_VERSION == expected
    
    def test_version_in_cache_paths(self):
        """Test that version is embedded in cache directory paths."""
        cache = DiskCache()
        
        # Cache directory should contain the version
        cache_dir_name = cache._cache_dir.name
        assert f"_v{CACHE_FORMAT_VERSION}" in cache_dir_name
        
        # Test different cache types
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            # Even with explicit cache_dir, the path should be versioned
            cache_path = cache._get_cache_path("test.cpp", "hash123")
            assert cache_path.exists() or cache_path.parent.parent == Path(temp_dir)
    
    def test_direct_pickle_serialization(self):
        """Test that cache now uses direct pickle serialization."""
        
        # Create test result
        result = FileAnalysisResult(
            lines=["test content"],
            line_byte_offsets=[0],
            include_positions=[1, 2, 3],
            magic_positions=[4, 5],
            directive_positions={"test": [6, 7]},
            directives=[],
            directive_by_line={},
            bytes_analyzed=100,
            was_truncated=False
        )
        
        # Test that DiskCache stores direct pickle data
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            cache.put("test.cpp", "hash123", result)
            
            # Read the cache file directly and verify it's a direct pickle
            cache_path = cache._get_cache_path("test.cpp", "hash123")
            with cache_path.open('rb') as f:
                cached_data = pickle.loads(f.read())
                
            # Should be a FileAnalysisResult directly, not a wrapper dict
            assert isinstance(cached_data, FileAnalysisResult)
            assert get_text_from_result(cached_data) == get_text_from_result(result)
            assert cached_data.include_positions == result.include_positions


class TestCacheCompatibilityIntegration:
    """Test cache compatibility in real cache implementations."""
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to existing simple C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_disk_cache_version_isolation(self, simple_cpp_file):
        """Test that different cache versions are isolated by directory paths."""
        from compiletools.file_analyzer import create_file_analyzer
        
        # Test default cache directory has version
        cache = DiskCache()
        cache_dir_name = cache._cache_dir.name
        assert f"_v{CACHE_FORMAT_VERSION}" in cache_dir_name
        
        # Test functionality with explicit cache dir
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            
            # Store current version data
            analyzer = create_file_analyzer(simple_cpp_file)
            result = analyzer.analyze()
            cache.put(simple_cpp_file, "test_hash", result)
            
            # Should retrieve successfully
            retrieved = cache.get(simple_cpp_file, "test_hash")
            assert retrieved is not None
            assert get_text_from_result(retrieved) == get_text_from_result(result)
            
            # Create a fake "old version" directory in the temp dir
            old_cache_dir = Path(temp_dir) / "old_version_data"
            old_cache_dir.mkdir(parents=True, exist_ok=True)
            
            # Should still work with current cache
            retrieved2 = cache.get(simple_cpp_file, "test_hash") 
            assert retrieved2 is not None
    
    def test_corrupted_pickle_handling(self, simple_cpp_file):
        """Test that corrupted pickle files are handled gracefully."""
        
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            
            # Create a corrupted cache file
            cache_path = cache._get_cache_path(simple_cpp_file, "corrupted_hash")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write corrupted data
            with cache_path.open('wb') as f:
                f.write(b"corrupted pickle data")
            
            # Should return None and clean up corrupted file
            retrieved = cache.get(simple_cpp_file, "corrupted_hash")
            assert retrieved is None
            assert not cache_path.exists()
    
    def test_old_version_cleanup(self):
        """Test cleanup of old version cache directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            
            # Create fake old version directories
            old_dirs = [
                Path(temp_dir).parent / "file_analyzer_cache_shared_v999999",
                Path(temp_dir).parent / "file_analyzer_cache_shared_v888888",
                Path(temp_dir).parent / "file_analyzer_cache_v777777"
            ]
            
            for old_dir in old_dirs:
                old_dir.mkdir(parents=True, exist_ok=True)
                (old_dir / "dummy_file.txt").write_text("old cache data")
            
            # Cleanup should remove old version directories
            cache.cleanup_old_versions()
            
            # Old directories should be gone
            for old_dir in old_dirs:
                assert not old_dir.exists()
            
            # Current version directory should still exist
            assert cache._cache_dir.exists()
    
    def test_memory_cache_graceful_degradation(self, simple_cpp_file):
        """Test that memory cache degrades gracefully with version issues."""
        from compiletools.file_analyzer import create_file_analyzer
        
        cache = MemoryCache()
        
        # Store and retrieve current version - should work
        analyzer = create_file_analyzer(simple_cpp_file)
        result = analyzer.analyze()
        cache.put(simple_cpp_file, "test_hash", result)
        
        retrieved = cache.get(simple_cpp_file, "test_hash")
        assert retrieved is not None
        assert get_text_from_result(retrieved) == get_text_from_result(result)
        
        # Manually inject incompatible data into cache
        cache._cache[cache._make_key(simple_cpp_file, "bad_hash")] = result
        
        # This simulates what would happen if the version format changed
        # and we had old cached objects in memory - they should still work
        # since they're actual FileAnalysisResult objects, not serialized data
        retrieved = cache.get(simple_cpp_file, "bad_hash")
        assert retrieved is not None