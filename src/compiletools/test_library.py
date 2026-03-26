"""Tests for library support across all backends.

Layered test structure:
- Layer 1: BuildGraph rule type tests (no filesystem, no compiler)
- Layer 2: build_graph() unit tests (mocked hunter/namer)
- Layer 3: Backend generate() output tests (StringIO, no filesystem/compiler)
- Layer 4: Integration tests (require compiler + build tool)
"""

from __future__ import annotations

import io
import os
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import compiletools.cake
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_graph import BuildGraph, BuildRule

# ---------------------------------------------------------------------------
# Layer 1: BuildGraph rule type tests
# ---------------------------------------------------------------------------


class TestLibraryRuleTypes:
    """Verify static_library and shared_library are valid BuildRule types."""

    def test_static_library_rule_type_valid(self):
        rule = BuildRule(
            output="lib/libfoo.a",
            inputs=["obj/foo.o"],
            command=["ar", "-src", "lib/libfoo.a", "obj/foo.o"],
            rule_type="static_library",
        )
        assert rule.rule_type == "static_library"

    def test_shared_library_rule_type_valid(self):
        rule = BuildRule(
            output="lib/libfoo.so",
            inputs=["obj/foo.o"],
            command=["g++", "-shared", "-o", "lib/libfoo.so", "obj/foo.o"],
            rule_type="shared_library",
        )
        assert rule.rule_type == "shared_library"

    def test_build_graph_rules_by_type_filters_libraries(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="lib/libfoo.a",
                inputs=["obj/foo.o"],
                command=["ar", "-src", "lib/libfoo.a", "obj/foo.o"],
                rule_type="static_library",
            )
        )
        graph.add_rule(
            BuildRule(
                output="lib/libfoo.so",
                inputs=["obj/foo.o"],
                command=["g++", "-shared", "-o", "lib/libfoo.so", "obj/foo.o"],
                rule_type="shared_library",
            )
        )

        static_rules = graph.rules_by_type("static_library")
        assert len(static_rules) == 1
        assert static_rules[0].output == "lib/libfoo.a"

        shared_rules = graph.rules_by_type("shared_library")
        assert len(shared_rules) == 1
        assert shared_rules[0].output == "lib/libfoo.so"


# ---------------------------------------------------------------------------
# Layer 2: build_graph() unit tests (mocked hunter/namer)
# ---------------------------------------------------------------------------


def _make_stub_backend_class():
    from compiletools.build_backend import BuildBackend

    class StubBackend(BuildBackend):
        def generate(self, graph, output=None):
            self.last_graph = graph

        def execute(self, target="build"):
            pass

        @staticmethod
        def name():
            return "stub_lib_test"

        @staticmethod
        def build_filename():
            return "Stubfile"

    return StubBackend


