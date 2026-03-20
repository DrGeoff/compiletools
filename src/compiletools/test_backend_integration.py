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
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.makefile
import compiletools.makefile_backend
import compiletools.ninja_backend
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.test_base import BaseCompileToolsTestCase


def _backend_tool_available(backend_name):
    """Check if the build tool for a backend is on PATH."""
    tool = {"make": "make", "ninja": "ninja"}.get(backend_name)
    if tool is None:
        return False
    return shutil.which(tool) is not None


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
    compiletools.makefile.MakefileCreator.add_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)

    headerdeps = compiletools.headerdeps.create(args)
    magicparser = compiletools.magicflags.create(args, headerdeps)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

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
    @pytest.mark.parametrize("backend_name", available_backends())
    def test_build_helloworld(self, backend_name, tmp_path):
        """Build helloworld_cpp.cpp with each registered backend."""
        if not _backend_tool_available(backend_name):
            pytest.skip(f"{backend_name} build tool not found on PATH")

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

            # Generate the build file
            objdir = args.objdir
            bindir = args.bindir
            os.makedirs(objdir, exist_ok=True)
            os.makedirs(bindir, exist_ok=True)
            build_file = os.path.join(str(tmp_path), type(backend).build_filename())
            with open(build_file, "w") as f:
                backend.generate(graph, output=f)

            assert os.path.exists(build_file), f"{backend_name}: build file not created"

            # Execute the build
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
