from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from compiletools.build_backend import available_backends, get_backend_class
from compiletools.cake import Cake


class TestBackendCLIArg:
    def test_make_is_default(self):
        cls = get_backend_class("make")
        assert cls.name() == "make"

    def test_available_includes_all_backends(self):
        backends = available_backends()
        assert "make" in backends
        assert "ninja" in backends
        assert "shake" in backends
        assert "bazel" in backends
        assert "cmake" in backends
        assert "tup" in backends


class TestCakeBackendDispatch:
    """Verify that cake.process() dispatches to the correct backend."""

    def _make_args(self, backend="make"):
        return SimpleNamespace(
            backend=backend,
            filename=["main.cpp"],
            tests=None,
            static=[],
            dynamic=[],
            auto=False,
            filelist=False,
            verbose=0,
            clean=False,
            output=None,
            compilation_database=False,
            makefilename="Makefile",
            objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            CC="gcc",
            CXX="g++",
            CFLAGS="-O2",
            CXXFLAGS="-O2",
            LD="g++",
            LDFLAGS="",
            file_locking=False,
            serialisetests=False,
            build_only_changed=None,
            parallel=1,
        )

    @pytest.mark.parametrize("backend_name", ["make", "ninja", "bazel", "cmake", "tup", "shake"])
    def test_backend_dispatch_instantiates_correct_backend(self, backend_name, tmp_path):
        """--backend=X should instantiate the correct backend class."""
        args = self._make_args(backend=backend_name)
        cake = Cake(args)
        cake._createctobjs = MagicMock()
        cake._call_compilation_database = MagicMock()
        cake._copyexes = MagicMock()
        namer = MagicMock()
        namer.executable_dir.return_value = str(tmp_path / "exe")
        namer.topbindir.return_value = str(tmp_path / "bin")
        cake.namer = namer

        expected_class = get_backend_class(backend_name)

        with (
            patch.object(expected_class, "build_graph") as mock_build_graph,
            patch.object(expected_class, "generate") as mock_generate,
            patch.object(expected_class, "execute") as mock_execute,
        ):
            mock_build_graph.return_value = MagicMock()
            cake.process()

            mock_build_graph.assert_called_once()
            mock_generate.assert_called_once()
            mock_execute.assert_called()

    def test_backend_dispatch_generates_compilation_database(self, tmp_path):
        """Backend dispatch should still generate compilation database."""
        args = self._make_args(backend="ninja")
        args.compilation_database = True
        cake = Cake(args)
        cake._createctobjs = MagicMock()
        cake._call_compilation_database = MagicMock()
        cake._copyexes = MagicMock()
        namer = MagicMock()
        namer.executable_dir.return_value = str(tmp_path / "exe")
        namer.topbindir.return_value = str(tmp_path / "bin")
        cake.namer = namer

        expected_class = get_backend_class("ninja")

        with (
            patch.object(expected_class, "build_graph", return_value=MagicMock()),
            patch.object(expected_class, "generate"),
            patch.object(expected_class, "execute"),
        ):
            cake.process()
            cake._call_compilation_database.assert_called_once()

    def test_backend_dispatch_clean_passes_realclean(self, tmp_path):
        """--clean should pass 'realclean' target to backend.execute()."""
        args = self._make_args(backend="ninja")
        args.clean = True
        args.output = str(tmp_path / "out")  # Use --output to avoid os.listdir on mock
        cake = Cake(args)
        cake._createctobjs = MagicMock()
        cake._call_compilation_database = MagicMock()
        namer = MagicMock()
        namer.executable_dir.return_value = str(tmp_path / "exe")
        cake.namer = namer

        expected_class = get_backend_class("ninja")

        mock_graph = MagicMock()
        mock_graph.outputs = {"realclean", "build", "all"}
        with (
            patch.object(expected_class, "build_graph", return_value=mock_graph),
            patch.object(expected_class, "generate"),
            patch.object(expected_class, "execute") as mock_execute,
        ):
            cake.process()
            mock_execute.assert_called_once_with("realclean")

    def test_backend_dispatch_runs_tests_when_runtests_in_graph(self, tmp_path):
        """When args.tests is set and 'runtests' is in graph.outputs, execute('runtests') should be called."""
        args = self._make_args(backend="ninja")
        args.tests = ["test_main.cpp"]
        cake = Cake(args)
        cake._createctobjs = MagicMock()
        cake._call_compilation_database = MagicMock()
        cake._copyexes = MagicMock()
        namer = MagicMock()
        namer.executable_dir.return_value = str(tmp_path / "exe")
        namer.topbindir.return_value = str(tmp_path / "bin")
        cake.namer = namer

        expected_class = get_backend_class("ninja")

        mock_graph = MagicMock()
        mock_graph.outputs = {"build", "all", "runtests"}
        with (
            patch.object(expected_class, "build_graph", return_value=mock_graph),
            patch.object(expected_class, "generate"),
            patch.object(expected_class, "execute") as mock_execute,
        ):
            cake.process()
            # Should have called execute("build") and execute("runtests")
            calls = [c[0][0] for c in mock_execute.call_args_list]
            assert "build" in calls
            assert "runtests" in calls

    def test_backend_dispatch_skips_runtests_when_no_tests(self, tmp_path):
        """When args.tests is empty, execute('runtests') should NOT be called."""
        args = self._make_args(backend="ninja")
        args.tests = None
        cake = Cake(args)
        cake._createctobjs = MagicMock()
        cake._call_compilation_database = MagicMock()
        cake._copyexes = MagicMock()
        namer = MagicMock()
        namer.executable_dir.return_value = str(tmp_path / "exe")
        namer.topbindir.return_value = str(tmp_path / "bin")
        cake.namer = namer

        expected_class = get_backend_class("ninja")

        mock_graph = MagicMock()
        mock_graph.outputs = {"build", "all"}
        with (
            patch.object(expected_class, "build_graph", return_value=mock_graph),
            patch.object(expected_class, "generate"),
            patch.object(expected_class, "execute") as mock_execute,
        ):
            cake.process()
            calls = [c[0][0] for c in mock_execute.call_args_list]
            assert "build" in calls
            assert "runtests" not in calls
