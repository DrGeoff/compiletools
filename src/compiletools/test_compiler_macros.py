"""Tests for the compiler_macros module."""

import subprocess
from unittest.mock import MagicMock, patch

import compiletools.compiler_macros as cm


class TestCompilerMacros:
    """Test the dynamic compiler macro detection functionality."""

    def test_get_compiler_macros_no_compiler(self):
        """Test get_compiler_macros with no compiler specified."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        macros = cm.get_compiler_macros("", verbose=0)
        assert macros == {}

    def test_get_compiler_macros_success(self):
        """Test successful querying of compiler macros."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        # Mock successful subprocess call
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """#define __GNUC__ 11
#define __GNUC_MINOR__ 2
#define __GNUC_PATCHLEVEL__ 0
#define __VERSION__ "11.2.0"
#define __x86_64__ 1
#define __linux__ 1"""

        with patch("subprocess.run", return_value=mock_result):
            macros = cm.get_compiler_macros("gcc", verbose=0)
            assert "__GNUC__" in macros
            assert macros["__GNUC__"] == "11"
            assert "__GNUC_MINOR__" in macros
            assert macros["__GNUC_MINOR__"] == "2"
            assert "__linux__" in macros
            assert macros["__linux__"] == "1"
            assert "__VERSION__" in macros
            assert macros["__VERSION__"] == "11.2.0"

    def test_get_compiler_macros_with_quotes(self):
        """Test handling of macros with quoted values."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '#define __VERSION__ "gcc version 11.2.0"'

        with patch("subprocess.run", return_value=mock_result):
            macros = cm.get_compiler_macros("gcc", verbose=0)
            assert macros["__VERSION__"] == "gcc version 11.2.0"

    def test_get_compiler_macros_failure_nonzero_return(self):
        """Test handling of non-zero return code."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            macros = cm.get_compiler_macros("bad-compiler", verbose=0)
            assert macros == {}

    def test_get_compiler_macros_failure_not_found(self):
        """Test handling of FileNotFoundError."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        with patch("subprocess.run", side_effect=FileNotFoundError("Compiler not found")):
            macros = cm.get_compiler_macros("nonexistent", verbose=0)
            assert macros == {}

    def test_get_compiler_macros_failure_timeout(self):
        """Test handling of timeout."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            macros = cm.get_compiler_macros("slow-compiler", verbose=0)
            assert macros == {}

    def test_lru_cache_functionality(self):
        """Test that the LRU cache is working properly."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = "#define __TEST__ 1"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            # First call should query the compiler
            macros1 = cm.get_compiler_macros("gcc", verbose=0)
            assert call_count == 1

            # Second call with same args should use cache
            macros2 = cm.get_compiler_macros("gcc", verbose=0)
            assert call_count == 1  # Should not have increased

            # Results should be identical
            assert macros1 == macros2

            # Different compiler should trigger new query
            cm.get_compiler_macros("clang", verbose=0)
            assert call_count == 2

    def test_clear_cache(self):
        """Test the cache clearing functionality."""
        # Populate cache
        cm.get_compiler_macros.cache_clear()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "#define __TEST__ 1"

            # First call
            cm.get_compiler_macros("gcc", verbose=0)
            assert mock_run.call_count == 1

            # Second call uses cache
            cm.get_compiler_macros("gcc", verbose=0)
            assert mock_run.call_count == 1

            # Clear cache
            cm.clear_cache()

            # Next call should query again
            cm.get_compiler_macros("gcc", verbose=0)
            assert mock_run.call_count == 2

    def test_real_gcc_if_available(self):
        """Test with real GCC compiler if available."""
        # Clear cache first
        cm.get_compiler_macros.cache_clear()

        # This test will only run if gcc is actually available
        try:
            subprocess.run(["gcc", "--version"], capture_output=True, check=True, timeout=1)
            has_gcc = True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            has_gcc = False

        if has_gcc:
            macros = cm.get_compiler_macros("gcc", verbose=0)
            # GCC should always define __GNUC__
            assert "__GNUC__" in macros
            # Should have many macros
            assert len(macros) > 50  # GCC typically defines 100+ macros


class TestFilterForExpansion:
    """Test filter_for_expansion() which strips non-standard legacy macros."""

    def test_keeps_double_underscore_macros(self):
        macros = {"__linux__": "1", "__GNUC__": "11", "__cplusplus": "201703L"}
        assert cm.filter_for_expansion(macros) == macros

    def test_keeps_single_underscore_macros(self):
        macros = {"_LP64": "1", "_STDC_PREDEF_H": "1"}
        assert cm.filter_for_expansion(macros) == macros

    def test_removes_bare_linux_and_unix(self):
        macros = {
            "__linux__": "1",
            "linux": "1",
            "__unix__": "1",
            "unix": "1",
            "__GNUC__": "11",
        }
        filtered = cm.filter_for_expansion(macros)
        assert "linux" not in filtered
        assert "unix" not in filtered
        assert "__linux__" in filtered
        assert "__unix__" in filtered
        assert "__GNUC__" in filtered

    def test_empty_dict(self):
        assert cm.filter_for_expansion({}) == {}


class TestQueryHasFunction:
    """Test the query_has_function() functionality."""

    def setup_method(self):
        cm.clear_cache()

    def test_returns_1_when_compiler_says_true(self):
        """Mock compiler preprocessor output containing '1'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "# 1 \"<stdin>\"\n1\n"

        with patch("subprocess.run", return_value=mock_result):
            assert cm.query_has_function("gcc", "__has_include(<iostream>)") == 1

    def test_returns_0_when_compiler_says_false(self):
        """Mock compiler preprocessor output containing '0'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "# 1 \"<stdin>\"\n0\n"

        with patch("subprocess.run", return_value=mock_result):
            assert cm.query_has_function("gcc", "__has_include(<nonexistent_header_xyz.h>)") == 0

    def test_returns_0_for_empty_compiler(self):
        """No compiler specified should return 0."""
        assert cm.query_has_function("", "__has_include(<iostream>)") == 0

    def test_returns_0_on_file_not_found(self):
        """FileNotFoundError should return 0."""
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            assert cm.query_has_function("nonexistent-compiler", "__has_include(<iostream>)") == 0

    def test_returns_0_on_timeout(self):
        """TimeoutExpired should return 0."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            assert cm.query_has_function("slow-compiler", "__has_include(<iostream>)") == 0

    def test_returns_0_on_nonzero_return_code(self):
        """Non-zero return code should return 0."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            assert cm.query_has_function("gcc", "__has_include(<iostream>)") == 0

    def test_lru_cache_avoids_repeated_calls(self):
        """Same args should use cached result, not call subprocess again."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = "# 1 \"<stdin>\"\n1\n"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            assert cm.query_has_function("gcc", "__has_include(<iostream>)") == 1
            assert call_count == 1

            # Second call with same args should use cache
            assert cm.query_has_function("gcc", "__has_include(<iostream>)") == 1
            assert call_count == 1

    def test_clear_cache_forces_recomputation(self):
        """clear_cache() should clear query_has_function cache."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = "# 1 \"<stdin>\"\n1\n"
            return result

        with patch("subprocess.run", side_effect=mock_run):
            cm.query_has_function("gcc", "__has_include(<iostream>)")
            assert call_count == 1

            cm.clear_cache()

            cm.query_has_function("gcc", "__has_include(<iostream>)")
            assert call_count == 2

    def test_cppflags_passed_to_compiler(self):
        """CPPFLAGS should be inserted into the compiler command."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            cm.query_has_function("gcc", "__has_include(<foo.h>)", cppflags="-I/usr/local/include")
            args_used = mock_run.call_args[0][0]
            assert "-I/usr/local/include" in args_used
