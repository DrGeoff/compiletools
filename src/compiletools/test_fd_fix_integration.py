"""Integration tests for file descriptor and filesystem compatibility fixes."""

import os
import resource
import pytest
from compiletools.filesystem_utils import (
    get_filesystem_type,
    get_lock_strategy,
    supports_mmap_safely,
    get_lockdir_sleep_interval,
)
from compiletools.file_analyzer import (
    FileAnalyzer,
    set_analyzer_args,
    _determine_file_reading_strategy,
)


class TestFilesystemIntegration:
    """Integration tests for filesystem detection."""

    def test_filesystem_detection_on_cwd(self):
        """Test that filesystem detection works on current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        assert isinstance(fstype, str)
        assert len(fstype) > 0

    def test_lock_strategy_for_cwd(self):
        """Test that lock strategy works for current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        strategy = get_lock_strategy(fstype)
        assert strategy in ['lockdir', 'cifs', 'flock']

    def test_mmap_safety_for_cwd(self):
        """Test that mmap safety check works for current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        mmap_safe = supports_mmap_safely(fstype)
        assert isinstance(mmap_safe, bool)

    def test_sleep_interval_for_cwd(self):
        """Test that sleep interval calculation works."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        interval = get_lockdir_sleep_interval(fstype)
        assert isinstance(interval, float)
        assert interval > 0


class TestUlimitDetection:
    """Tests for ulimit detection."""

    def test_ulimit_readable(self):
        """Test that ulimit can be read."""
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert soft > 0
            assert hard >= soft
        except (OSError, AttributeError):
            pytest.skip("Ulimit not available on this system")


class TestFileReadingStrategy:
    """Tests for file reading strategy selection."""

    def setup_method(self):
        """Reset cached strategy before each test."""
        import compiletools.file_analyzer
        compiletools.file_analyzer._file_reading_strategy = None
        compiletools.file_analyzer._analyzer_args = None

    def test_default_strategy(self):
        """Test default strategy selection."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'no_mmap': False,
            'fd_safe_file_reading': False,
            'force_normal_mode': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy in ['normal', 'fd_safe', 'no_mmap']

    def test_manual_override_no_mmap(self):
        """Test manual override to no_mmap mode."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'no_mmap': True,
            'fd_safe_file_reading': False,
            'force_normal_mode': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'no_mmap'

    def test_manual_override_fd_safe(self):
        """Test manual override to fd_safe mode."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'no_mmap': False,
            'fd_safe_file_reading': True,
            'force_normal_mode': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'fd_safe'

    def test_manual_override_force_normal(self):
        """Test manual override to force normal mode."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'no_mmap': False,
            'fd_safe_file_reading': False,
            'force_normal_mode': True,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'normal'

    def test_low_ulimit_auto_fd_safe(self):
        """Test that very low ulimit (< 100) automatically triggers fd_safe mode."""
        # This test simulates the behavior, but can't actually change ulimit
        # The real test is that pytest -n auto works with ulimit 20
        import resource
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            # If we're running with low ulimit, verify fd_safe is used
            if soft < 100:
                args = type('Args', (), {
                    'max_read_size': 0,
                    'verbose': 0,
                    'exemarkers': [],
                    'testmarkers': [],
                    'librarymarkers': [],
                    'no_mmap': False,
                    'fd_safe_file_reading': False,
                    'force_normal_mode': False,
                    'suppress_fd_warnings': True,
                    'suppress_filesystem_warnings': True,
                })()

                set_analyzer_args(args)
                strategy = _determine_file_reading_strategy()
                assert strategy == 'fd_safe', f"Expected fd_safe with ulimit {soft}, got {strategy}"
        except (OSError, AttributeError):
            pytest.skip("Cannot read ulimit on this system")


class TestFileAnalyzerArguments:
    """Tests for FileAnalyzer.add_arguments."""

    def test_add_arguments_no_mmap(self):
        """Test that --no-mmap argument works."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--no-mmap'])
        assert args.no_mmap is True

    def test_add_arguments_fd_safe(self):
        """Test that --fd-safe-file-reading argument works."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--fd-safe-file-reading'])
        assert args.fd_safe_file_reading is True

    def test_add_arguments_suppress_warnings(self):
        """Test that warning suppression arguments work."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--suppress-fd-warnings', '--suppress-filesystem-warnings'])
        assert args.suppress_fd_warnings is True
        assert args.suppress_filesystem_warnings is True

    def test_add_arguments_force_normal(self):
        """Test that --force-normal-mode argument works."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--force-normal-mode'])
        assert args.force_normal_mode is True
