from unittest.mock import MagicMock, patch

import pytest

import compiletools.apptools
import compiletools.bazel_backend  # noqa: F401
import compiletools.cmake_backend  # noqa: F401
import compiletools.makefile_backend  # noqa: F401
import compiletools.ninja_backend  # noqa: F401
import compiletools.shake_backend  # noqa: F401
import compiletools.tup_backend  # noqa: F401
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.testhelper import CakeTestContext


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

    @pytest.mark.parametrize("backend_name", ["make", "ninja", "bazel", "cmake", "tup", "shake"])
    def test_backend_dispatch_instantiates_correct_backend(self, backend_name):
        """--backend=X should instantiate the correct backend class."""
        with CakeTestContext(backend_name) as (cake, tmpdir):
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

    def test_backend_dispatch_generates_compilation_database(self):
        """Backend dispatch should still generate compilation database."""
        with CakeTestContext("ninja", compilation_database=True) as (cake, tmpdir):
            expected_class = get_backend_class("ninja")

            with (
                patch.object(expected_class, "build_graph", return_value=MagicMock()),
                patch.object(expected_class, "generate"),
                patch.object(expected_class, "execute"),
            ):
                cake.process()
                cake._call_compilation_database.assert_called_once()

    def test_clean_calls_backend_clean_method(self):
        """--clean should call backend.clean() instead of execute('realclean')."""
        with CakeTestContext("ninja", clean=True) as (cake, tmpdir):
            cake.args.output = tmpdir + "/out"
            expected_class = get_backend_class("ninja")

            mock_graph = MagicMock()
            mock_graph.outputs = {"build", "all"}
            with (
                patch.object(expected_class, "build_graph", return_value=mock_graph),
                patch.object(expected_class, "generate"),
                patch.object(expected_class, "clean") as mock_clean,
            ):
                cake.process()
                mock_clean.assert_called_once()

    def test_backend_dispatch_runs_tests_when_runtests_in_graph(self):
        """When args.tests is set and 'runtests' is in graph.outputs, execute('runtests') should be called."""
        with CakeTestContext("ninja", tests=["test_main.cpp"]) as (cake, tmpdir):
            expected_class = get_backend_class("ninja")

            mock_graph = MagicMock()
            mock_graph.outputs = {"build", "all", "runtests"}
            with (
                patch.object(expected_class, "build_graph", return_value=mock_graph),
                patch.object(expected_class, "generate"),
                patch.object(expected_class, "execute") as mock_execute,
            ):
                cake.process()
                calls = [c[0][0] for c in mock_execute.call_args_list]
                assert "build" in calls
                assert "runtests" in calls

    def test_static_flag_allowed_for_make_backend(self):
        """--static should not raise for --backend=make."""
        with CakeTestContext("make", static=["lib.cpp"]) as (cake, tmpdir):
            cake.hunter = MagicMock()
            cake.hunter.required_source_files = MagicMock(return_value=[])
            expected_class = get_backend_class("make")

            with (
                patch.object(compiletools.apptools, "substitutions"),
                patch.object(compiletools.apptools, "verboseprintconfig"),
                patch.object(expected_class, "build_graph", return_value=MagicMock()),
                patch.object(expected_class, "generate"),
                patch.object(expected_class, "execute"),
            ):
                cake.process()

    def test_backend_dispatch_skips_runtests_when_no_tests(self):
        """When args.tests is empty, execute('runtests') should NOT be called."""
        with CakeTestContext("ninja") as (cake, tmpdir):
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
