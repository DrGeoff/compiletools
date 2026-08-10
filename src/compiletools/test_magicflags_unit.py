"""Unit tests for magicflags.py flag handler methods and helpers."""

import os
from argparse import Namespace
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stringzilla as sz


def _make_partial(cls_name: str = "MagicFlagsBase", **args_attrs):
    """Create a minimally-mocked ``magicflags.<cls_name>`` instance for unit tests.

    ``__init__`` is patched to a no-op so the test can side-step the full
    parser/headerdeps wiring. The returned instance has only
    ``obj._args = Namespace(verbose=0, **args_attrs)`` set (finalized so
    BuildState-reading helpers like ``find_system_header`` accept it);
    the test body is responsible for any other attributes it needs
    (``defined_macros``, ``_final_macro_states``, etc.).
    """
    import compiletools.magicflags as mf
    import compiletools.testhelper as uth

    cls = getattr(mf, cls_name)
    args_attrs.setdefault("verbose", 0)
    with patch.object(cls, "__init__", lambda self, *a, **kw: None):
        obj = cls.__new__(cls)
        obj._args = Namespace(**args_attrs)
        uth.finalize_flag_state(obj._args)
        return obj


class TestHandleInclude:
    """Test MagicFlagsBase._handle_include()."""

    def _make_base(self):
        return _make_partial()

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
        return _make_partial()

    def test_handle_source_absolute(self, tmp_path):
        obj = self._make_base()
        tmpfile = tmp_path / "x.cpp"
        tmpfile.write_bytes(b"int x;")
        magic_flag_data = {"source_file_context": None}
        result = obj._handle_source(sz.Str(str(tmpfile)), magic_flag_data, "/some/main.cpp", sz.Str("SOURCE"))
        assert str(result).endswith(".cpp")

    def test_handle_source_relative(self, tmp_path):
        obj = self._make_base()
        (tmp_path / "helper.cpp").write_text("int x;")
        main_file = str(tmp_path / "main.cpp")
        magic_flag_data = {"source_file_context": None}
        result = obj._handle_source(sz.Str("helper.cpp"), magic_flag_data, main_file, sz.Str("SOURCE"))
        assert str(result).endswith("helper.cpp")

    def test_handle_source_nonexistent(self):
        obj = self._make_base()
        magic_flag_data = {"source_file_context": None}

        with pytest.raises(OSError):
            obj._handle_source(sz.Str("/nonexistent/file.cpp"), magic_flag_data, "/some/main.cpp", sz.Str("SOURCE"))


class TestExtractMacrosFromMagicFlags:
    """Test DirectMagicFlags._extract_macros_from_magic_flags()."""

    def test_extract_macros_from_cppflags(self):
        from compiletools.preprocessing_cache import MacroState

        obj = _make_partial("DirectMagicFlags")
        # Start from a real, empty MacroState so we can assert the ACTUAL
        # parsed macros end up in the resulting variable dict (not merely
        # that some method was invoked on a mock).
        obj.defined_macros = MacroState(core={}, variable={}, anchor_root="")

        magic_flags_result = {
            sz.Str("CPPFLAGS"): [sz.Str("-DFOO=1"), sz.Str("-DBAR=2")],
        }
        obj._extract_macros_from_magic_flags(magic_flags_result)

        variable = obj.defined_macros.variable
        assert sz.Str("FOO") in variable
        assert sz.Str("BAR") in variable
        assert str(variable[sz.Str("FOO")]) == "1"
        assert str(variable[sz.Str("BAR")]) == "2"


class TestGetFinalMacroStateKey:
    """Test MagicFlagsBase.get_final_macro_state_key() and get_final_macro_state_hash()."""

    def _make_base(self):
        obj = _make_partial()
        obj._final_macro_states = {}
        return obj

    @pytest.mark.parametrize(
        "method_name",
        [
            pytest.param("get_final_macro_state_key", id="key"),
            pytest.param("get_final_macro_state_hash", id="hash"),
        ],
    )
    def test_get_final_macro_state_raises_on_unknown_file(self, method_name):
        obj = self._make_base()
        with pytest.raises(KeyError, match="not processed"):
            getattr(obj, method_name)("/nonexistent/file.cpp")


