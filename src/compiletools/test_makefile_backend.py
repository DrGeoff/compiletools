import io
from types import SimpleNamespace
from unittest.mock import MagicMock

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.makefile_backend import MakefileBackend


class TestMakefileBackendRegistered:
    def test_registered_as_make(self):
        cls = get_backend_class("make")
        assert cls is MakefileBackend

    def test_name(self):
        assert MakefileBackend.name() == "make"

    def test_build_filename(self):
        assert MakefileBackend.build_filename() == "Makefile"


class TestMakefileGenerate:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
            filename=[],
            tests=[],
            static=[],
            dynamic=[],
            CC="gcc",
            CXX="g++",
            CFLAGS="-O2",
            CXXFLAGS="-O2",
            LD="g++",
            LDFLAGS="",
            serialisetests=False,
            build_only_changed=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_generate_writes_makefile_syntax(self):
        """generate() should produce valid Makefile syntax from a BuildGraph."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp", "foo.h"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
                order_only_deps=["/tmp/obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(
            BuildRule(
                output="build",
                inputs=["bin/foo"],
                command=None,
                rule_type="phony",
            )
        )

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".DELETE_ON_ERROR:" in content
        assert "obj/foo.o: foo.cpp foo.h" in content
        assert "| /tmp/obj" in content
        assert "g++ -c foo.cpp -o obj/foo.o" in content
        assert "bin/foo: obj/foo.o" in content
        assert ".PHONY: build" in content

    def test_generate_phony_no_recipe(self):
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".PHONY: all" in content
        assert "all: build" in content
