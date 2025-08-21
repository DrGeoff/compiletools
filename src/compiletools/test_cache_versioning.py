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
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to existing simple C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_versioned_serialization_format(self, simple_cpp_file):
        """Test that serialized data includes version information."""
        from compiletools.file_analyzer import create_file_analyzer
        
        # Create a result to serialize
        analyzer = create_file_analyzer(simple_cpp_file)
        result = analyzer.analyze()
        
        # Test serialization with version info
        cache = MemoryCache()
        serialized_data = cache._serialize_result(result)
        
        # Should be pickle data that includes version
        unpickled = pickle.loads(serialized_data)
        assert isinstance(unpickled, dict)
        assert "version" in unpickled
        assert "data" in unpickled
        assert unpickled["version"] == CACHE_FORMAT_VERSION
        
        # Data should be the result as dict
        assert unpickled["data"] == {
            "text": result.text,
            "include_positions": result.include_positions,
            "magic_positions": result.magic_positions,
            "directive_positions": result.directive_positions,
            "bytes_analyzed": result.bytes_analyzed,
            "was_truncated": result.was_truncated
        }
    
    def test_versioned_deserialization_succeeds(self, simple_cpp_file):
        """Test that current version data deserializes correctly."""
        from compiletools.file_analyzer import create_file_analyzer
        
        analyzer = create_file_analyzer(simple_cpp_file)
        original_result = analyzer.analyze()
        
        cache = MemoryCache()
        serialized_data = cache._serialize_result(original_result)
        deserialized_result = cache._deserialize_result(serialized_data)
        
        # Should successfully deserialize
        assert deserialized_result is not None
        assert deserialized_result.text == original_result.text
        assert deserialized_result.include_positions == original_result.include_positions
        assert deserialized_result.magic_positions == original_result.magic_positions
        assert deserialized_result.directive_positions == original_result.directive_positions
        assert deserialized_result.bytes_analyzed == original_result.bytes_analyzed
        assert deserialized_result.was_truncated == original_result.was_truncated
    
    def test_incompatible_version_returns_none(self):
        """Test that incompatible version data returns None."""
        cache = MemoryCache()
        
        # Create fake versioned data with wrong version
        fake_data = {
            "version": "999.999",  # Incompatible version
            "data": {
                "text": "test",
                "include_positions": [1, 2],
                "magic_positions": [],
                "directive_positions": {},
                "bytes_analyzed": 4,
                "was_truncated": False
            }
        }
        serialized_fake = pickle.dumps(fake_data)
        
        # Should return None for incompatible version
        result = cache._deserialize_result(serialized_fake)
        assert result is None
    
    def test_legacy_format_compatibility(self):
        """Test that legacy format (direct dict) is handled gracefully."""
        cache = MemoryCache()
        
        # Create legacy format data (direct FileAnalysisResult dict)
        legacy_data = {
            "text": "legacy test",
            "include_positions": [5, 10],
            "magic_positions": [15],
            "directive_positions": {"include": [5, 10]},
            "bytes_analyzed": 11,
            "was_truncated": False
        }
        serialized_legacy = pickle.dumps(legacy_data)
        
        # Should handle legacy format
        result = cache._deserialize_result(serialized_legacy)
        assert result is not None
        assert result.text == "legacy test"
        assert result.include_positions == [5, 10]
        assert result.magic_positions == [15]
    
    def test_corrupted_data_returns_none(self):
        """Test that corrupted data returns None."""
        cache = MemoryCache()
        
        # Test various corrupted data scenarios
        test_cases = [
            b"not pickle data",
            pickle.dumps("not a dict"),
            pickle.dumps({"version": "1.0"}),  # Missing data
            pickle.dumps({"data": "not a dict"}),  # Missing version
            pickle.dumps({"version": "1.0", "data": "not a dict"}),  # Invalid data type
        ]
        
        for corrupted_data in test_cases:
            result = cache._deserialize_result(corrupted_data)
            assert result is None, f"Should return None for corrupted data: {corrupted_data}"
    
    def test_missing_required_fields_returns_none(self):
        """Test that data missing required fields returns None."""
        cache = MemoryCache()
        
        # Test missing required fields
        incomplete_data = {
            "version": CACHE_FORMAT_VERSION,
            "data": {
                "text": "test",
                "include_positions": [1, 2]
                # Missing: magic_positions, directive_positions, bytes_analyzed, was_truncated
            }
        }
        serialized_incomplete = pickle.dumps(incomplete_data)
        
        result = cache._deserialize_result(serialized_incomplete)
        assert result is None
    
    def test_wrong_field_types_returns_none(self):
        """Test that wrong field types return None."""
        cache = MemoryCache()
        
        # Test wrong field types
        wrong_types_data = {
            "version": CACHE_FORMAT_VERSION,
            "data": {
                "text": 123,  # Should be str
                "include_positions": "not a list",  # Should be list
                "magic_positions": [],
                "directive_positions": {},
                "bytes_analyzed": 4,
                "was_truncated": False
            }
        }
        serialized_wrong = pickle.dumps(wrong_types_data)
        
        result = cache._deserialize_result(serialized_wrong)
        assert result is None


class TestCacheCompatibilityIntegration:
    """Test cache compatibility in real cache implementations."""
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to existing simple C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_disk_cache_version_compatibility(self, simple_cpp_file):
        """Test that disk cache handles version mismatches correctly."""
        from compiletools.file_analyzer import create_file_analyzer
        
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = DiskCache(cache_dir=temp_dir)
            
            # Store current version data
            analyzer = create_file_analyzer(simple_cpp_file)
            result = analyzer.analyze()
            cache.put(simple_cpp_file, "test_hash", result)
            
            # Should retrieve successfully
            retrieved = cache.get(simple_cpp_file, "test_hash")
            assert retrieved is not None
            assert retrieved.text == result.text
            
            # Manually create incompatible version file
            cache_path = cache._get_cache_path(simple_cpp_file, "test_hash2")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            
            incompatible_data = {
                "version": "999.999",
                "data": {"invalid": "structure"}
            }
            with open(cache_path, 'wb') as f:
                pickle.dump(incompatible_data, f)
            
            # Should return None for incompatible version
            retrieved = cache.get(simple_cpp_file, "test_hash2")
            assert retrieved is None
            
            # Cache file should be removed
            assert not cache_path.exists()
    
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
        assert retrieved.text == result.text
        
        # Manually inject incompatible data into cache
        cache._cache[cache._make_key(simple_cpp_file, "bad_hash")] = result
        
        # This simulates what would happen if the version format changed
        # and we had old cached objects in memory - they should still work
        # since they're actual FileAnalysisResult objects, not serialized data
        retrieved = cache.get(simple_cpp_file, "bad_hash")
        assert retrieved is not None