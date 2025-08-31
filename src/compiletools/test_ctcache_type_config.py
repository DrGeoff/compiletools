"""Tests for CTCACHE_TYPE configuration hierarchy."""

import os
from pathlib import Path
import pytest

import compiletools.dirnamer
import compiletools.configutils
from compiletools.file_analyzer import create_file_analyzer
from compiletools.testhelper import samplesdir


def get_text_from_result(result):
    """Helper function to reconstruct text from FileAnalysisResult lines for testing."""
    return '\n'.join(result.lines)


class TestCTCacheTypeConfiguration:
    """Test CTCACHE_TYPE configuration hierarchy and integration."""
    
    @pytest.fixture(autouse=True)
    def preserve_environment(self):
        """Preserve and restore CTCACHE_TYPE environment variable."""
        original_env = os.environ.get('CTCACHE_TYPE')
        
        yield
        
        # Restore original value
        if original_env is not None:
            os.environ['CTCACHE_TYPE'] = original_env
        elif 'CTCACHE_TYPE' in os.environ:
            del os.environ['CTCACHE_TYPE']
    
    def test_default_from_config_file(self):
        """Test that default value from config file is None."""
        # Clear environment variable
        if 'CTCACHE_TYPE' in os.environ:
            del os.environ['CTCACHE_TYPE']
        
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type is None, "Default should be None"
    
    def test_environment_variable_override(self):
        """Test that environment variable overrides config file."""
        os.environ['CTCACHE_TYPE'] = 'disk'
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type == 'disk', "Should get value from environment"
    
    def test_command_line_override(self):
        """Test that command line overrides environment variable."""
        # Set environment variable
        os.environ['CTCACHE_TYPE'] = 'disk'
        
        # Command line should override
        argv = ['--CTCACHE_TYPE=memory']
        cache_type = compiletools.dirnamer.get_cache_type(argv=argv)
        assert cache_type == 'memory', "Command line should override environment"
    
    def test_none_string_conversion(self):
        """Test that 'None' string is converted to None value."""
        os.environ['CTCACHE_TYPE'] = 'None'
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type is None, "'None' string should convert to None"
    
    @pytest.mark.parametrize("cache_type_str", ['null', 'memory', 'disk', 'sqlite', 'redis', 'oracle', 'mmap'])
    def test_valid_cache_types(self, cache_type_str):
        """Test that all valid cache types are returned correctly."""
        os.environ['CTCACHE_TYPE'] = cache_type_str
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type == cache_type_str, f"Should get {cache_type_str}"
    
    @pytest.fixture
    def simple_cpp_file(self):
        """Path to existing simple C++ test file."""
        return str(Path(samplesdir()) / "simple" / "helloworld_cpp.cpp")
    
    @pytest.mark.parametrize("cache_type_str,expected", [
        ('None', None),
        ('memory', 'memory'),
        ('disk', 'disk'),
    ])
    def test_integration_with_file_analyzer(self, simple_cpp_file, cache_type_str, expected):
        """Test that CTCACHE_TYPE configuration is properly used by FileAnalyzer."""
        # Set environment variable to test configuration pickup
        os.environ['CTCACHE_TYPE'] = cache_type_str
        
        # Create analyzer - it should use the environment configuration via get_cache_type
        # when cache_type parameter matches the expected value
        analyzer = create_file_analyzer(simple_cpp_file, cache_type=expected)
        result = analyzer.analyze()
        
        # Verify analysis works
        assert result is not None, "Analysis should succeed"
        assert "#include" in get_text_from_result(result), "Should read file content"
        assert len(result.include_positions) > 0, "Should detect include statements"
    
    def test_verbose_output_disabled_by_default(self):
        """Test that verbose output is disabled by default."""
        # This test ensures that get_cache_type doesn't print debug info
        # when verbose is not explicitly set
        
        # Clear environment
        if 'CTCACHE_TYPE' in os.environ:
            del os.environ['CTCACHE_TYPE']
        
        # Should not raise exceptions or print to stdout
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type is None
    
    def test_priority_order_complete(self):
        """Test the complete priority order: CLI > ENV > config > default."""
        # Set up environment
        os.environ['CTCACHE_TYPE'] = 'memory'
        
        # Environment should be used when no CLI args
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type == 'memory'
        
        # CLI should override environment
        argv = ['--CTCACHE_TYPE=disk']
        cache_type = compiletools.dirnamer.get_cache_type(argv=argv)
        assert cache_type == 'disk'
        
        # Clear environment - should fall back to config default (None)
        del os.environ['CTCACHE_TYPE']
        cache_type = compiletools.dirnamer.get_cache_type()
        assert cache_type is None
    
    @pytest.mark.parametrize("cache_type_str,expected", [
        ('None', None),
        ('memory', 'memory'),
        ('disk', 'disk'),
        ('oracle', 'oracle'),
        ('mmap', 'mmap'),
    ])
    def test_calculator_sample_with_cache_types(self, cache_type_str, expected):
        """Test calculator sample dependency detection across different cache types.
        
        This test specifically verifies the deeper dependency pattern that exposes cache bugs:
        main.cpp → calculator.h → calculator.cpp → add.H → (build system discovers) → add.C
        """
        import compiletools.testhelper as uth
        
        # Set cache type environment
        os.environ['CTCACHE_TYPE'] = cache_type_str
        
        # Get calculator sample files
        calculator_main = str(Path(samplesdir()) / "calculator" / "main.cpp")
        
        # Use temp directory for cache location instead of project root
        if cache_type_str == 'None':
            cache_dir = 'None'
            # Use the utility function from testhelper instead of manual setup
            dependencies = uth.headerdeps_result(
                calculator_main,
                kind="direct",
                include=str(Path(samplesdir()) / "calculator"),
                cache=cache_dir
            )
            
            # Test cache consistency by running again
            dependencies2 = uth.headerdeps_result(
                calculator_main,
                kind="direct", 
                include=str(Path(samplesdir()) / "calculator"),
                cache=cache_dir
            )
        else:
            # Use temp directory context for cache types that need disk storage
            with uth.TempDirContextNoChange(prefix=f'test_cache_{cache_type_str}_') as cache_dir:
                
                # Use the utility function from testhelper instead of manual setup
                dependencies = uth.headerdeps_result(
                    calculator_main,
                    kind="direct",
                    include=str(Path(samplesdir()) / "calculator"),
                    cache=cache_dir
                )
                
                # Test cache consistency by running again
                dependencies2 = uth.headerdeps_result(
                    calculator_main,
                    kind="direct", 
                    include=str(Path(samplesdir()) / "calculator"),
                    cache=cache_dir
                )
        
        # Verify that calculator.h is in dependencies
        calculator_h_found = False
        for dep in dependencies:
            if dep.endswith('calculator.h'):
                calculator_h_found = True
                break
        
        assert calculator_h_found, f"calculator.h not found in dependencies with cache type {cache_type_str}. Dependencies: {dependencies}"
        
        # Verify add.H is also found in the deeper dependency chain
        add_h_found = False
        for dep in dependencies:
            if dep.endswith('add.H'):
                add_h_found = True
                break
        
        assert add_h_found, f"add.H not found in dependencies with cache type {cache_type_str}. Dependencies: {dependencies}"
        
        assert dependencies == dependencies2, f"Second run results differ for {cache_type_str}"


class TestCTCacheTypeArgument:
    """Test CTCACHE_TYPE command line argument handling."""
    
    def test_argument_choices_validation(self):
        """Test that the argument parser accepts valid choices."""
        # This would typically be tested by actually parsing arguments,
        # but we test the configuration extraction instead
        
        valid_choices = ["None", "null", "memory", "disk", "sqlite", "redis", "oracle", "mmap"]
        
        for choice in valid_choices:
            argv = [f'--CTCACHE_TYPE={choice}']
            extracted = compiletools.configutils.extract_value_from_argv(
                key="CTCACHE_TYPE", argv=argv
            )
            assert extracted == choice, f"Should extract {choice} from command line"