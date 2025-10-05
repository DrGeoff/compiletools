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
            'use_mmap': True,
            'force_mmap': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy in ['mmap', 'no_mmap']

    def test_manual_override_no_use_mmap(self):
        """Test manual override to disable mmap (no_mmap mode)."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'use_mmap': False,
            'force_mmap': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'no_mmap'

    def test_manual_override_force_mmap(self):
        """Test manual override to force mmap mode."""
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': [],
            'testmarkers': [],
            'librarymarkers': [],
            'use_mmap': True,
            'force_mmap': True,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'mmap'

    def test_low_ulimit_auto_no_mmap(self):
        """Test that very low ulimit (< 100) automatically triggers no_mmap mode."""
        # This test simulates the behavior, but can't actually change ulimit
        # The real test is that pytest -n auto works with ulimit 20
        import resource
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            # If we're running with low ulimit, verify no_mmap is used
            if soft < 100:
                args = type('Args', (), {
                    'max_read_size': 0,
                    'verbose': 0,
                    'exemarkers': [],
                    'testmarkers': [],
                    'librarymarkers': [],
                    'use_mmap': True,
                    'force_mmap': False,
                    'suppress_fd_warnings': True,
                    'suppress_filesystem_warnings': True,
                })()

                set_analyzer_args(args)
                strategy = _determine_file_reading_strategy()
                assert strategy == 'no_mmap', f"Expected no_mmap with ulimit {soft}, got {strategy}"
        except (OSError, AttributeError):
            pytest.skip("Cannot read ulimit on this system")


class TestFileAnalyzerArguments:
    """Tests for FileAnalyzer.add_arguments."""

    def test_add_arguments_no_use_mmap(self):
        """Test that --no-use-mmap argument works."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--no-use-mmap'])
        assert args.use_mmap is False

    def test_add_arguments_force_mmap(self):
        """Test that --force-mmap argument works."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--force-mmap'])
        assert args.force_mmap is True

    def test_add_arguments_suppress_warnings(self):
        """Test that warning suppression arguments work."""
        import configargparse
        cap = configargparse.ArgumentParser()
        FileAnalyzer.add_arguments(cap)

        args = cap.parse_args(['--suppress-fd-warnings', '--suppress-filesystem-warnings'])
        assert args.suppress_fd_warnings is True
        assert args.suppress_filesystem_warnings is True



class TestFileReadingWithRealFiles:
    """Tests that verify file reading strategies work with real sample files."""

    def setup_method(self):
        """Reset cached strategy before each test."""
        import compiletools.file_analyzer
        compiletools.file_analyzer._file_reading_strategy = None
        compiletools.file_analyzer._analyzer_args = None
        compiletools.file_analyzer._filesystem_override_strategy = None

    def test_no_mmap_mode_reads_real_file(self):
        """Test that no_mmap mode actually reads and analyzes real files correctly."""
        from compiletools.global_hash_registry import load_hashes, get_file_hash
        import compiletools.wrappedos

        # Use a simple sample file
        sample_file = os.path.join(
            os.path.dirname(__file__),
            'samples', 'simple', 'helloworld_cpp.cpp'
        )
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file), f"Sample file not found: {sample_file}"

        # Load hash registry
        load_hashes()
        content_hash = get_file_hash(sample_file)

        # Configure no_mmap mode
        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': ['int main'],
            'testmarkers': [],
            'librarymarkers': [],
            'use_mmap': False,
            'force_mmap': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'no_mmap'

        # Analyze the file - this exercises _read_file_with_strategy
        from compiletools.file_analyzer import analyze_file
        result = analyze_file(content_hash)

        # Verify the analysis worked correctly
        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any('iostream' in str(inc['filename']) for inc in result.includes)
        assert result.bytes_analyzed > 0

    def test_mmap_mode_reads_real_file(self):
        """Test that mmap mode reads and analyzes real files correctly."""
        from compiletools.global_hash_registry import load_hashes, get_file_hash
        import compiletools.wrappedos

        sample_file = os.path.join(
            os.path.dirname(__file__),
            'samples', 'simple', 'helloworld_cpp.cpp'
        )
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file)

        load_hashes()
        content_hash = get_file_hash(sample_file)

        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': ['int main'],
            'testmarkers': [],
            'librarymarkers': [],
            'use_mmap': True,
            'force_mmap': True,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'mmap'

        from compiletools.file_analyzer import analyze_file
        result = analyze_file(content_hash)

        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any('iostream' in str(inc['filename']) for inc in result.includes)

    def test_no_use_mmap_mode_reads_real_file(self):
        """Test that --no-use-mmap mode reads and analyzes real files correctly."""
        from compiletools.global_hash_registry import load_hashes, get_file_hash
        import compiletools.wrappedos

        sample_file = os.path.join(
            os.path.dirname(__file__),
            'samples', 'simple', 'helloworld_cpp.cpp'
        )
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file)

        load_hashes()
        content_hash = get_file_hash(sample_file)

        args = type('Args', (), {
            'max_read_size': 0,
            'verbose': 0,
            'exemarkers': ['int main'],
            'testmarkers': [],
            'librarymarkers': [],
            'use_mmap': False,
            'force_mmap': False,
            'suppress_fd_warnings': True,
            'suppress_filesystem_warnings': True,
        })()

        set_analyzer_args(args)
        strategy = _determine_file_reading_strategy()
        assert strategy == 'no_mmap'

        from compiletools.file_analyzer import analyze_file
        result = analyze_file(content_hash)

        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any('iostream' in str(inc['filename']) for inc in result.includes)