class TestHandleSourceVerbose:
    """Test _handle_source verbose logging and source_file_context."""

    def _make_base(self):
        return _make_partial(verbose=9)

    def test_handle_source_verbose_with_context(self, tmp_path, capsys):
        obj = self._make_base()
        (tmp_path / "helper.cpp").write_text("int x;")
        context_file = str(tmp_path / "context.hpp")
        magic_flag_data = {"source_file_context": context_file}
        result = obj._handle_source(sz.Str("helper.cpp"), magic_flag_data, str(tmp_path / "main.cpp"), sz.Str("SOURCE"))
        captured = capsys.readouterr()
        assert "context_file=" in captured.out
        assert str(result).endswith("helper.cpp")

    def test_handle_source_verbose_no_context(self, tmp_path, capsys):
        obj = self._make_base()
        tmpfile = tmp_path / "x.cpp"
        tmpfile.write_bytes(b"int x;")
        tmppath = str(tmpfile)
        magic_flag_data = {"source_file_context": None}
        obj._handle_source(sz.Str(tmppath), magic_flag_data, tmppath, sz.Str("SOURCE"))
        captured = capsys.readouterr()
        assert "SOURCE:" in captured.out
        assert "context_file=" not in captured.out


class TestHandleIncludeVerbose:
    """Test _handle_include verbose logging."""

    def test_verbose_include(self, capsys):
        obj = _make_partial(verbose=9)
        obj._handle_include(sz.Str("/some/path"))
        captured = capsys.readouterr()
        assert "Added -I" in captured.out


class TestResolveReadmacrosPath:
    """Test MagicFlagsBase._resolve_readmacros_path()."""

    def _make_base(self):
        return _make_partial()

    def test_resolve_absolute_path(self, tmp_path):
        obj = self._make_base()
        tmpfile = tmp_path / "x.hpp"
        tmpfile.write_bytes(b"#define FOO 1")
        tmppath = str(tmpfile)
        result = obj._resolve_readmacros_path(sz.Str(tmppath), "/some/source.cpp")
        assert result == os.path.realpath(tmppath)

    def test_resolve_relative_path(self, tmp_path):
        obj = self._make_base()
        header = tmp_path / "macros.hpp"
        header.write_text("#define FOO 1")
        source = str(tmp_path / "source.cpp")
        result = obj._resolve_readmacros_path(sz.Str("macros.hpp"), source)
        assert result == os.path.realpath(str(header))

    def test_resolve_nonexistent_raises(self):
        obj = self._make_base()
        with pytest.raises(OSError, match="does not exist"):
            obj._resolve_readmacros_path(sz.Str("/nonexistent/macros.hpp"), "/some/source.cpp")

    def test_resolve_via_extra_include_paths(self, tmp_path):
        obj = self._make_base()
        incdir = tmp_path / "vendor"
        incdir.mkdir()
        (incdir / "macros.hpp").write_text("#define FOO 1")
        source = str(tmp_path / "src" / "source.cpp")

        result = obj._resolve_readmacros_path(sz.Str("macros.hpp"), source, extra_include_paths=[str(incdir)])

        assert result == os.path.realpath(str(incdir / "macros.hpp"))

    def test_extra_include_paths_searched_in_order(self, tmp_path):
        obj = self._make_base()
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        (first / "macros.hpp").write_text("#define FOO 1")
        (second / "macros.hpp").write_text("#define FOO 2")

        result = obj._resolve_readmacros_path(
            sz.Str("macros.hpp"), str(tmp_path / "source.cpp"), extra_include_paths=[str(first), str(second)]
        )

        assert result == os.path.realpath(str(first / "macros.hpp"))

    def test_global_include_path_beats_file_declared(self, tmp_path):
        """Global -I is searched before the file's own declarations.

        This mirrors the real command line, where per-file magic flags are
        appended after the global flags.
        """
        globaldir = tmp_path / "global"
        filedir = tmp_path / "filedeclared"
        globaldir.mkdir()
        filedir.mkdir()
        (globaldir / "macros.hpp").write_text("#define FOO 1")
        (filedir / "macros.hpp").write_text("#define FOO 2")

        obj = _make_partial(CPPFLAGS=f"-I{globaldir}", CFLAGS="", CXXFLAGS="", INCLUDE="")

        result = obj._resolve_readmacros_path(
            sz.Str("macros.hpp"), str(tmp_path / "source.cpp"), extra_include_paths=[str(filedir)]
        )

        assert result == os.path.realpath(str(globaldir / "macros.hpp"))


