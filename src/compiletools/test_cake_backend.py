import compiletools.makefile_backend  # noqa: F401 — ensure registered
import compiletools.ninja_backend  # noqa: F401 — ensure registered
import compiletools.shake_backend  # noqa: F401 — ensure registered
import compiletools.bazel_backend  # noqa: F401 — ensure registered
import compiletools.cmake_backend  # noqa: F401 — ensure registered
import compiletools.tup_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class


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
