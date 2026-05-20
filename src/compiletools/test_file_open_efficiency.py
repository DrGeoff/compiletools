"""Test that ct-cake --auto opens each source file at most once.

This test verifies an important performance optimization: compiletools should
read each source file exactly once during dependency analysis, rather than
reopening files multiple times.
"""

import os
import unittest.mock as mock
from collections import Counter

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.test_base
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


class FileOpenTracker:
    """Context manager that tracks file open calls."""

    def __init__(self, track_extensions=(".cpp", ".c", ".h", ".hpp", ".cc", ".cxx")):
        self.track_extensions = track_extensions
        self.counter = Counter()
        self.original_open = open

    def tracking_open(self, filepath, *args, **kwargs):
        """Wrapper around open() that tracks source file access."""
        if isinstance(filepath, str):
            abs_path = os.path.realpath(filepath)
            if abs_path.endswith(self.track_extensions):
                self.counter[abs_path] += 1
        return self.original_open(filepath, *args, **kwargs)

    def __enter__(self):
        self.counter.clear()
        self.builtin_patch = mock.patch("builtins.open", side_effect=self.tracking_open)
        self.io_patch = mock.patch("io.open", side_effect=self.tracking_open)
        self.builtin_patch.__enter__()
        self.io_patch.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.io_patch.__exit__(exc_type, exc_val, exc_tb)
        self.builtin_patch.__exit__(exc_type, exc_val, exc_tb)
        return False

    def get_multiple_opens(self):
        """Return dict of files opened more than once."""
        return {path: count for path, count in self.counter.items() if count > 1}


class TestFileOpenEfficiency(compiletools.test_base.BaseCompileToolsTestCase):
    @pytest.mark.parametrize(
        "sample_dir",
        [
            "simple",
            "factory",
            "magicinclude",
        ],
    )
    def test_cake_auto_opens_files_once(self, sample_dir, monkeypatch):
        """Test that ct-cake --auto opens each source file at most once.

        This test verifies the efficiency of file I/O operations during the build
        process. Opening files multiple times is wasteful and indicates potential
        optimization issues in the dependency analysis code.
        """
        test_dir = uth.example_path(sample_dir)
        if not os.path.exists(test_dir):
            pytest.skip(f"Sample directory not found: {test_dir}")

        monkeypatch.chdir(test_dir)

        with uth.ParserContext(), FileOpenTracker() as tracker:
            cap = compiletools.apptools.create_parser("Test ct-cake file efficiency")
            compiletools.cake.Cake.add_arguments(cap)

            # --file-list triggers full dependency analysis without invoking the compiler.
            argv = ["--file-list"]
            args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())

            cake = compiletools.cake.Cake(args)
            cake.process()

        multiple_opens = tracker.get_multiple_opens()
        assert len(multiple_opens) == 0, f"Files opened multiple times: {multiple_opens}"
