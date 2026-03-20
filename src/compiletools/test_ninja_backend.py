import io
from types import SimpleNamespace
from unittest.mock import MagicMock

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.ninja_backend import NinjaBackend


class TestNinjaBackendRegistered:
    def test_registered_as_ninja(self):
        cls = get_backend_class("ninja")
        assert cls is NinjaBackend

    def test_name(self):
        assert NinjaBackend.name() == "ninja"

    def test_build_filename(self):
        assert NinjaBackend.build_filename() == "build.ninja"


class TestNinjaGenerate:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            shared_objects=False,
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

    def test_generate_writes_ninja_syntax(self):
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
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Ninja uses "build <output>: <rule> <inputs>" syntax
        assert "build obj/foo.o: compile_cmd foo.cpp" in content
        assert "build bin/foo: link_cmd obj/foo.o" in content
        # Ninja uses "build <alias>: phony <deps>" for phony targets
        assert "build build: phony bin/foo" in content
        # Order-only deps use || in Ninja
        assert "|| /tmp/obj" in content

    def test_ninja_rule_definitions(self):
        """Ninja requires rule definitions (rule compile_cmd / rule link_cmd)."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Should define a Ninja rule with command variable
        assert "rule compile_cmd" in content
        assert "command = $cmd" in content
