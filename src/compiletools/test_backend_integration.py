"""Integration tests verifying the full pipeline:
source files -> Hunter -> BuildGraph -> Backend -> build file -> build tool -> executable

Tests each registered backend end-to-end with a real compiler.
"""

import io
import os
import pathlib
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
from compiletools.trace_backend import SlurmBackend


def _add_backend_arguments(cap):
    """Add all arguments needed by backends (general + backend-specific)."""
    compiletools.apptools.add_target_arguments_ex(cap)
    compiletools.apptools.add_link_arguments(cap)
    compiletools.namer.Namer.add_arguments(cap)
    compiletools.hunter.add_arguments(cap)
    MakefileBackend.add_arguments(cap)
    SlurmBackend.add_arguments(cap)


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
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

    BackendClass = get_backend_class(backend_name)
    backend = BackendClass(args=args, hunter=hunter, context=ctx)
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
    def test_build_helloworld(self, backend_name, tmp_path, monkeypatch, capfd):
        """Build helloworld_cpp.cpp with each registered backend."""
        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp, uth.ParserContext():
            effective_tmp = pathlib.Path(effective_tmp)
            backend, graph, args = _setup_backend_for_source(backend_name, effective_tmp)

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

            if backend_name in ("shake", "slurm"):
                # Self-executing backends — no external build file needed
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
                except subprocess.CalledProcessError:
                    captured = capfd.readouterr()
                    uth.skip_if_bazel_env_error(captured.err)
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
                for dirpath, _dirs, files in os.walk(str(effective_tmp)):
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


