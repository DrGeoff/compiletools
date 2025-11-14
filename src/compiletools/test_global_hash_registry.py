import sys
import os
import pytest

# Add the parent directory to sys.path so we can import ct modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestGlobalHashRegistry:
    """Unit tests for global_hash_registry.py functions"""

    def test_get_filepath_by_hash_raises_on_missing(self):
        """Verify get_filepath_by_hash raises FileNotFoundError for unknown hash."""
        from compiletools.global_hash_registry import get_filepath_by_hash

        fake_hash = "0" * 40  # Hash that doesn't exist

        with pytest.raises(FileNotFoundError, match="not found in working directory"):
            get_filepath_by_hash(fake_hash)

    def test_file_analyzer_raises_on_missing(self):
        """Verify file_analyzer fails fast when file missing from registry."""
        from compiletools.file_analyzer import analyze_file, set_analyzer_args
        import argparse

        args = argparse.Namespace(verbose=0)
        set_analyzer_args(args)

        fake_hash = "0" * 40  # Hash not in registry

        with pytest.raises(FileNotFoundError):
            analyze_file(fake_hash)

    def test_simple_preprocessor_raises_on_missing(self):
        """Verify simple_preprocessor fails fast when file missing from registry."""
        import stringzilla as sz
        from compiletools.simple_preprocessor import SimplePreprocessor
        from compiletools.file_analyzer import FileAnalysisResult

        # Create SimplePreprocessor instance with empty macro state
        preprocessor = SimplePreprocessor(defined_macros={})

        # Create minimal FileAnalysisResult with all required fields
        # Use fake hash not in registry
        fake_hash = "0" * 40
        file_result = FileAnalysisResult(
            # Required fields
            line_count=10,
            line_byte_offsets=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90],
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[],
            directive_by_line={},
            bytes_analyzed=100,
            was_truncated=False,
            # Optional field with fake hash
            content_hash=fake_hash
        )

        # Should raise FileNotFoundError when looking up filepath from fake hash
        with pytest.raises(FileNotFoundError, match="not found in working directory"):
            preprocessor.process_structured(file_result)
