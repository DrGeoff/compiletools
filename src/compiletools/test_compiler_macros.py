"""Tests for the compiler_macros module."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import compiletools.apptools as apptools
import compiletools.compiler_macros as cm
from compiletools.testhelper import requires_functional_compiler


def _mock_subprocess_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a MagicMock shaped like a ``subprocess.run`` CompletedProcess.

    Used by tests that patch ``subprocess.run`` and only care about
    ``.returncode`` / ``.stdout``. Keeps the boilerplate at each site to
    a single line so the test body stays focused on the behaviour under
    assertion.
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


class TestCompilerMacros:
    """Test the dynamic compiler macro detection functionality."""

    @pytest.fixture(autouse=True)
    def _clear_macro_cache(self):
        cm.get_compiler_macros.cache_clear()

    def test_get_compiler_macros_no_compiler(self):
        """Test get_compiler_macros with no compiler specified."""
        macros = cm.get_compiler_macros("", verbose=0)
        assert macros == {}

    def test_get_compiler_macros_success(self):
        """Test successful querying of compiler macros."""
        stdout = """#define __GNUC__ 11
#define __GNUC_MINOR__ 2
#define __GNUC_PATCHLEVEL__ 0
#define __VERSION__ "11.2.0"
#define __x86_64__ 1
#define __linux__ 1"""

        with patch("subprocess.run", return_value=_mock_subprocess_result(stdout=stdout)):
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
        stdout = '#define __VERSION__ "gcc version 11.2.0"'
        with patch("subprocess.run", return_value=_mock_subprocess_result(stdout=stdout)):
            macros = cm.get_compiler_macros("gcc", verbose=0)
            assert macros["__VERSION__"] == "gcc version 11.2.0"

    def test_unbalanced_quote_in_compiler_path_is_attributed_to_cxx(self):
        """final-review-v2 Minor #3: get_compiler_macros' only production
        caller (apptools.py / magicflags.py) always passes args.CXX, so its
        split_compiler_command() call should attribute a malformed value
        to "CXX" -- not the library-default generic "compiler command"
        slot, which would leave the user unable to tell which flag to fix.

        Mutation guard: dropping the ``slot="CXX"`` argument from
        compiler_macros.py's split_compiler_command call makes this fail
        with match="compiler command" instead.
        """
        import compiletools.utils

        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CXX"):
            cm.get_compiler_macros('ccache "g++', verbose=0)

    def test_get_compiler_macros_failure_nonzero_return(self):
        """Test handling of non-zero return code."""
        with patch("subprocess.run", return_value=_mock_subprocess_result(returncode=1)):
            macros = cm.get_compiler_macros("bad-compiler", verbose=0)
            assert macros == {}

    def test_retries_without_wp_v_when_driver_rejects_it(self):
        """A driver that rejects -Wp,-v must not lose the macro half.

        The F11 probe passes -Wp,-v so ONE invocation yields macros (stdout)
        and built-in include dirs (stderr). A driver that rejects -Wp,-v
        exits non-zero; get_compiler_macros must retry once without the
        flag, recovering the macros, and memoise () for the include dirs
        (the fallback run prints no search list)."""
        # get_compiler_macros is lru_cached and the include-dir memo is
        # module-global: start from a clean slate.
        cm.clear_cache()

        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "-Wp,-v" in argv:
                return _mock_subprocess_result(returncode=1)
            return _mock_subprocess_result(stdout="#define FOO 1")

        with patch("subprocess.run", side_effect=fake_run):
            macros = cm.get_compiler_macros("fake-driver-0001", verbose=0)
            dirs = cm.builtin_include_dirs("fake-driver-0001")

        assert macros == {"FOO": "1"}
        assert dirs == ()
        # The retry dropped the flag, and the () include-dir memo satisfied
        # the builtin_include_dirs query with no third invocation.
        assert len(calls) == 2
        assert "-Wp,-v" in calls[0]
        assert "-Wp,-v" not in calls[1]

    def test_get_compiler_macros_failure_not_found(self):
        """Test handling of FileNotFoundError."""
        with patch("subprocess.run", side_effect=FileNotFoundError("Compiler not found")):
            macros = cm.get_compiler_macros("nonexistent", verbose=0)
            assert macros == {}

    def test_get_compiler_macros_failure_timeout(self):
        """Test handling of timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 5)):
            macros = cm.get_compiler_macros("slow-compiler", verbose=0)
            assert macros == {}

    def test_lru_cache_functionality(self):
        """Test that the LRU cache is working properly."""
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


class TestBuiltinIncludeDirs:
    """``builtin_include_dirs`` -- Stage 2 of the auto-pkg-config plan.

    The include-dir probe is folded into the existing ``-dM`` macro probe
    via ``-Wp,-v`` (plan finding F11): one compiler invocation yields both
    the macros (stdout) and the angle-bracket search list (stderr), so the
    macros+dirs pair costs exactly one subprocess.
    """

    @pytest.fixture(autouse=True)
    def _clear_probe_caches(self):
        cm.clear_cache()
        yield
        cm.clear_cache()

    @requires_functional_compiler
    def test_returns_existing_dirs_containing_std_headers(self):
        compiler = apptools.get_functional_cxx_compiler()
        assert compiler, "the requires_functional_compiler decorator guarantees this"

        dirs = cm.builtin_include_dirs(compiler)

        assert dirs, "expected a non-empty built-in include dir list"
        for directory in dirs:
            assert os.path.isdir(directory)
        assert any(
            os.path.isfile(os.path.join(directory, "stddef.h")) or os.path.isfile(os.path.join(directory, "stdio.h"))
            for directory in dirs
        ), f"no built-in include dir ships stddef.h/stdio.h: {dirs}"

    @requires_functional_compiler
    def test_macros_and_dirs_share_one_subprocess(self, monkeypatch):
        """F11: the macros+dirs pair triggers exactly ONE compiler invocation."""
        compiler = apptools.get_functional_cxx_compiler()
        assert compiler, "the requires_functional_compiler decorator guarantees this"

        calls: list[object] = []
        real_run = subprocess.run

        def counting_run(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            return real_run(*args, **kwargs)

        monkeypatch.setattr(cm.subprocess, "run", counting_run)

        macros = cm.get_compiler_macros(compiler)
        dirs = cm.builtin_include_dirs(compiler)

        assert len(calls) == 1  # one invocation covers the pair
        assert macros
        assert dirs

    def test_failure_yields_empty_tuple(self):
        """Any probe failure degrades to ``()`` rather than raising."""
        assert cm.builtin_include_dirs("nonexistent_compiler_xyz_999") == ()
        assert cm.builtin_include_dirs("") == ()

    @requires_functional_compiler
    def test_cxx_probe_is_run_as_cxx_not_c(self):
        """Regression: the stdin probe (``-E -``) gives the driver no file
        extension to sniff, and gcc/clang then default to C -- even for
        ``g++``. That silently dropped ``__cplusplus`` and every ``__cpp_*``
        feature macro from the C++ macro set (so ``#if __cplusplus >= ...``
        guards evaluated false) and omitted the libstdc++ trees from the
        ``-Wp,-v`` search list (so ``<algorithm>`` was reported as a header
        the compiler could not place). ``-x c++`` pins the language.
        """
        compiler = apptools.get_functional_cxx_compiler()
        assert compiler, "the requires_functional_compiler decorator guarantees this"

        macros = cm.get_compiler_macros(compiler)
        dirs = cm.builtin_include_dirs(compiler)

        assert "__cplusplus" in macros, "C++ probe ran in C mode: __cplusplus missing"
        assert any(os.path.isfile(os.path.join(d, "algorithm")) for d in dirs), (
            f"no built-in dir ships <algorithm> -- libstdc++ trees missing from the C++ probe: {dirs}"
        )

    @requires_functional_compiler
    def test_c_and_cxx_probes_are_memoised_separately(self):
        """The same driver probed as C and as C++ yields different results
        and both are cached: a C probe must never serve the C++ answer."""
        compiler = apptools.get_functional_cxx_compiler()
        assert compiler, "the requires_functional_compiler decorator guarantees this"

        cxx_macros = cm.get_compiler_macros(compiler, language="c++")
        c_macros = cm.get_compiler_macros(compiler, language="c")

        assert "__cplusplus" in cxx_macros
        assert "__cplusplus" not in c_macros
        assert cm.builtin_include_dirs(compiler, language="c") != cm.builtin_include_dirs(compiler, language="c++")

    def test_unknown_language_is_rejected(self):
        with pytest.raises(ValueError, match="language must be one of"):
            cm.get_compiler_macros("g++", language="fortran")


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

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        cm.clear_cache()

    @pytest.mark.parametrize(
        ("stdout", "expression", "expected"),
        [
            pytest.param('# 1 "<stdin>"\n1\n', "__has_include(<iostream>)", 1, id="compiler-says-true"),
            pytest.param(
                '# 1 "<stdin>"\n0\n',
                "__has_include(<nonexistent_header_xyz.h>)",
                0,
                id="compiler-says-false",
            ),
        ],
    )
    def test_uses_compiler_preprocessor_result(self, stdout, expression, expected):
        """Mock compiler preprocessor output containing the query result."""
        with patch("subprocess.run", return_value=_mock_subprocess_result(stdout=stdout)):
            assert cm.query_has_function("gcc", expression) == expected

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
        with patch("subprocess.run", return_value=_mock_subprocess_result(returncode=1)):
            assert cm.query_has_function("gcc", "__has_include(<iostream>)") == 0

    def test_lru_cache_avoids_repeated_calls(self):
        """Same args should use cached result, not call subprocess again."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = '# 1 "<stdin>"\n1\n'
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
            result.stdout = '# 1 "<stdin>"\n1\n'
            return result

        with patch("subprocess.run", side_effect=mock_run):
            cm.query_has_function("gcc", "__has_include(<iostream>)")
            assert call_count == 1

            cm.clear_cache()

            cm.query_has_function("gcc", "__has_include(<iostream>)")
            assert call_count == 2

    def test_cppflags_passed_to_compiler(self):
        """CPPFLAGS should be inserted into the compiler command."""
        with patch("subprocess.run", return_value=_mock_subprocess_result(stdout="1\n")) as mock_run:
            cm.query_has_function("gcc", "__has_include(<foo.h>)", cppflags="-I/usr/local/include")
            args_used = mock_run.call_args[0][0]
            assert "-I/usr/local/include" in args_used

    def test_unbalanced_quote_in_compiler_path_is_attributed_to_cxx(self):
        """final-review-v2 Minor #3: query_has_function's only production
        caller (simple_preprocessor.py, via headerdeps/magicflags/
        preprocessing_cache) always threads through args.CXX, so its
        split_compiler_command() call should attribute a malformed value
        to "CXX", matching get_compiler_macros' sibling fix.

        Mutation guard: dropping the ``slot="CXX"`` argument from
        compiler_macros.py's split_compiler_command call makes this fail
        with match="compiler command" instead.
        """
        import compiletools.utils

        with pytest.raises(compiletools.utils.FlagTokenizeError, match="CXX"):
            cm.query_has_function('ccache "g++', "__has_include(<iostream>)")