def _make_args(**overrides):
    defaults = dict(
        filename=[],
        tests=[],
        static=[],
        dynamic=[],
        verbose=0,
        objdir="/tmp/obj",
        bindir="/tmp/bin",
        git_root="",
        CC="gcc",
        CXX="g++",
        CFLAGS="-O2",
        CXXFLAGS="-O2 -std=c++17",
        LD="g++",
        LDFLAGS="",
        file_locking=False,
        serialisetests=False,
        build_only_changed=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_hunter(sources=None, headers=None, magicflags_map=None):
    hunter = MagicMock()
    hunter.huntsource = MagicMock()
    sources = sources or ["/src/mylib.cpp"]
    hunter.getsources = MagicMock(return_value=sources)
    hunter.required_source_files = MagicMock(side_effect=lambda s: sources)
    headers = headers or ["/src/mylib.h"]
    hunter.header_dependencies = MagicMock(return_value=headers)
    default_magic = magicflags_map or {}
    hunter.magicflags = MagicMock(return_value=default_magic)
    hunter.macro_state_hash = MagicMock(return_value="abcdef1234567890")
    return hunter


def _make_namer():
    namer = MagicMock()
    namer.object_pathname = MagicMock(
        side_effect=lambda f, mh, dh: f"/tmp/obj/{f.split('/')[-1].replace('.cpp', '.o')}"
    )
    namer.executable_pathname = MagicMock(
        side_effect=lambda f: f"/tmp/bin/{f.split('/')[-1].replace('.cpp', '')}"
    )
    namer.staticlibrary_pathname = MagicMock(return_value="/tmp/bin/libmylib.a")
    namer.dynamiclibrary_pathname = MagicMock(return_value="/tmp/bin/libmylib.so")
    namer.compute_dep_hash = MagicMock(return_value="dep_hash_12345")
    namer.executable_dir = MagicMock(return_value="/tmp/bin")
    return namer


def _make_backend(args=None, hunter=None):
    StubClass = _make_stub_backend_class()
    args = args or _make_args()
    hunter = hunter or _make_hunter()
    backend = StubClass(args=args, hunter=hunter)
    backend.namer = _make_namer()
    return backend


class TestBuildGraphStaticLibrary:
    """Test build_graph() creates correct rules for --static."""

    def test_build_graph_static_library_rule(self):
        args = _make_args(static=["/src/mylib.cpp"])
        hunter = _make_hunter(sources=["/src/mylib.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        static_rules = graph.rules_by_type("static_library")
        assert len(static_rules) == 1
        rule = static_rules[0]
        assert rule.output == "/tmp/bin/libmylib.a"
        assert "ar" in rule.command[0]

    def test_build_graph_shared_library_rule(self):
        args = _make_args(dynamic=["/src/mylib.cpp"])
        hunter = _make_hunter(sources=["/src/mylib.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        shared_rules = graph.rules_by_type("shared_library")
        assert len(shared_rules) == 1
        rule = shared_rules[0]
        assert rule.output == "/tmp/bin/libmylib.so"
        assert "-shared" in rule.command

    def test_build_graph_library_in_build_phony(self):
        args = _make_args(static=["/src/mylib.cpp"])
        hunter = _make_hunter(sources=["/src/mylib.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        build_rule = graph.get_rule("build")
        assert build_rule is not None
        assert "/tmp/bin/libmylib.a" in build_rule.inputs

    def test_build_graph_library_with_exe_adds_link_flags(self):
        args = _make_args(
            static=["/src/mylib.cpp"],
            filename=["/src/main.cpp"],
        )
        hunter = _make_hunter(sources=["/src/mylib.cpp", "/src/main.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        link_rules = graph.rules_by_type("link")
        assert len(link_rules) >= 1
        link_rule = link_rules[0]
        link_cmd = " ".join(link_rule.command)
        assert "-L" in link_cmd
        assert "-l" in link_cmd
        # Library output should be a dependency of the link rule
        assert "/tmp/bin/libmylib.a" in link_rule.inputs

    def test_build_graph_library_only_no_link_rules(self):
        args = _make_args(static=["/src/mylib.cpp"], filename=[], tests=[])
        hunter = _make_hunter(sources=["/src/mylib.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        link_rules = graph.rules_by_type("link")
        assert len(link_rules) == 0
        static_rules = graph.rules_by_type("static_library")
        assert len(static_rules) == 1

    def test_build_graph_static_and_dynamic_together(self):
        args = _make_args(
            static=["/src/mylib.cpp"],
            dynamic=["/src/mylib.cpp"],
        )
        hunter = _make_hunter(sources=["/src/mylib.cpp"])
        backend = _make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        static_rules = graph.rules_by_type("static_library")
        shared_rules = graph.rules_by_type("shared_library")
        assert len(static_rules) == 1
        assert len(shared_rules) == 1


# ---------------------------------------------------------------------------
# Layer 3: Backend generate() output tests (StringIO, no filesystem/compiler)
# ---------------------------------------------------------------------------


def _make_library_graph():
    """Create a BuildGraph with compile + static_library rules for testing."""
    graph = BuildGraph()
    graph.add_rule(
        BuildRule(
            output="/tmp/obj",
            inputs=[],
            command=["mkdir", "-p", "/tmp/obj"],
            rule_type="mkdir",
        )
    )
    graph.add_rule(
        BuildRule(
            output="/tmp/obj/mylib.o",
            inputs=["/src/mylib.cpp", "/src/mylib.h"],
            command=["g++", "-O2 -std=c++17", "-c", "/src/mylib.cpp", "-o", "/tmp/obj/mylib.o"],
            rule_type="compile",
            order_only_deps=["/tmp/obj"],
        )
    )
    graph.add_rule(
        BuildRule(
            output="/tmp/bin/libmylib.a",
            inputs=["/tmp/obj/mylib.o"],
            command=["ar", "-src", "/tmp/bin/libmylib.a", "/tmp/obj/mylib.o"],
            rule_type="static_library",
        )
    )
    graph.add_rule(
        BuildRule(
            output="build",
            inputs=["/tmp/bin/libmylib.a"],
            command=None,
            rule_type="phony",
        )
    )
    graph.add_rule(
        BuildRule(
            output="all",
            inputs=["build"],
            command=None,
            rule_type="phony",
        )
    )
    return graph


def _make_shared_library_graph():
    """Create a BuildGraph with compile + shared_library rules for testing."""
    graph = BuildGraph()
    graph.add_rule(
        BuildRule(
            output="/tmp/obj",
            inputs=[],
            command=["mkdir", "-p", "/tmp/obj"],
            rule_type="mkdir",
        )
    )
    graph.add_rule(
        BuildRule(
            output="/tmp/obj/mylib.o",
            inputs=["/src/mylib.cpp", "/src/mylib.h"],
            command=["g++", "-O2 -std=c++17 -fPIC", "-c", "/src/mylib.cpp", "-o", "/tmp/obj/mylib.o"],
            rule_type="compile",
            order_only_deps=["/tmp/obj"],
        )
    )
    graph.add_rule(
        BuildRule(
            output="/tmp/bin/libmylib.so",
            inputs=["/tmp/obj/mylib.o"],
            command=["g++", "-shared", "-o", "/tmp/bin/libmylib.so", "/tmp/obj/mylib.o"],
            rule_type="shared_library",
        )
    )
    graph.add_rule(
        BuildRule(
            output="build",
            inputs=["/tmp/bin/libmylib.so"],
            command=None,
            rule_type="phony",
        )
    )
    graph.add_rule(
        BuildRule(
            output="all",
            inputs=["build"],
            command=None,
            rule_type="phony",
        )
    )
    return graph


class TestNinjaLibraryOutput:
    def test_ninja_static_library_output(self):
        from compiletools.ninja_backend import NinjaBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = NinjaBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "rule static_library_cmd" in output
        assert "build /tmp/bin/libmylib.a: static_library_cmd" in output

    def test_ninja_shared_library_output(self):
        from compiletools.ninja_backend import NinjaBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = NinjaBackend(args=args, hunter=hunter)
        graph = _make_shared_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "rule shared_library_cmd" in output
        assert "build /tmp/bin/libmylib.so: shared_library_cmd" in output


class TestCMakeLibraryOutput:
    def test_cmake_static_library_output(self):
        from compiletools.cmake_backend import CMakeBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = CMakeBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "add_library(" in output
        assert "STATIC" in output

    def test_cmake_shared_library_output(self):
        from compiletools.cmake_backend import CMakeBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = CMakeBackend(args=args, hunter=hunter)
        graph = _make_shared_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "add_library(" in output
        assert "SHARED" in output


class TestBazelLibraryOutput:
    def test_bazel_static_library_output(self):
        from compiletools.bazel_backend import BazelBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = BazelBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "cc_library(" in output

    def test_bazel_shared_library_output(self):
        from compiletools.bazel_backend import BazelBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = BazelBackend(args=args, hunter=hunter)
        graph = _make_shared_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "cc_binary(" in output
        assert "linkshared = True" in output


class TestShakeLibrarySummary:
    def test_shake_static_library_summary(self):
        from compiletools.shake_backend import ShakeBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = ShakeBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "static_library" in output


class TestTupLibraryOutput:
    def test_tup_static_library_output(self):
        from compiletools.tup_backend import TupBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = TupBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "ar -src" in output


class TestMakefileLibraryOutput:
    def test_makefile_static_library_output(self):
        from compiletools.makefile_backend import MakefileBackend

        args = _make_args()
        hunter = _make_hunter()
        backend = MakefileBackend(args=args, hunter=hunter)
        graph = _make_library_graph()

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        output = buf.getvalue()

        assert "ar -src" in output


# ---------------------------------------------------------------------------
# Layer 4: Integration tests (existing + new)
# ---------------------------------------------------------------------------


class TestLibrary:
    def setup_method(self):
        pass

    @uth.requires_functional_compiler
    def test_build_and_link_static_library(self):
        with uth.TempDirContextWithChange() as tmpdir:
            # Mimic the build.sh and create the library in a 'mylib' subdirectory
            # Copy the sample source files into the test build location
            mylibdir = os.path.join(tmpdir, "mylib")
            shutil.copytree(os.path.join(uth.samplesdir(), "library/mylib"), mylibdir)

            # Add unique comments to copied files to avoid hash collision with originals
            for root, _dirs, files in os.walk(mylibdir):
                for filename in files:
                    if filename.endswith((".cpp", ".hpp")):
                        filepath = os.path.join(root, filename)
                        with open(filepath) as f:
                            content = f.read()
                        with open(filepath, "w") as f:
                            f.write(f"// Test copy: {filename}\n{content}")

            # Build the library
            temp_config_name = uth.create_temp_config(tmpdir)
            uth.create_temp_ct_conf(tmpdir, defaultvariant=temp_config_name[:-5])
            argv = [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + temp_config_name,
                "--static",
                os.path.join(tmpdir, "mylib/get_numbers.cpp"),
            ]

            with uth.DirectoryContext(mylibdir), uth.ParserContext():
                compiletools.cake.main(argv)

            # Copy the main that will link to the library into the test build location
            relativepaths = ["library/main.cpp"]
            realpaths = [os.path.join(uth.samplesdir(), filename) for filename in relativepaths]
            for ff in realpaths:
                shutil.copy2(ff, tmpdir)

            # Build the exe, linking against the library
            argv = ["--config=" + temp_config_name] + realpaths
            with uth.ParserContext():
                compiletools.cake.main(argv)

            # Check that an executable got built for each cpp
            actual_exes = set()
            for root, _dirs, files in os.walk(tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            expected_exes = {os.path.splitext(os.path.split(filename)[1])[0] for filename in relativepaths}
            assert expected_exes == actual_exes

    def teardown_method(self):
        uth.reset()
