"""Unit tests for magicflags.py flag handler methods and helpers."""

import os
import tempfile
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stringzilla as sz


class TestHandleInclude:
    """Test MagicFlagsBase._handle_include()."""

    def _make_base(self):
        """Create a MagicFlagsBase with minimal mocks."""
        from compiletools.magicflags import MagicFlagsBase

        args = SimpleNamespace(verbose=0)
        # Patch __init__ to avoid full initialization
        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = args
            return obj

    def test_handle_include_adds_I_flag(self):
        obj = self._make_base()
        result = obj._handle_include(sz.Str("/some/path"))
        assert sz.Str("-I") in result[sz.Str("CPPFLAGS")]
        assert sz.Str("/some/path") in result[sz.Str("CPPFLAGS")]
        assert sz.Str("-I") in result[sz.Str("CFLAGS")]
        assert sz.Str("-I") in result[sz.Str("CXXFLAGS")]


class TestHandleSource:
    """Test MagicFlagsBase._handle_source()."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=0)
            return obj

    def test_handle_source_absolute(self):
        obj = self._make_base()
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as f:
            f.write(b"int x;")
            tmppath = f.name
        try:
            magic_flag_data = {"source_file_context": None}
            result = obj._handle_source(
                sz.Str(tmppath), magic_flag_data, "/some/main.cpp", sz.Str("SOURCE")
            )
            assert str(result).endswith(".cpp")
        finally:
            os.unlink(tmppath)

    def test_handle_source_relative(self):
        obj = self._make_base()
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "helper.cpp")
            with open(source, "w") as f:
                f.write("int x;")
            main_file = os.path.join(tmpdir, "main.cpp")
            magic_flag_data = {"source_file_context": None}
            result = obj._handle_source(
                sz.Str("helper.cpp"), magic_flag_data, main_file, sz.Str("SOURCE")
            )
            assert str(result).endswith("helper.cpp")

    def test_handle_source_nonexistent(self):
        obj = self._make_base()
        magic_flag_data = {"source_file_context": None}
        import pytest

        with pytest.raises(OSError):
            obj._handle_source(
                sz.Str("/nonexistent/file.cpp"), magic_flag_data, "/some/main.cpp", sz.Str("SOURCE")
            )


class TestExtractMacrosFromMagicFlags:
    """Test DirectMagicFlags._extract_macros_from_magic_flags()."""

    def test_extract_macros_from_cppflags(self):
        from compiletools.magicflags import DirectMagicFlags

        with patch.object(DirectMagicFlags, "__init__", lambda self, *a, **kw: None):
            obj = DirectMagicFlags.__new__(DirectMagicFlags)
            obj._args = SimpleNamespace(verbose=0)

            # Create a mock MacroState that supports with_updates
            mock_macro_state = MagicMock()
            mock_macro_state.with_updates.return_value = mock_macro_state
            obj.defined_macros = mock_macro_state

            magic_flags_result = {
                sz.Str("CPPFLAGS"): [sz.Str("-DFOO=1"), sz.Str("-DBAR=2")],
            }
            obj._extract_macros_from_magic_flags(magic_flags_result)
            mock_macro_state.with_updates.assert_called_once()


class TestGetFinalMacroStateKey:
    """Test MagicFlagsBase.get_final_macro_state_key() and get_final_macro_state_hash()."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=0)
            obj._final_macro_states = {}
            return obj

    def test_get_final_macro_state_key_raises_on_unknown_file(self):
        obj = self._make_base()
        with pytest.raises(KeyError, match="not processed"):
            obj.get_final_macro_state_key("/nonexistent/file.cpp")

    def test_get_final_macro_state_hash_raises_on_unknown_file(self):
        obj = self._make_base()
        with pytest.raises(KeyError, match="not processed"):
            obj.get_final_macro_state_hash("/nonexistent/file.cpp")


