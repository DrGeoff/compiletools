"""Tests for file_analyzer module."""

import os
import tempfile
from types import SimpleNamespace

from compiletools.file_analyzer import (
    FileAnalysisResult,
    FileAnalyzer,
    read_file_mmap,
    read_file_traditional
)


class TestFileAnalysisResult:
    """Test FileAnalysisResult dataclass."""
    
    def test_dataclass_creation(self):
        result = FileAnalysisResult(
            line_count=2,
            line_byte_offsets=[0, 5],
            include_positions=[10, 20],
            magic_positions=[5],
            directive_positions={"include": [10, 20], "define": [30]},
            directives=[],
            directive_by_line={},
            bytes_analyzed=100,
            was_truncated=False
        )

        assert result.line_count == 2
        assert list(result.line_byte_offsets) == [0, 5]
        assert result.include_positions == [10, 20]
        assert result.magic_positions == [5]
        assert result.directive_positions == {"include": [10, 20], "define": [30]}
        assert result.bytes_analyzed == 100
        assert result.was_truncated is False


class TestFileAnalyzer:
    """Test FileAnalyzer implementation."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.test_files = {}
        
    def teardown_method(self):
        """Clean up test files."""
        for filepath in self.test_files.values():
            try:
                os.unlink(filepath)
            except OSError:
                pass
                
    def create_test_file(self, filename, content):
        """Helper to create temporary test files."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
            f.write(content)
        self.test_files[filename] = f.name
        return f.name
        
    def test_simple_include_file(self):
        """Test FileAnalyzer on a simple file with includes."""
        content = '''#include <stdio.h>
#include <stdlib.h>
// #include "commented.h"
int main() {
    return 0;
}'''

        filepath = self.create_test_file("test.c", content)
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(filepath)
        args = SimpleNamespace(max_read_size=0, verbose=0)
        analyzer = FileAnalyzer(content_hash, args)
        result = analyzer.analyze()

        # Should have 2 include positions (not the commented one)
        assert len(result.include_positions) == 2
        # Verify the includes were detected correctly
        assert len(result.includes) == 2
        include_files = [inc['filename'] for inc in result.includes]
        assert 'stdio.h' in include_files
        assert 'stdlib.h' in include_files
        
    def test_magic_flags_detection(self):
        """Test magic flags detection."""
        content = '''// Magic flags test
//#LIBS=pthread m
//#CFLAGS=-O2 -g
#include <stdio.h>
int main() {
    return 0;
}'''

        filepath = self.create_test_file("magic.c", content)
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(filepath)
        args = SimpleNamespace(max_read_size=0, verbose=0)
        analyzer = FileAnalyzer(content_hash, args)
        result = analyzer.analyze()
        
        # Should detect 2 magic flags
        assert len(result.magic_positions) == 2
        assert len(result.magic_flags) == 2
        
        # Check magic flag content
        magic_keys = [flag['key'] for flag in result.magic_flags]
        assert 'LIBS' in magic_keys
        assert 'CFLAGS' in magic_keys


class TestReadFunctions:
    """Test different file reading functions."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.test_files = {}
        
    def teardown_method(self):
        """Clean up test files."""
        for filepath in self.test_files.values():
            try:
                os.unlink(filepath)
            except OSError:
                pass
                
    def create_test_file(self, filename, content):
        """Helper to create temporary test files."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
            f.write(content)
        self.test_files[filename] = f.name
        return f.name
        
    def test_read_file_mmap(self):
        """Test memory-mapped file reading."""
        content = "Hello\nWorld\nTest"
        filepath = self.create_test_file("mmap_test.c", content)
        
        # Test full file read
        text, bytes_analyzed, was_truncated = read_file_mmap(filepath, 0)
        assert text == content
        assert bytes_analyzed == len(content.encode('utf-8'))
        assert was_truncated is False
        
        # Test limited read
        text_limited, bytes_limited, was_truncated_limited = read_file_mmap(filepath, 5)
        assert len(text_limited.encode('utf-8')) <= 5
        assert bytes_limited <= 5
        assert was_truncated_limited is True
        
    def test_read_file_traditional(self):
        """Test traditional file reading."""
        content = "Traditional\nFile\nReading"
        filepath = self.create_test_file("traditional_test.c", content)
        
        # Test full file read
        text, bytes_analyzed, was_truncated = read_file_traditional(filepath, 0)
        assert text == content
        assert bytes_analyzed == len(content.encode('utf-8'))
        assert was_truncated is False
        
        # Test limited read
        text_limited, bytes_limited, was_truncated_limited = read_file_traditional(filepath, 8)
        assert len(text_limited.encode('utf-8')) <= 8
        assert bytes_limited <= 8
        assert was_truncated_limited is True
        
    def test_read_functions_consistency(self):
        """Test that mmap and traditional reading produce identical results."""
        content = "Consistency\nTest\nFile\nWith\nMultiple\nLines"
        filepath = self.create_test_file("consistency_test.c", content)
        
        # Read with both methods
        mmap_text, mmap_bytes, mmap_truncated = read_file_mmap(filepath, 0)
        trad_text, trad_bytes, trad_truncated = read_file_traditional(filepath, 0)
        
        # Results should be identical
        assert mmap_text == trad_text
        assert mmap_bytes == trad_bytes
        assert mmap_truncated == trad_truncated
        
    def test_empty_file_handling(self):
        """Test handling of empty files."""
        filepath = self.create_test_file("empty_test.c", "")
        
        # Both methods should handle empty files gracefully
        mmap_text, mmap_bytes, mmap_truncated = read_file_mmap(filepath, 0)
        trad_text, trad_bytes, trad_truncated = read_file_traditional(filepath, 0)
        
        assert mmap_text == ""
        assert trad_text == ""
        assert mmap_bytes == 0
        assert trad_bytes == 0
        assert mmap_truncated is False
        assert trad_truncated is False


class TestFileAnalyzerFactory:
    """Test FileAnalyzer constructor."""

    def test_analyzer_constructor(self):
        """Test that FileAnalyzer constructor works correctly."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
            f.write("int main() { return 0; }")
            filepath = f.name

        try:
            from compiletools.global_hash_registry import get_file_hash
            content_hash = get_file_hash(filepath)
            args = SimpleNamespace(max_read_size=0, verbose=0)
            analyzer = FileAnalyzer(content_hash, args)
            assert isinstance(analyzer, FileAnalyzer)
        finally:
            os.unlink(filepath)