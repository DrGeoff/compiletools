"""Integration tests verifying the full pipeline:
source files -> Hunter -> BuildGraph -> Backend -> build file -> build tool -> executable

Tests each registered backend end-to-end with a real compiler.
"""

import io
import os
import shutil
import subprocess

import pytest

import compiletools.apptools
import compiletools.bazel_backend
import compiletools.cmake_backend
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.makefile_backend
import compiletools.namer
import compiletools.ninja_backend
import compiletools.testhelper as uth
import compiletools.trace_backend
import compiletools.tup_backend
import compiletools.utils
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.makefile_backend import MakefileBackend
from compiletools.test_base import BaseCompileToolsTestCase


def _add_backend_arguments(cap):
    """Add all arguments needed by backends (general + make-specific)."""
    compiletools.apptools.add_target_arguments_ex(cap)
    compiletools.apptools.add_link_arguments(cap)
    compiletools.namer.Namer.add_arguments(cap)
    compiletools.hunter.add_arguments(cap)
    MakefileBackend.add_arguments(cap)


def _setup_backend_for_source(backend_name, tmp_path, src_file="helloworld_cpp.cpp"):
    """Create a backend with real args and hunter for a simple source.

    Returns (backend, graph, args) with everything ready for generate()/execute().
    """
    shutil.copy2(os.path.join(uth.samplesdir(), "simple", src_file), tmp_path)
    source_path = os.path.realpath(os.path.join(tmp_path, src_file))

    objdir = os.path.join(str(tmp_path), "obj")
    bindir = os.path.join(str(tmp_path), "bin")
    argv = [
        "--include",
        str(tmp_path),
        "--objdir",
        objdir,
        "--bindir",
        bindir,
        source_path,
    ]

    cap = compiletools.apptools.create_parser("Backend integration test", argv=argv)
    _add_backend_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)

    ctx = BuildContext()
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

    BackendClass = get_backend_class(backend_name)
    backend = BackendClass(args=args, hunter=hunter)
    graph = backend.build_graph()

    return backend, graph, args