class TestFileDeclaredIncludePaths:
    """Test MagicFlagsBase._file_declared_include_paths()."""

    @staticmethod
    def _analysis(*pairs):
        return SimpleNamespace(magic_flags=[{"key": sz.Str(key), "value": sz.Str(value)} for key, value in pairs])

    def test_harvests_isystem_and_I_from_compile_slots(self):
        obj = _make_partial()
        analysis = self._analysis(
            ("CXXFLAGS", "-isystem /opt/a"),
            ("CPPFLAGS", "-I/opt/b"),
            ("CFLAGS", "-I /opt/c"),
        )

        assert obj._file_declared_include_paths(analysis) == ["/opt/a", "/opt/b", "/opt/c"]

    def test_include_magic_value_is_the_directory(self):
        obj = _make_partial()
        analysis = self._analysis(("INCLUDE", "/opt/vendor"))

        assert obj._file_declared_include_paths(analysis) == ["/opt/vendor"]

    def test_unrelated_keys_and_non_path_flags_contribute_nothing(self):
        obj = _make_partial()
        analysis = self._analysis(
            ("CXXFLAGS", "-DFOO=1 -O2"),
            ("LDFLAGS", "-L/opt/lib -lfoo"),
            ("SOURCE", "helper.cpp"),
            ("READMACROS", "version.hpp"),
        )

        assert obj._file_declared_include_paths(analysis) == []

    def test_duplicates_dropped_first_occurrence_wins(self):
        obj = _make_partial()
        analysis = self._analysis(
            ("CXXFLAGS", "-I/opt/a -I/opt/b"),
            ("CPPFLAGS", "-isystem /opt/a"),
        )

        assert obj._file_declared_include_paths(analysis) == ["/opt/a", "/opt/b"]

    def test_malformed_value_is_skipped_not_raised(self):
        """A tokenize failure here must not abort discovery.

        The authoritative diagnostic for a malformed value comes from
        _process_magic_flag; path discovery just moves on.
        """
        obj = _make_partial()
        analysis = self._analysis(
            ("CXXFLAGS", '-DFOO="bar'),
            ("CPPFLAGS", "-I/opt/good"),
        )

        assert obj._file_declared_include_paths(analysis) == ["/opt/good"]


class TestHandleReadmacros:
    """Test MagicFlagsBase._handle_readmacros()."""

    def _make_base(self):
        obj = _make_partial()
        obj._explicit_macro_files = set()
        return obj

    def test_handle_readmacros_adds_to_set(self, tmp_path):
        obj = self._make_base()
        tmpfile = tmp_path / "x.hpp"
        tmpfile.write_bytes(b"#define FOO 1")
        tmppath = str(tmpfile)
        obj._handle_readmacros(sz.Str(tmppath), "/some/source.cpp")
        assert os.path.realpath(tmppath) in obj._explicit_macro_files


class TestDirectMagicFlagsClearCache:
    """Test DirectMagicFlags.clear_cache() is a documented no-op.

    The subclass ``clear_cache`` is a genuine no-op ("Instance caches are
    per-instance; nothing class-level to clear"). The shared module-level
    caches are cleared by the *base* ``MagicFlagsBase.clear_cache`` (the
    aggregator), not the subclass. These tests assert that division of
    responsibility observably: the subclass leaves shared cache state
    intact, and the base aggregator is what actually empties it.
    """

    def test_clear_cache_is_a_noop_leaving_shared_caches_intact(self):
        import compiletools.utils
        from compiletools.magicflags import DirectMagicFlags

        compiletools.utils.split_command_cached_sz.cache_clear()
        # Populate the shared LRU cache so we have an observable postcondition.
        compiletools.utils.split_command_cached_sz(sz.Str("-DFOO=1 -DBAR=2"))
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0

        # Subclass no-op MUST NOT touch the shared cache.
        DirectMagicFlags.clear_cache()
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0