class TestBackendBuildPCH(BaseCompileToolsTestCase):
    """Build a PCH-enabled project with each available backend."""

    @uth.requires_functional_compiler
    @uth.requires_backend_tool()
    @pytest.mark.parametrize("backend_name", available_backends())
    def test_build_pch(self, backend_name, tmp_path, monkeypatch, capfd):
        """Build pch sample with each registered backend."""
        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp, uth.ParserContext():
            effective_tmp = pathlib.Path(effective_tmp)
            # Copy PCH sample files to temp dir
            pch_sample = os.path.join(uth.samplesdir(), "pch")
            for f in os.listdir(pch_sample):
                shutil.copy2(os.path.join(pch_sample, f), effective_tmp)

            source_path = os.path.realpath(os.path.join(effective_tmp, "pch_user.cpp"))
            objdir = os.path.join(str(effective_tmp), "obj")
            bindir = os.path.join(str(effective_tmp), "bin")
            pchdir = os.path.join(str(effective_tmp), "pch")
            argv = [
                "--include",
                str(effective_tmp),
                "--objdir",
                objdir,
                "--bindir",
                bindir,
                "--pchdir",
                pchdir,
                source_path,
            ]

            cap = compiletools.apptools.create_parser("PCH integration test", argv=argv)
            _add_backend_arguments(cap)
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

            BackendClass = get_backend_class(backend_name)
            backend = BackendClass(args=args, hunter=hunter, context=ctx)
            graph = backend.build_graph()

            # Verify graph has a .gch compile rule
            compile_rules = graph.rules_by_type("compile")
            gch_rules = [r for r in compile_rules if r.output.endswith(".gch")]
            assert len(gch_rules) >= 1, (
                f"{backend_name}: expected a .gch compile rule, got outputs: {[r.output for r in compile_rules]}"
            )
            gch_rule = gch_rules[0]
            assert "-x" in gch_rule.command
            assert "c++-header" in gch_rule.command

            # Verify .gch is placed under pchdir with content-addressable hash dir
            assert gch_rule.output.startswith(pchdir + "/"), (
                f"{backend_name}: .gch should be under pchdir, got: {gch_rule.output}"
            )
            # Layout: <pchdir>/<16-char-hash>/stdafx.h.gch
            rel = gch_rule.output[len(pchdir) + 1 :]
            parts = rel.split("/")
            assert len(parts) == 2, f"Expected <hash>/header.gch, got: {rel}"
            assert len(parts[0]) == 16, f"Hash dir should be 16 chars, got: {parts[0]}"
            assert parts[1] == "stdafx.h.gch"

            # Verify source compile command includes -I for the pchdir hash dir
            source_rules = [r for r in compile_rules if not r.output.endswith(".gch")]
            assert source_rules, f"{backend_name}: expected source compile rules"
            source_cmd = source_rules[0].command
            assert source_cmd is not None
            assert "-I" in source_cmd, f"{backend_name}: source compile should include -I for pchdir"
            pch_i_indices = [
                i
                for i, tok in enumerate(source_cmd)
                if tok == "-I" and i + 1 < len(source_cmd) and source_cmd[i + 1].startswith(pchdir + "/")
            ]
            assert pch_i_indices, f"{backend_name}: no -I points to pchdir hash dir; cmd={source_cmd}"

            # Generate and execute the build
            os.makedirs(bindir, exist_ok=True)

            if backend_name in ("shake", "slurm"):
                backend.generate(graph)
                backend.execute("build")
            elif backend_name == "cmake":
                build_file = os.path.join(str(effective_tmp), "CMakeLists.txt")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                cmake_build_dir = os.path.join(str(effective_tmp), "cmake-build")
                os.makedirs(cmake_build_dir, exist_ok=True)
                result = subprocess.run(
                    ["cmake", "-S", str(effective_tmp), "-B", cmake_build_dir],
                    cwd=str(effective_tmp),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, (
                    f"cmake configure failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
                )
                result = subprocess.run(
                    ["cmake", "--build", cmake_build_dir],
                    cwd=str(effective_tmp),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, f"cmake build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
                for dirpath, _dirs, files in os.walk(cmake_build_dir):
                    for fname in files:
                        full = os.path.join(dirpath, fname)
                        if os.access(full, os.X_OK) and not fname.endswith(".cmake"):
                            shutil.copy2(full, os.path.join(bindir, fname))
            elif backend_name == "tup":
                build_file = os.path.join(str(effective_tmp), "Tupfile")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                subprocess.check_call(["tup", "init"], cwd=str(effective_tmp), timeout=10)
                result = subprocess.run(
                    ["tup"],
                    cwd=str(effective_tmp),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, f"tup build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            elif backend_name == "bazel":
                build_file = os.path.join(str(effective_tmp), "BUILD.bazel")
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                with open(os.path.join(str(effective_tmp), "WORKSPACE"), "w") as f:
                    f.write("# WORKSPACE generated by compiletools\n")
                with open(os.path.join(str(effective_tmp), "MODULE.bazel"), "w") as f:
                    f.write('module(name = "compiletools_test")\n')
                    f.write('bazel_dep(name = "rules_cc", version = "0.1.1")\n')
                monkeypatch.chdir(str(effective_tmp))
                try:
                    backend.execute("build")
                except subprocess.CalledProcessError:
                    captured = capfd.readouterr()
                    uth.skip_if_bazel_env_error(captured.err)
                    raise
            else:
                build_file = os.path.join(str(effective_tmp), type(backend).build_filename())
                with open(build_file, "w") as f:
                    backend.generate(graph, output=f)
                if backend_name == "make":
                    cmd = ["make", "-s", "-j1", "-f", build_file, "build"]
                elif backend_name == "ninja":
                    cmd = ["ninja", "-f", build_file, "build"]
                else:
                    pytest.skip(f"Don't know how to invoke {backend_name}")
                result = subprocess.run(
                    cmd,
                    cwd=str(effective_tmp),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, (
                    f"{backend_name} build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
                )

            # Verify an executable was produced and runs correctly
            exe_name = "pch_user"
            candidates = []
            for dirpath, _dirs, files in os.walk(str(effective_tmp)):
                for f in files:
                    full = os.path.join(dirpath, f)
                    if compiletools.utils.is_executable(full) and exe_name in f:
                        candidates.append(full)
            assert candidates, (
                f"{backend_name}: no executable '{exe_name}' found.\n"
                f"Files in bindir: {list(os.listdir(bindir)) if os.path.isdir(bindir) else 'N/A'}\n"
                f"Files in objdir: {list(os.listdir(objdir)) if os.path.isdir(objdir) else 'N/A'}"
            )

            run_result = subprocess.run(
                [candidates[0]],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert run_result.returncode == 0, (
                f"{backend_name}: executable failed:\nstdout: {run_result.stdout}\nstderr: {run_result.stderr}"
            )
            assert "pch works" in run_result.stdout, (
                f"{backend_name}: expected 'pch works' in output, got: {run_result.stdout}"
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
        backend = BackendClass(args=args, hunter=hunter, context=BuildContext())

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
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

            BackendClass = get_backend_class("ninja")
            backend = BackendClass(args=args, hunter=hunter, context=ctx)
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
