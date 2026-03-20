import compiletools.makefile_backend  # noqa: F401 — ensure registered
from compiletools.build_backend import available_backends, get_backend_class


class TestBackendCLIArg:
    def test_make_is_default(self):
        cls = get_backend_class("make")
        assert cls.name() == "make"

    def test_available_includes_make_and_ninja(self):
        import compiletools.ninja_backend  # noqa: F401 — ensure registered

        backends = available_backends()
        assert "make" in backends
        assert "ninja" in backends
