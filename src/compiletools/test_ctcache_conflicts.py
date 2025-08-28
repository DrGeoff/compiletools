"""Tests for CTCACHE and CTCACHE_TYPE configuration conflicts."""

import os
import tempfile
from pathlib import Path
import pytest

from compiletools.file_analyzer import create_file_analyzer
from compiletools.file_analyzer_cache import DiskCache, SQLiteCache
from compiletools.testhelper import samplesdir


def get_text_from_result(result):
    """Helper function to reconstruct text from FileAnalysisResult lines for testing."""
    return '\n'.join(result.lines)


class TestCTCacheConfigurationConflicts:
    """Test handling of conflicting CTCACHE and CTCACHE_TYPE settings."""
    
    @pytest.fixture(autouse=True)
    def preserve_environment(self):
        """Preserve and restore CTCACHE environment variables."""
        original_ctcache = os.environ.get('CTCACHE')
        original_ctcache_type = os.environ.get('CTCACHE_TYPE')
        
        yield
        
        # Restore original values
        if original_ctcache is not None:
            os.environ['CTCACHE'] = original_ctcache
        elif 'CTCACHE' in os.environ:
            del os.environ['CTCACHE']
            
        if original_ctcache_type is not None:
            os.environ['CTCACHE_TYPE'] = original_ctcache_type
        elif 'CTCACHE_TYPE' in os.environ:
            del os.environ['CTCACHE_TYPE']
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to existing simple C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    def test_disk_cache_with_ctcache_disabled(self, simple_cpp_file):
        """Test that CTCACHE_TYPE=disk works when CTCACHE=None by using temp directory."""
        # Set conflicting configuration
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = 'disk'
        
        # Create disk cache - should fall back to temp directory
        cache = DiskCache()
        
        # Verify cache directory is in temp area (not "None")
        temp_dir = Path(tempfile.gettempdir())
        assert cache._cache_dir.parent == temp_dir
        # Should have unique name with shared prefix and version
        assert cache._cache_dir.name.startswith("file_analyzer_cache_shared_v")
        assert "None" not in str(cache._cache_dir)
        
        # Verify cache operations work
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='disk')
        result = analyzer.analyze()
        
        assert result is not None
        assert "#include" in get_text_from_result(result)
        assert len(result.include_positions) > 0
    
    def test_sqlite_cache_with_ctcache_disabled(self, simple_cpp_file):
        """Test that CTCACHE_TYPE=sqlite works when CTCACHE=None by using temp directory."""
        # Set conflicting configuration
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = 'sqlite'
        
        # Create SQLite cache - should fall back to temp directory
        cache = SQLiteCache()
        
        # Verify database path is in temp area (not "None")
        temp_dir = Path(tempfile.gettempdir())
        assert cache._db_path.parent == temp_dir
        # Should have shared prefix with version and unique ID to avoid parallel test conflicts
        assert cache._db_path.name.startswith("file_analyzer_cache_shared_v")
        assert cache._db_path.name.endswith(".db")
        
        # Verify cache operations work
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='sqlite')
        result = analyzer.analyze()
        
        assert result is not None
        assert "#include" in get_text_from_result(result)
        assert len(result.include_positions) > 0
    
    def test_memory_cache_unaffected_by_ctcache_disabled(self, simple_cpp_file):
        """Test that CTCACHE_TYPE=memory works regardless of CTCACHE setting."""
        # Set CTCACHE disabled but CTCACHE_TYPE to memory
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = 'memory'
        
        # Memory cache should work normally (doesn't depend on disk paths)
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='memory')
        result = analyzer.analyze()
        
        assert result is not None
        assert "#include" in get_text_from_result(result)
        assert len(result.include_positions) > 0
    
    def test_null_cache_with_any_ctcache_setting(self, simple_cpp_file):
        """Test that CTCACHE_TYPE=null works regardless of CTCACHE setting."""
        # Test with CTCACHE disabled
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = 'null'
        
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='null')
        result = analyzer.analyze()
        
        assert result is not None
        assert "#include" in get_text_from_result(result)
        assert len(result.include_positions) > 0
    
    @pytest.mark.parametrize("cache_type", ['null', 'memory', 'disk', 'sqlite'])
    def test_cache_types_work_with_ctcache_disabled(self, simple_cpp_file, cache_type):
        """Test that all cache types handle CTCACHE=None gracefully."""
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = cache_type
        
        # Should work without errors for all cache types
        analyzer = create_file_analyzer(simple_cpp_file, cache_type=cache_type)
        result = analyzer.analyze()
        
        assert result is not None
        assert "#include" in get_text_from_result(result)
        assert len(result.include_positions) > 0
    
    def test_cache_fallback_hierarchy_with_conflicts(self, simple_cpp_file):
        """Test complete fallback hierarchy when CTCACHE and CTCACHE_TYPE conflict."""
        # Both disabled - should work with no caching
        os.environ['CTCACHE'] = 'None'
        os.environ['CTCACHE_TYPE'] = 'None'
        
        analyzer = create_file_analyzer(simple_cpp_file)  # No cache_type specified
        result = analyzer.analyze()
        assert result is not None
        
        # CTCACHE disabled, but CTCACHE_TYPE=memory - should work
        os.environ['CTCACHE_TYPE'] = 'memory'
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='memory')
        result = analyzer.analyze()
        assert result is not None
        
        # CTCACHE disabled, but CTCACHE_TYPE=disk - should work with temp fallback
        os.environ['CTCACHE_TYPE'] = 'disk'
        analyzer = create_file_analyzer(simple_cpp_file, cache_type='disk')
        result = analyzer.analyze()
        assert result is not None


class TestCachePathResolution:
    """Test cache path resolution when CTCACHE is set to None."""
    
    def test_disk_cache_path_resolution_with_none_ctcache(self):
        """Test that disk cache resolves to temp directory when CTCACHE is None."""
        original_ctcache = os.environ.get('CTCACHE')
        
        try:
            os.environ['CTCACHE'] = 'None'
            cache = DiskCache()
            
            # Should resolve to temp directory, not literal "None" directory
            temp_dir = Path(tempfile.gettempdir())
            assert cache._cache_dir.parent == temp_dir
            assert "None" not in str(cache._cache_dir)
            
        finally:
            if original_ctcache is not None:
                os.environ['CTCACHE'] = original_ctcache
            elif 'CTCACHE' in os.environ:
                del os.environ['CTCACHE']
    
    def test_sqlite_cache_path_resolution_with_none_ctcache(self):
        """Test that SQLite cache resolves to temp directory when CTCACHE is None."""
        original_ctcache = os.environ.get('CTCACHE')
        
        try:
            os.environ['CTCACHE'] = 'None'
            cache = SQLiteCache()
            
            # Should resolve to temp directory, not literal "None" directory
            temp_dir = Path(tempfile.gettempdir())
            assert cache._db_path.parent == temp_dir
            assert "None" not in str(cache._db_path)
            
        finally:
            if original_ctcache is not None:
                os.environ['CTCACHE'] = original_ctcache
            elif 'CTCACHE' in os.environ:
                del os.environ['CTCACHE']