import argparse
import os
import sys

import pytest

from compiletools.build_context import BuildContext
from compiletools.file_analyzer import FileAnalysisResult, analyze_file, set_analyzer_args
from compiletools.global_hash_registry import get_filepath_by_hash
from compiletools.simple_preprocessor import SimplePreprocessor

# Add the parent directory to sys.path so we can import ct modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestGlobalHashRegistry:
    """Unit tests for global_hash_registry.py functions"""

    def test_get_filepath_by_hash_raises_on_missing(self):
        """Verify get_filepath_by_hash raises FileNotFoundError for unknown hash."""
        ctx = BuildContext()
        fake_hash = "0" * 40  # Hash that doesn't exist

        with pytest.raises(FileNotFoundError, match="not found in working directory"):
            get_filepath_by_hash(fake_hash, ctx)

    def test_get_filepath_by_hash_names_remedy_on_duplicate(self):
        """The duplicate-content error must state the unique-content rule and
        warn that --auto-exclude cannot resolve it, so users are not sent
        toward the one remedy that does not work."""
        ctx = BuildContext()
        dup_hash = "1" * 40
        ctx.file_hashes = {"/repo/app/util.hpp": dup_hash, "/repo/vendor/util.hpp": dup_hash}
        ctx.reverse_hashes = {dup_hash: ["/repo/app/util.hpp", "/repo/vendor/util.hpp"]}

        with pytest.raises(RuntimeError) as excinfo:
            get_filepath_by_hash(dup_hash, ctx)

        message = str(excinfo.value)
        assert "/repo/app/util.hpp" in message
        assert "/repo/vendor/util.hpp" in message
        assert "unique content" in message
        assert "--auto-exclude cannot resolve this" in message

    def test_file_analyzer_raises_on_missing(self):
        """Verify file_analyzer fails fast when file missing from registry."""
        ctx = BuildContext()
        args = argparse.Namespace(verbose=0)
        set_analyzer_args(args, ctx)

        fake_hash = "0" * 40  # Hash not in registry

        with pytest.raises(FileNotFoundError):
            analyze_file(fake_hash, ctx)

    def test_simple_preprocessor_raises_on_missing(self):
        """Verify simple_preprocessor fails fast when file missing from registry."""
        ctx = BuildContext()
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
            content_hash=fake_hash,
        )

        # Should raise FileNotFoundError when looking up filepath from fake hash
        with pytest.raises(FileNotFoundError, match="not found in working directory"):
            preprocessor.process_structured(file_result, context=ctx)