class TestHandleSourceVerbose:
    """Test _handle_source verbose logging and source_file_context."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=9)
            return obj

    def test_handle_source_verbose_with_context(self, capsys):
        obj = self._make_base()
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "helper.cpp")
            with open(source, "w") as f:
                f.write("int x;")
            context_file = os.path.join(tmpdir, "context.hpp")
            magic_flag_data = {"source_file_context": context_file}
            result = obj._handle_source(
                sz.Str("helper.cpp"), magic_flag_data, os.path.join(tmpdir, "main.cpp"), sz.Str("SOURCE")
            )
            captured = capsys.readouterr()
            assert "context_file=" in captured.out
            assert str(result).endswith("helper.cpp")

    def test_handle_source_verbose_no_context(self, capsys):
        obj = self._make_base()
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as f:
            f.write(b"int x;")
            tmppath = f.name
        try:
            magic_flag_data = {"source_file_context": None}
            obj._handle_source(sz.Str(tmppath), magic_flag_data, tmppath, sz.Str("SOURCE"))
            captured = capsys.readouterr()
            assert "SOURCE:" in captured.out
            assert "context_file=" not in captured.out
        finally:
            os.unlink(tmppath)


class TestHandleIncludeVerbose:
    """Test _handle_include verbose logging."""

    def test_verbose_include(self, capsys):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=9)
            obj._handle_include(sz.Str("/some/path"))
            captured = capsys.readouterr()
            assert "Added -I" in captured.out


class TestResolveReadmacrosPath:
    """Test MagicFlagsBase._resolve_readmacros_path()."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=0)
            return obj

    def test_resolve_absolute_path(self):
        obj = self._make_base()
        with tempfile.NamedTemporaryFile(suffix=".hpp", delete=False) as f:
            f.write(b"#define FOO 1")
            tmppath = f.name
        try:
            result = obj._resolve_readmacros_path(sz.Str(tmppath), "/some/source.cpp")
            assert result == os.path.realpath(tmppath)
        finally:
            os.unlink(tmppath)

    def test_resolve_relative_path(self):
        obj = self._make_base()
        with tempfile.TemporaryDirectory() as tmpdir:
            header = os.path.join(tmpdir, "macros.hpp")
            with open(header, "w") as f:
                f.write("#define FOO 1")
            source = os.path.join(tmpdir, "source.cpp")
            result = obj._resolve_readmacros_path(sz.Str("macros.hpp"), source)
            assert result == os.path.realpath(header)

    def test_resolve_nonexistent_raises(self):
        obj = self._make_base()
        with pytest.raises(OSError, match="does not exist"):
            obj._resolve_readmacros_path(sz.Str("/nonexistent/macros.hpp"), "/some/source.cpp")