class TestCppMagicFlagsClearCache:
    """Test CppMagicFlags.clear_cache() is a documented no-op."""

    def test_clear_cache_is_a_noop_leaving_shared_caches_intact(self):
        import compiletools.utils
        from compiletools.magicflags import CppMagicFlags

        compiletools.utils.split_command_cached_sz.cache_clear()
        compiletools.utils.split_command_cached_sz(sz.Str("-DFOO=1 -DBAR=2"))
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0

        # Subclass no-op MUST NOT touch the shared cache.
        CppMagicFlags.clear_cache()
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0

    def test_base_aggregator_actually_clears_shared_caches(self):
        import compiletools.utils
        from compiletools.magicflags import MagicFlagsBase

        compiletools.utils.split_command_cached_sz.cache_clear()
        compiletools.utils.split_command_cached_sz(sz.Str("-DFOO=1 -DBAR=2"))
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0

        # The base aggregator IS what empties the shared LRU caches.
        MagicFlagsBase.clear_cache()
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize == 0


class TestProcessMagicFlag:
    """Test MagicFlagsBase._process_magic_flag()."""

    def _make_base(self):
        return _make_partial(separate_flags_CPP_CXX=False)

    def test_readmacros_skipped(self):
        obj = self._make_base()
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(sz.Str("READMACROS"), sz.Str("somefile.hpp"), flagsforfilename, {}, "/some/file.cpp")
        assert sz.Str("READMACROS") not in flagsforfilename

    def test_ldflags_added(self):
        obj = self._make_base()
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(sz.Str("LDFLAGS"), sz.Str("-lm"), flagsforfilename, {}, "/some/file.cpp")
        assert sz.Str("-lm") in flagsforfilename[sz.Str("LDFLAGS")]

    def test_verbose_logging(self, capsys):
        obj = self._make_base()
        obj._args.verbose = 5
        flagsforfilename = defaultdict(list)
        obj._process_magic_flag(sz.Str("LDFLAGS"), sz.Str("-lm"), flagsforfilename, {}, "/some/file.cpp")
        captured = capsys.readouterr()
        assert "Using magic flag" in captured.out


class TestConvergeMacroState:
    """Test DirectMagicFlags._converge_macro_state()."""

    def _make_direct(self):
        obj = _make_partial("DirectMagicFlags")
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
        return _make_partial("DirectMagicFlags", verbose=5)

    def test_handles_exception_gracefully(self, capsys):
        obj = self._make_direct()
        # _get_file_analyzer_result raises OSError (FileNotFoundError) for nonexistent files
        with patch.object(obj, "_get_file_analyzer_result", side_effect=FileNotFoundError("file not found")):
            result = obj._collect_explicit_macro_files(["/nonexistent/file.cpp"])
        assert result == set()
        captured = capsys.readouterr()
        assert "could not scan" in captured.err

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

    def test_main_with_style_null(self, tmp_path):
        """Test main() runs with null style (covers lines 1172-1194)."""
        from compiletools.magicflags import main

        source = tmp_path / "test.cpp"
        source.write_text("int main() { return 0; }\n")
        # main() requires a functional build environment; mock the heavy parts
        with (
            patch("compiletools.apptools.create_parser") as mock_cp,
            patch("compiletools.apptools.parseargs") as mock_pa,
            patch("compiletools.headerdeps.create"),
            patch("compiletools.magicflags.create") as mock_create,
        ):
            mock_args = SimpleNamespace(
                filename=[str(source)],
                style="null",
                verbose=0,
                git_root=str(tmp_path),
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
