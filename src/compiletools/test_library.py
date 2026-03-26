"""Tests for library support across all backends.

Layered test structure:
- Layer 1: BuildGraph rule type tests (no filesystem, no compiler)
- Layer 2: build_graph() unit tests (mocked hunter/namer)
- Layer 3: Backend generate() output tests (StringIO, no filesystem/compiler)
- Layer 4: Integration tests (require compiler + build tool)
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil

import compiletools.cake
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.testhelper import (
    TempDirContextNoChange,
    make_backend_args,
    make_mock_hunter,
    make_mock_namer,
    make_stub_backend_class,
)

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


@contextlib.contextmanager
def _library_backend_context(args_overrides=None, hunter_overrides=None):
    """Context manager for library test backends."""
    with TempDirContextNoChange() as tmpdir:
        args_kw = dict(CXXFLAGS="-O2 -std=c++17")
        if args_overrides:
            args_kw.update(args_overrides)
        args = make_backend_args(tmpdir, **args_kw)

        hunter_kw = dict(sources=["/src/mylib.cpp"], headers=["/src/mylib.h"])
        if hunter_overrides:
            hunter_kw.update(hunter_overrides)
        hunter = make_mock_hunter(**hunter_kw)

        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        yield backend, args, tmpdir


class TestBuildGraphStaticLibrary:
    """Test build_graph() creates correct rules for --static."""

    def test_build_graph_static_library_rule(self):
        with _library_backend_context(
            args_overrides=dict(static=["/src/mylib.cpp"]),
            hunter_overrides=dict(sources=["/src/mylib.cpp"]),
        ) as (backend, args, _tmpdir):
            graph = backend.build_graph()

            static_rules = graph.rules_by_type("static_library")
            assert len(static_rules) == 1
            rule = static_rules[0]
            assert rule.output == f"{args.bindir}/libmylib.a"
            assert "ar" in rule.command[0]

    def test_build_graph_shared_library_rule(self):
        with _library_backend_context(
            args_overrides=dict(dynamic=["/src/mylib.cpp"]),
            hunter_overrides=dict(sources=["/src/mylib.cpp"]),
        ) as (backend, args, _tmpdir):
            graph = backend.build_graph()

            shared_rules = graph.rules_by_type("shared_library")
            assert len(shared_rules) == 1
            rule = shared_rules[0]
            assert rule.output == f"{args.bindir}/libmylib.so"
            assert "-shared" in rule.command

    def test_build_graph_library_in_build_phony(self):
        with _library_backend_context(
            args_overrides=dict(static=["/src/mylib.cpp"]),
            hunter_overrides=dict(sources=["/src/mylib.cpp"]),
        ) as (backend, args, _tmpdir):
            graph = backend.build_graph()

            build_rule = graph.get_rule("build")
            assert build_rule is not None
            assert f"{args.bindir}/libmylib.a" in build_rule.inputs

    def test_build_graph_library_with_exe_adds_link_flags(self):
        with _library_backend_context(
            args_overrides=dict(
                static=["/src/mylib.cpp"],
                filename=["/src/main.cpp"],
            ),
            hunter_overrides=dict(sources=["/src/mylib.cpp", "/src/main.cpp"]),
        ) as (backend, args, _tmpdir):
            graph = backend.build_graph()

            link_rules = graph.rules_by_type("link")
            assert len(link_rules) >= 1
            link_rule = link_rules[0]
            link_cmd = " ".join(link_rule.command)
            assert "-L" in link_cmd
            assert "-l" in link_cmd
            # Library output should be a dependency of the link rule
            assert f"{args.bindir}/libmylib.a" in link_rule.inputs

    def test_build_graph_library_only_no_link_rules(self):
        with _library_backend_context(
            args_overrides=dict(static=["/src/mylib.cpp"], filename=[], tests=[]),
            hunter_overrides=dict(sources=["/src/mylib.cpp"]),
        ) as (backend, _args, _tmpdir):
            graph = backend.build_graph()

            link_rules = graph.rules_by_type("link")
            assert len(link_rules) == 0
            static_rules = graph.rules_by_type("static_library")
            assert len(static_rules) == 1

    def test_build_graph_static_and_dynamic_together(self):
        with _library_backend_context(
            args_overrides=dict(
                static=["/src/mylib.cpp"],
                dynamic=["/src/mylib.cpp"],
            ),
            hunter_overrides=dict(sources=["/src/mylib.cpp"]),
        ) as (backend, _args, _tmpdir):
            graph = backend.build_graph()

            static_rules = graph.rules_by_type("static_library")
            shared_rules = graph.rules_by_type("shared_library")
            assert len(static_rules) == 1
            assert len(shared_rules) == 1


# ---------------------------------------------------------------------------
# Layer 3: Backend generate() output tests (StringIO, no filesystem/compiler)
# ---------------------------------------------------------------------------


def _make_library_graph(objdir, bindir):
    """Create a BuildGraph with compile + static_library rules for testing."""
    graph = BuildGraph()
    graph.add_rule(BuildRule(output=objdir, inputs=[], command=["mkdir", "-p", objdir], rule_type="mkdir"))
    obj = f"{objdir}/mylib.o"
    lib = f"{bindir}/libmylib.a"
    graph.add_rule(
        BuildRule(
            output=obj,
            inputs=["/src/mylib.cpp", "/src/mylib.h"],
            command=["g++", "-O2 -std=c++17", "-c", "/src/mylib.cpp", "-o", obj],
            rule_type="compile",
            order_only_deps=[objdir],
        )
    )
    graph.add_rule(BuildRule(output=lib, inputs=[obj], command=["ar", "-src", lib, obj], rule_type="static_library"))
    graph.add_rule(BuildRule(output="build", inputs=[lib], command=None, rule_type="phony"))
    graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))
    return graph


def _make_shared_library_graph(objdir, bindir):
    """Create a BuildGraph with compile + shared_library rules for testing."""
    graph = BuildGraph()
    graph.add_rule(BuildRule(output=objdir, inputs=[], command=["mkdir", "-p", objdir], rule_type="mkdir"))
    obj = f"{objdir}/mylib.o"
    lib = f"{bindir}/libmylib.so"
    graph.add_rule(
        BuildRule(
            output=obj,
            inputs=["/src/mylib.cpp", "/src/mylib.h"],
            command=["g++", "-O2 -std=c++17 -fPIC", "-c", "/src/mylib.cpp", "-o", obj],
            rule_type="compile",
            order_only_deps=[objdir],
        )
    )
    graph.add_rule(
        BuildRule(output=lib, inputs=[obj], command=["g++", "-shared", "-o", lib, obj], rule_type="shared_library")
    )
    graph.add_rule(BuildRule(output="build", inputs=[lib], command=None, rule_type="phony"))
    graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))
    return graph


@contextlib.contextmanager
def _layer3_context(backend_class, graph_factory):
    """Context manager for Layer 3 generate() tests.

    Yields (backend, graph, output_string) after generating into a StringIO.
    """
    with TempDirContextNoChange() as tmpdir:
        args = make_backend_args(tmpdir, CXXFLAGS="-O2 -std=c++17")
        hunter = make_mock_hunter(sources=["/src/mylib.cpp"], headers=["/src/mylib.h"])
        backend = backend_class(args=args, hunter=hunter)
        graph = graph_factory(args.objdir, args.bindir)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        yield buf.getvalue(), args


class TestNinjaLibraryOutput:
    def test_ninja_static_library_output(self):
        from compiletools.ninja_backend import NinjaBackend

        with _layer3_context(NinjaBackend, _make_library_graph) as (output, args):
            assert "rule static_library_cmd" in output
            assert f"build {args.bindir}/libmylib.a: static_library_cmd" in output

    def test_ninja_shared_library_output(self):
        from compiletools.ninja_backend import NinjaBackend

        with _layer3_context(NinjaBackend, _make_shared_library_graph) as (output, args):
            assert "rule shared_library_cmd" in output
            assert f"build {args.bindir}/libmylib.so: shared_library_cmd" in output


class TestCMakeLibraryOutput:
    def test_cmake_static_library_output(self):
        from compiletools.cmake_backend import CMakeBackend

        with _layer3_context(CMakeBackend, _make_library_graph) as (output, _args):
            assert "add_library(" in output
            assert "STATIC" in output

    def test_cmake_shared_library_output(self):
        from compiletools.cmake_backend import CMakeBackend

        with _layer3_context(CMakeBackend, _make_shared_library_graph) as (output, _args):
            assert "add_library(" in output
            assert "SHARED" in output


class TestBazelLibraryOutput:
    def test_bazel_static_library_output(self):
        from compiletools.bazel_backend import BazelBackend

        with _layer3_context(BazelBackend, _make_library_graph) as (output, _args):
            assert "cc_library(" in output

    def test_bazel_shared_library_output(self):
        from compiletools.bazel_backend import BazelBackend

        with _layer3_context(BazelBackend, _make_shared_library_graph) as (output, _args):
            assert "cc_binary(" in output
            assert "linkshared = True" in output


class TestShakeLibrarySummary:
    def test_shake_static_library_summary(self):
        from compiletools.shake_backend import ShakeBackend

        with _layer3_context(ShakeBackend, _make_library_graph) as (output, _args):
            assert "static_library" in output


class TestTupLibraryOutput:
    def test_tup_static_library_output(self):
        from compiletools.tup_backend import TupBackend

        with _layer3_context(TupBackend, _make_library_graph) as (output, _args):
            assert "ar -src" in output


class TestMakefileLibraryOutput:
    def test_makefile_static_library_output(self):
        from compiletools.makefile_backend import MakefileBackend

        with _layer3_context(MakefileBackend, _make_library_graph) as (output, _args):
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