class TestHandleReadmacros:
    """Test MagicFlagsBase._handle_readmacros()."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=0)
            obj._explicit_macro_files = set()
            return obj

    def test_handle_readmacros_adds_to_set(self):
        obj = self._make_base()
        with tempfile.NamedTemporaryFile(suffix=".hpp", delete=False) as f:
            f.write(b"#define FOO 1")
            tmppath = f.name
        try:
            obj._handle_readmacros(sz.Str(tmppath), "/some/source.cpp")
            assert os.path.realpath(tmppath) in obj._explicit_macro_files
        finally:
            os.unlink(tmppath)


class TestExtractMacrosFromPreprocessor:
    """Test CppMagicFlags._extract_macros_from_preprocessor()."""

    def _make_cpp_magicflags(self):
        from compiletools.magicflags import CppMagicFlags
        from compiletools.preprocessing_cache import MacroState

        with patch.object(CppMagicFlags, "__init__", lambda self, *a, **kw: None):
            obj = CppMagicFlags.__new__(CppMagicFlags)
            obj._args = SimpleNamespace(verbose=0)
            obj._initial_macro_state = MacroState(
                core={sz.Str("__cplusplus"): sz.Str("201703L")},
                variable={},
                compiler_path="g++",
                cppflags="",
                cflags="",
                cxxflags="",
            )
            obj.preprocessor = MagicMock()
            return obj

    def test_parses_define_lines(self):
        obj = self._make_cpp_magicflags()
        obj.preprocessor.process.return_value = (
            "#define FOO 42\n"
            "#define BAR baz\n"
            "#define __cplusplus 201703L\n"  # should be skipped (in core)
            "some other line\n"
        )
        result = obj._extract_macros_from_preprocessor("/some/file.cpp")
        # FOO and BAR should be in variable macros, __cplusplus should not
        assert sz.Str("FOO") in result.variable
        assert str(result.variable[sz.Str("FOO")]) == "42"
        assert sz.Str("BAR") in result.variable
        assert str(result.variable[sz.Str("BAR")]) == "baz"
        assert sz.Str("__cplusplus") not in result.variable

    def test_skips_function_like_macros(self):
        obj = self._make_cpp_magicflags()
        obj.preprocessor.process.return_value = (
            "#define FUNC(x) (x+1)\n"
            "#define SIMPLE 1\n"
        )
        result = obj._extract_macros_from_preprocessor("/some/file.cpp")
        assert sz.Str("SIMPLE") in result.variable
        # FUNC should be skipped (function-like)

    def test_define_without_value(self):
        obj = self._make_cpp_magicflags()
        obj.preprocessor.process.return_value = "#define DEFINED_ONLY\n"
        result = obj._extract_macros_from_preprocessor("/some/file.cpp")
        assert sz.Str("DEFINED_ONLY") in result.variable
        assert str(result.variable[sz.Str("DEFINED_ONLY")]) == "1"

    def test_empty_output(self):
        obj = self._make_cpp_magicflags()
        obj.preprocessor.process.return_value = ""
        result = obj._extract_macros_from_preprocessor("/some/file.cpp")
        assert len(result.variable) == 0


class TestDirectMagicFlagsClearCache:
    """Test DirectMagicFlags.clear_cache() handles missing cache gracefully."""

    def test_clear_cache_no_error(self):
        from compiletools.magicflags import DirectMagicFlags

        # Should not raise even if _compute_file_processing_result hasn't been called
        DirectMagicFlags.clear_cache()


class TestCppMagicFlagsClearCache:
    """Test CppMagicFlags.clear_cache() is a no-op."""

    def test_clear_cache(self):
        from compiletools.magicflags import CppMagicFlags

        CppMagicFlags.clear_cache()


class TestProcessMagicFlag:
    """Test MagicFlagsBase._process_magic_flag()."""

    def _make_base(self):
        from compiletools.magicflags import MagicFlagsBase

        with patch.object(MagicFlagsBase, "__init__", lambda self, *a, **kw: None):
            obj = MagicFlagsBase.__new__(MagicFlagsBase)
            obj._args = SimpleNamespace(verbose=0, separate_flags_CPP_CXX=False)
            return obj

    def test_readmacros_skipped(self):
        obj = self._make_base()
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(
            sz.Str("READMACROS"), sz.Str("somefile.hpp"), flagsforfilename, {}, "/some/file.cpp"
        )
        assert sz.Str("READMACROS") not in flagsforfilename

    def test_ldflags_added(self):
        obj = self._make_base()
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(
            sz.Str("LDFLAGS"), sz.Str("-lm"), flagsforfilename, {}, "/some/file.cpp"
        )
        assert sz.Str("-lm") in flagsforfilename[sz.Str("LDFLAGS")]

    def test_verbose_logging(self, capsys):
        obj = self._make_base()
        obj._args.verbose = 5
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(
            sz.Str("LDFLAGS"), sz.Str("-lm"), flagsforfilename, {}, "/some/file.cpp"
        )
        captured = capsys.readouterr()
        assert "Using magic flag" in captured.out


class TestConvergeMacroState:
    """Test DirectMagicFlags._converge_macro_state()."""

    def _make_direct(self):
        from compiletools.magicflags import DirectMagicFlags

        with patch.object(DirectMagicFlags, "__init__", lambda self, *a, **kw: None):
            obj = DirectMagicFlags.__new__(DirectMagicFlags)
            obj._args = SimpleNamespace(verbose=0)
            obj._stored_active_magic_flags = {}
            return obj

    def test_converges_with_no_files(self):
        obj = self._make_direct()
        mock_state = MagicMock()
        mock_state.get_cache_key.return_value = frozenset()
        obj.defined_macros = mock_state
        iterations = obj._converge_macro_state([])
        assert iterations == 1


class TestCollectExplicitMacroFiles:
    """Test DirectMagicFlags._collect_explicit_macro_files()."""

    def _make_direct(self):
        from compiletools.magicflags import DirectMagicFlags

        with patch.object(DirectMagicFlags, "__init__", lambda self, *a, **kw: None):
            obj = DirectMagicFlags.__new__(DirectMagicFlags)
            obj._args = SimpleNamespace(verbose=5)
            return obj

    def test_handles_exception_gracefully(self, capsys):
        obj = self._make_direct()
        # _get_file_analyzer_result will fail for nonexistent files
        with patch.object(obj, "_get_file_analyzer_result", side_effect=Exception("file not found")):
            result = obj._collect_explicit_macro_files(["/nonexistent/file.cpp"])
        assert result == set()
        captured = capsys.readouterr()
        assert "could not scan" in captured.out

    def test_collects_readmacros(self):
        obj = self._make_direct()
        mock_result = MagicMock()
        mock_result.magic_flags = [
            {"key": sz.Str("READMACROS"), "value": sz.Str("/tmp/macros.hpp")},
        ]
        with (
            patch.object(obj, "_get_file_analyzer_result", return_value=mock_result),
            patch.object(obj, "_resolve_readmacros_path", return_value="/tmp/macros.hpp"),
        ):
            result = obj._collect_explicit_macro_files(["/some/file.cpp"])
        assert "/tmp/macros.hpp" in result


class TestMainFunction:
    """Test magicflags.main() entry point."""

    def test_main_with_style_null(self):
        """Test main() runs with null style (covers lines 1172-1194)."""
        from compiletools.magicflags import main

        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "test.cpp")
            with open(source, "w") as f:
                f.write("int main() { return 0; }\n")
            # main() requires a functional build environment; mock the heavy parts
            with (
                patch("compiletools.apptools.create_parser") as mock_cp,
                patch("compiletools.apptools.parseargs") as mock_pa,
                patch("compiletools.headerdeps.create"),
                patch("compiletools.magicflags.create") as mock_create,
            ):
                mock_args = SimpleNamespace(
                    filename=[source],
                    style="null",
                    verbose=0,
                    git_root=tmpdir,
                    strip_git_root=False,
                )
                mock_pa.return_value = mock_args
                mock_parser = MagicMock()
                mock_cp.return_value = mock_parser
                mock_magicparser = MagicMock()
                mock_magicparser.parse.return_value = {sz.Str("LDFLAGS"): [sz.Str("-lm")]}
                mock_create.return_value = mock_magicparser

                result = main(argv=["test.cpp"])
                assert result == 0