class TestBackendBuildApplication(BaseCompileToolsTestCase):
    """Build a real application with each available backend.

    Mirrors test_cake.py::TestCake::test_no_git_root but uses the new
    backend pipeline: build_graph() -> generate() -> execute().
    """

    @uth.requires_functional_compiler
    @uth.requires_backend_tool()
    @pytest.mark.parametrize("backend_name", available_backends())
    def test_build_helloworld(self, backend_name, tmp_path, monkeypatch):
        """Build helloworld_cpp.cpp with each registered backend."""
        with uth.ParserContext():
            backend, graph, args = _setup_backend_for_source(backend_name, tmp_path)

            # Verify graph has expected structure
            compile_rules = graph.rules_by_type("compile")
            link_rules = graph.rules_by_type("link")
            phony_rules = graph.rules_by_type("phony")
            assert len(compile_rules) >= 1, f"{backend_name}: expected compile rules"
            assert len(link_rules) >= 1, f"{backend_name}: expected link rules"
            assert any(r.output == "build" for r in phony_rules), f"{backend_name}: expected 'build' phony"
            assert any(r.output == "all" for r in phony_rules), f"{backend_name}: expected 'all' phony"

            # Generate and execute the build
            # Note: do NOT pre-create objdir here — the build system must
            # handle it via the mkdir rule in the BuildGraph.
            objdir = args.objdir
            bindir = args.bindir
            os.makedirs(bindir, exist_ok=True)

            if backend_name == "shake":
                # Self-executing backend — no external build file needed
                backend.generate(graph)
                backend.execute("build")
            elif backend_name == "cmake":
                # CMake needs CMakeLists.txt in the source directory
                build_file = os.path.join(str(tmp_path), "CMakeLists.txt")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                assert os.path.exists(build_file), f"{backend_name}: CMakeLists.txt not created"

                cmake_build_dir = os.path.join(str(tmp_path), "cmake-build")
                os.makedirs(cmake_build_dir, exist_ok=True)

                # Configure
                result = subprocess.run(
                    ["cmake", "-S", str(tmp_path), "-B", cmake_build_dir],
                    cwd=str(tmp_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, (
                    f"cmake configure failed (rc={result.returncode}):\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )

                # Build
                result = subprocess.run(
                    ["cmake", "--build", cmake_build_dir],
                    cwd=str(tmp_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, (
                    f"cmake build failed (rc={result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
                )

                # Copy executable to bindir for verification
                for dirpath, _dirs, files in os.walk(cmake_build_dir):
                    for fname in files:
                        full = os.path.join(dirpath, fname)
                        if os.access(full, os.X_OK) and not fname.endswith(".cmake"):
                            shutil.copy2(full, os.path.join(bindir, fname))

            elif backend_name == "tup":
                # Tup requires tup init + Tupfile in the build directory
                build_file = os.path.join(str(tmp_path), "Tupfile")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                assert os.path.exists(build_file), f"{backend_name}: Tupfile not created"

                subprocess.check_call(["tup", "init"], cwd=str(tmp_path), timeout=10)
                result = subprocess.run(["tup"], cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, (
                    f"tup build failed (rc={result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
                )

            elif backend_name == "bazel":
                # Bazel needs BUILD.bazel + WORKSPACE in the source directory
                build_file = os.path.join(str(tmp_path), "BUILD.bazel")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                workspace_file = os.path.join(str(tmp_path), "WORKSPACE")
                with open(workspace_file, "w") as f:
                    f.write("# WORKSPACE generated by compiletools\n")
                module_file = os.path.join(str(tmp_path), "MODULE.bazel")
                with open(module_file, "w") as f:
                    f.write('module(name = "compiletools_test")\n')
                    f.write('bazel_dep(name = "rules_cc", version = "0.1.1")\n')
                assert os.path.exists(build_file), f"{backend_name}: BUILD.bazel not created"

                monkeypatch.chdir(str(tmp_path))
                try:
                    backend.execute("build")
                except subprocess.CalledProcessError as e:
                    # Skip on TLS cert errors (environment issue, not a code bug)
                    stderr = getattr(e, "stderr", "") or ""
                    if "certificate" in stderr.lower():
                        pytest.skip(f"bazel TLS error (environment issue): {stderr[:200]}")
                    raise
            else:
                build_file = os.path.join(str(tmp_path), type(backend).build_filename())
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)

                assert os.path.exists(build_file), f"{backend_name}: build file not created"

                if backend_name == "make":
                    cmd = ["make", "-s", "-j1", "-f", build_file, "build"]
                elif backend_name == "ninja":
                    cmd = ["ninja", "-f", build_file, "build"]
                else:
                    pytest.skip(f"Don't know how to invoke {backend_name}")

                result = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, (
                    f"{backend_name} build failed (rc={result.returncode}):\n"
                    f"stdout: {result.stdout}\nstderr: {result.stderr}"
                )

            # Verify an executable was produced
            src_file = "helloworld_cpp.cpp"
            exe_name = os.path.splitext(src_file)[0]  # "helloworld_cpp"
            exe_path = os.path.join(bindir, exe_name)
            if not os.path.exists(exe_path):
                # Search for it
                candidates = []
                for dirpath, _dirs, files in os.walk(str(tmp_path)):
                    for f in files:
                        full = os.path.join(dirpath, f)
                        if compiletools.utils.is_executable(full) and exe_name in f:
                            candidates.append(full)
                assert candidates, (
                    f"{backend_name}: no executable '{exe_name}' found.\n"
                    f"Files in bindir: {list(os.listdir(bindir)) if os.path.isdir(bindir) else 'N/A'}\n"
                    f"Files in objdir: {list(os.listdir(objdir)) if os.path.isdir(objdir) else 'N/A'}"
                )
                exe_path = candidates[0]

            assert compiletools.utils.is_executable(exe_path), f"{backend_name}: {exe_path} is not executable"

            # Run the built executable to verify it works
            run_result = subprocess.run([exe_path], capture_output=True, text=True, timeout=10)
            assert run_result.returncode == 0, (
                f"{backend_name}: executable failed (rc={run_result.returncode}):\n"
                f"stdout: {run_result.stdout}\nstderr: {run_result.stderr}"
            )


class TestBackendRunTestsDispatch:
    """Verify each non-make backend dispatches execute('runtests') to _run_tests()."""

    @pytest.mark.parametrize("backend_name", ["ninja", "bazel", "cmake", "shake", "tup"])
    def test_execute_runtests_calls_run_tests(self, backend_name):
        from unittest.mock import MagicMock, patch

        BackendClass = get_backend_class(backend_name)
        args = MagicMock()
        args.tests = ["/src/test_foo.cpp"]
        args.verbose = 0
        hunter = MagicMock()
        backend = BackendClass(args=args, hunter=hunter)

        with patch.object(backend, "_run_tests") as mock_run_tests:
            backend.execute("runtests")
            mock_run_tests.assert_called_once()


class TestBackendBuildGraphWithTests(BaseCompileToolsTestCase):
    """Verify build_graph() includes test targets for all backends."""

    @uth.requires_functional_compiler
    def test_build_graph_includes_runtests(self, tmp_path):
        """build_graph() with tests should include 'runtests' phony target."""
        with uth.ParserContext():
            # Use helloworld_cpp.cpp as both a regular and test target to verify graph structure
            shutil.copy2(os.path.join(uth.samplesdir(), "simple", "helloworld_cpp.cpp"), tmp_path)
            source_path = os.path.realpath(os.path.join(tmp_path, "helloworld_cpp.cpp"))

            objdir = os.path.join(str(tmp_path), "obj")
            bindir = os.path.join(str(tmp_path), "bin")
            argv = [
                "--include",
                str(tmp_path),
                "--objdir",
                objdir,
                "--bindir",
                bindir,
                "--tests",
                source_path,
            ]

            cap = compiletools.apptools.create_parser("Backend integration test", argv=argv)
            _add_backend_arguments(cap)
            args = compiletools.apptools.parseargs(cap, argv)

            ctx = BuildContext()
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

            BackendClass = get_backend_class("ninja")
            backend = BackendClass(args=args, hunter=hunter)
            graph = backend.build_graph()

            assert "runtests" in graph.outputs
            runtests_rule = graph.get_rule("runtests")
            assert runtests_rule is not None
            assert runtests_rule.rule_type == "phony"
            assert len(runtests_rule.inputs) >= 1


class TestBackendGenerateOutput(BaseCompileToolsTestCase):
    """Verify each backend produces syntactically correct output without building."""

    def test_make_output_has_makefile_syntax(self, tmp_path):
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("make", tmp_path)

            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

            assert ".DELETE_ON_ERROR:" in content
            assert ".PHONY: build" in content
            assert ".PHONY: all" in content
            assert "\t" in content  # Tab-indented recipes
            for rule in graph.rules_by_type("compile"):
                assert rule.output in content

    def test_ninja_output_has_ninja_syntax(self, tmp_path):
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("ninja", tmp_path)

            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

            assert "rule compile_cmd" in content
            assert "command = $cmd" in content
            assert "build build: phony" in content
            for rule in graph.rules_by_type("compile"):
                assert f"build {rule.output}:" in content

    def test_bazel_output_has_build_syntax(self, tmp_path):
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("bazel", tmp_path)

            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

            assert "cc_binary(" in content
            assert "srcs = [" in content
            assert "BUILD.bazel generated by compiletools" in content
            # Should have at least one source file referenced
            assert ".cpp" in content

    def test_tup_output_has_tupfile_syntax(self, tmp_path):
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("tup", tmp_path)

            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

            assert "Tupfile generated by compiletools" in content
            # Tup syntax: lines start with ": " and contain "|>" markers
            assert "|>" in content
            for rule in graph.rules_by_type("compile"):
                assert rule.output in content
                # Each compile rule's source should appear
                assert rule.inputs[0] in content

    def test_cmake_output_has_cmake_syntax(self, tmp_path):
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("cmake", tmp_path)

            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

            assert "cmake_minimum_required(VERSION 3.15)" in content
            assert "project(compiletools_build CXX)" in content
            assert "add_executable(" in content
            assert "CMakeLists.txt generated by compiletools" in content
            assert ".cpp" in content


class TestBackendDefaultFileWrite(BaseCompileToolsTestCase):
    """Verify each backend's generate(graph) with output=None writes the expected file."""

    @pytest.mark.parametrize(
        "backend_name,expected_file,expected_marker",
        [
            ("make", "Makefile", ".DELETE_ON_ERROR:"),
            ("ninja", "build.ninja", "rule compile_cmd"),
            ("bazel", "BUILD.bazel", "BUILD.bazel generated by compiletools"),
            ("cmake", "CMakeLists.txt", "cmake_minimum_required"),
            ("tup", "Tupfile", "Tupfile generated by compiletools"),
        ],
    )
    def test_default_file_write(self, backend_name, expected_file, expected_marker, tmp_path, monkeypatch):
        """generate(graph) with output=None should write the expected file to disk."""
        with uth.ParserContext():
            backend, graph, args = _setup_backend_for_source(backend_name, tmp_path)

            # For make backend, set makefilename to tmp_path so it writes there
            if backend_name == "make":
                args.makefilename = os.path.join(str(tmp_path), expected_file)

            # chdir to tmp_path so default file writes land there
            monkeypatch.chdir(str(tmp_path))
            backend.generate(graph)

            output_path = os.path.join(str(tmp_path), expected_file)
            assert os.path.exists(output_path), f"{backend_name}: expected {expected_file} at {output_path}"
            with open(output_path) as fh:
                content = fh.read()
            assert expected_marker in content, (
                f"{backend_name}: {expected_file} missing expected marker '{expected_marker}'"
            )

    def test_shake_stores_graph_internally(self, tmp_path, monkeypatch):
        """Shake backend stores graph internally; no file written on generate()."""
        with uth.ParserContext():
            backend, graph, _args = _setup_backend_for_source("shake", tmp_path)

            monkeypatch.chdir(str(tmp_path))
            backend.generate(graph)

            # Shake stores the graph internally, no file is written
            assert backend._graph is graph
