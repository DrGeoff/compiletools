"""Test that ct-cake --auto opens each source file at most once.

This test verifies an important performance optimization: compiletools should
read each source file exactly once during dependency analysis, rather than
reopening files multiple times.
"""

import os
import pytest
import unittest.mock as mock
from collections import Counter

import compiletools.apptools
import compiletools.cake
import compiletools.testhelper as uth


class FileOpenTracker:
    """Context manager that tracks file open calls."""

    def __init__(self, track_extensions=('.cpp', '.c', '.h', '.hpp', '.cc', '.cxx')):
        self.track_extensions = track_extensions
        self.counter = Counter()
        self.original_open = open

    def tracking_open(self, filepath, *args, **kwargs):
        """Wrapper around open() that tracks source file access."""
        if isinstance(filepath, str):
            abs_path = os.path.abspath(filepath)
            if abs_path.endswith(self.track_extensions):
                self.counter[abs_path] += 1
        return self.original_open(filepath, *args, **kwargs)

    def __enter__(self):
        self.counter.clear()
        self.builtin_patch = mock.patch('builtins.open', side_effect=self.tracking_open)
        self.io_patch = mock.patch('io.open', side_effect=self.tracking_open)
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


@pytest.mark.parametrize("sample_dir", [
    "simple",
    "factory",
    "magicinclude",
])
def test_cake_auto_opens_files_once(sample_dir):
    """Test that ct-cake --auto opens each source file at most once.

    This test verifies the efficiency of file I/O operations during the build
    process. Opening files multiple times is wasteful and indicates potential
    optimization issues in the dependency analysis code.
    """
    # Get the sample directory path
    samples_base = uth.samplesdir()
    test_dir = os.path.join(samples_base, sample_dir)

    if not os.path.exists(test_dir):
        pytest.skip(f"Sample directory not found: {test_dir}")

    # Save current directory to restore later
    original_dir = os.getcwd()

    try:
        # Change to test directory
        os.chdir(test_dir)

        # Track file opens during cake execution
        with uth.ParserContext():
            with FileOpenTracker() as tracker:
                # Create argument parser and run cake
                cap = compiletools.apptools.create_parser("Test ct-cake file efficiency")
                compiletools.cake.Cake.add_arguments(cap)

                # Use --auto to trigger dependency analysis and --file-list to avoid compilation
                # This runs the full dependency analysis without invoking the compiler
                argv = ['--auto', '--file-list']
                args = compiletools.apptools.parseargs(cap, argv=argv)

                try:
                    cake = compiletools.cake.Cake(args)
                    cake.process()
                except SystemExit:
                    pass  # May exit early if no targets found

            # Check for multiple opens
            multiple_opens = tracker.get_multiple_opens()

            if multiple_opens:
                # Create detailed failure message
                msg_parts = [
                    f"\n{len(multiple_opens)} source files were opened multiple times in {sample_dir}:"
                ]
                for path, count in sorted(multiple_opens.items(), key=lambda x: -x[1]):
                    msg_parts.append(f"  {count}x: {os.path.basename(path)}")
                msg_parts.append("\nAll file opens:")
                for path, count in sorted(tracker.counter.items()):
                    msg_parts.append(f"  {count}x: {os.path.basename(path)}")

                pytest.fail("\n".join(msg_parts))

            # Success: all files opened at most once
            assert len(multiple_opens) == 0, "All source files should be opened at most once"

    finally:
        # Restore original directory
        os.chdir(original_dir)
