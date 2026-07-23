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
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_backend import available_backends, ensure_backends_registered, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.test_base import BaseCompileToolsTestCase

ensure_backends_registered()


def _setup_backend_for_source(backend_name, tmp_path, src_file="helloworld_cpp.cpp", extra_argv=None):
    """Create a backend with real args and hunter for a simple source.

    Returns (backend, graph, args) with everything ready for generate()/execute().
    """
    shutil.copy2(uth.example_file(f"simple/{src_file}"), tmp_path)
    source_path = os.path.realpath(os.path.join(tmp_path, src_file))

    objdir = os.path.join(str(tmp_path), "obj")
    bindir = os.path.join(str(tmp_path), "bin")
    argv = (extra_argv or []) + [
        "--include",
        str(tmp_path),
        "--cas-objdir",
        objdir,
        "--bindir",
        bindir,
        source_path,
    ]

    cap = compiletools.apptools.create_parser("Backend integration test", argv=argv)
    uth.add_backend_arguments(cap)
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

    BackendClass = get_backend_class(backend_name)
    backend = BackendClass(args=args, hunter=hunter, context=ctx)
    graph = backend.build_graph()

    return backend, graph, args


def _render_backend_to_string(backend_name, tmp_path):
    """Build a graph for *backend_name* and render it to an in-memory string.

    Returns ``(content, graph)``. The graph is included so per-backend
    assertions can iterate ``graph.rules_by_type("compile")`` against the
    rendered output. ``uth.ParserContext()`` isolates argparse state.
    """
    with uth.ParserContext():
        backend, graph, _args = _setup_backend_for_source(backend_name, tmp_path)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue(), graph


def _run_backend_build(
    backend,
    backend_name: str,
    graph,
    effective_tmp,
    *,
    monkeypatch,
    capfd,
    timeout: int = 30,
) -> None:
    """Generate the native build file and execute the build for *backend*.

    Handles per-backend scaffolding (BUILD.bazel + MODULE.bazel for bazel,
    chdir for cmake/bazel, raw subprocess for make/ninja). Translates a
    bazel toolchain-env failure into pytest.skip via skip_if_bazel_env_error.
    """
    effective_tmp = str(effective_tmp)
    if backend_name == "shake":
        backend.generate(graph)
        backend.execute("build")
        return
    if backend_name == "cmake":
        with open(os.path.join(effective_tmp, "CMakeLists.txt"), "w") as f:
            backend.generate(graph, output=f)
        monkeypatch.chdir(effective_tmp)
        backend.execute("build")
        return
    if backend_name == "bazel":
        with open(os.path.join(effective_tmp, "BUILD.bazel"), "w") as f:
            backend.generate(graph, output=f)
        with open(os.path.join(effective_tmp, "MODULE.bazel"), "w") as f:
            f.write('module(name = "compiletools_test")\n')
            f.write('bazel_dep(name = "rules_cc", version = "0.1.5")\n')
        monkeypatch.chdir(effective_tmp)
        try:
            backend.execute("build")
        except subprocess.CalledProcessError:
            captured = capfd.readouterr()
            uth.skip_if_bazel_env_error(captured.err)
            raise
        return
    build_file = os.path.join(effective_tmp, type(backend).build_filename())
    with open(build_file, "w") as f:
        backend.generate(graph, output=f)
    if backend_name == "make":
        cmd = ["make", "-s", "-j1", "-f", build_file, "build"]
    elif backend_name == "ninja":
        cmd = ["ninja", "-f", build_file, "build"]
    else:
        pytest.skip(f"Don't know how to invoke {backend_name}")
    result = subprocess.run(cmd, cwd=effective_tmp, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f"{backend_name} build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"


def _find_built_executable(search_root, exe_name: str, *, exact: bool = False) -> list[str]:
    """Walk *search_root* and return executable paths matching *exe_name*.

    When *exact* is True, basename must equal *exe_name*; otherwise *exe_name*
    just has to be a substring of the basename.
    """
    matches: list[str] = []
    for dirpath, _dirs, files in os.walk(str(search_root)):
        for f in files:
            if (exact and f != exe_name) or (not exact and exe_name not in f):
                continue
            full = os.path.join(dirpath, f)
            if compiletools.utils.is_executable(full):
                matches.append(full)
    return matches


class TestBackendBuildApplication(BaseCompileToolsTestCase):
    """Build a real application with each available backend.

    Mirrors test_cake.py::TestCake::test_no_git_root but uses the new
    backend pipeline: build_graph() -> generate() -> execute().
    """

    @uth.requires_functional_compiler
    @uth.requires_compiler_supports_default_std
    @uth.requires_backend_tool()
    @pytest.mark.parametrize("backend_name", available_backends())
    def test_build_helloworld(self, backend_name, tmp_path, monkeypatch, capfd, capped_parallel_argv):
        """Build helloworld_cpp.cpp with each registered backend."""
        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp, uth.ParserContext():
            effective_tmp = pathlib.Path(effective_tmp)
            backend, graph, args = _setup_backend_for_source(
                backend_name, effective_tmp, extra_argv=capped_parallel_argv
            )

            # Verify graph has expected structure
            compile_rules = graph.rules_by_type("compile")
            link_rules = graph.rules_by_type("link")
            phony_rules = graph.rules_by_type("phony")
            assert len(compile_rules) >= 1, f"{backend_name}: expected compile rules"
            assert len(link_rules) >= 1, f"{backend_name}: expected link rules"
            assert any(r.output == "build" for r in phony_rules), f"{backend_name}: expected 'build' phony"
            assert any(r.output == "all" for r in phony_rules), f"{backend_name}: expected 'all' phony"

            # Note: do NOT pre-create objdir here — the build system must
            # handle it via the mkdir rule in the BuildGraph.
            objdir = args.cas_objdir
            bindir = args.bindir
            os.makedirs(bindir, exist_ok=True)

            _run_backend_build(backend, backend_name, graph, effective_tmp, monkeypatch=monkeypatch, capfd=capfd)

            src_file = "helloworld_cpp.cpp"
            exe_name = os.path.splitext(src_file)[0]
            exe_path = os.path.join(bindir, exe_name)
            if not os.path.exists(exe_path):
                candidates = _find_built_executable(effective_tmp, exe_name)
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
    def test_build_pch(self, backend_name, tmp_path, monkeypatch, capfd, capped_parallel_argv):
        """Build pch sample with each registered backend."""
        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp, uth.ParserContext():
            effective_tmp = pathlib.Path(effective_tmp)
            # Copy PCH sample files to temp dir
            pch_sample = uth.example_path("pch")
            for f in os.listdir(pch_sample):
                shutil.copy2(os.path.join(pch_sample, f), effective_tmp)

            source_path = os.path.realpath(os.path.join(effective_tmp, "pch_user.cpp"))
            objdir = os.path.join(str(effective_tmp), "obj")
            bindir = os.path.join(str(effective_tmp), "bin")
            pchdir = os.path.join(str(effective_tmp), "pch")
            argv = capped_parallel_argv + [
                "--include",
                str(effective_tmp),
                "--cas-objdir",
                objdir,
                "--bindir",
                bindir,
                "--cas-pchdir",
                pchdir,
                source_path,
            ]

            cap = compiletools.apptools.create_parser("PCH integration test", argv=argv)
            uth.add_backend_arguments(cap)
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            # cas-objdir / cas-pchdir get the active variant appended at
            # parse time, so refresh the test's local references from the
            # post-parse values before asserting on output paths.
            objdir = args.cas_objdir
            pchdir = args.cas_pchdir
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
            assert gch_rule.command is not None
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

            # Verify source compile loads the cached PCH via `-include
            # <pchdir>/<hash>/<basename>` (NOT `-I <pchdir>/<hash>` — see
            # examples-features/pch_bypass_bug/).
            source_rules = [r for r in compile_rules if not r.output.endswith(".gch")]
            assert source_rules, f"{backend_name}: expected source compile rules"
            source_cmd = source_rules[0].command
            assert source_cmd is not None
            assert "-include" in source_cmd, (
                f"{backend_name}: source compile should -include the cached PCH; cmd={source_cmd}"
            )
            pch_inc_indices = [
                i
                for i, tok in enumerate(source_cmd)
                if tok == "-include" and i + 1 < len(source_cmd) and source_cmd[i + 1].startswith(pchdir + "/")
            ]
            assert pch_inc_indices, f"{backend_name}: no -include points under pchdir; cmd={source_cmd}"
            # Also assert the staged path basename matches the PCH header.
            staged = source_cmd[pch_inc_indices[0] + 1]
            assert staged.endswith("/stdafx.h"), staged

            os.makedirs(bindir, exist_ok=True)
            _run_backend_build(backend, backend_name, graph, effective_tmp, monkeypatch=monkeypatch, capfd=capfd)

            candidates = _find_built_executable(effective_tmp, "pch_user")
            assert candidates, (
                f"{backend_name}: no executable 'pch_user' found.\n"
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

            # Verify the .gch artefact was actually built. Without the
            # prebuild step in build_backend._prebuild_aux_artefacts the
            # cmake and bazel backends would silently fall through to
            # raw-header inclusion -- correctness preserved, performance
            # lost. Asserting the .gch exists pins the prebuild contract.
            assert os.path.isfile(gch_rule.output), (
                f"{backend_name}: PCH .gch was not built at {gch_rule.output}; "
                f"pchdir contents: {list(pathlib.Path(pchdir).rglob('*')) if os.path.isdir(pchdir) else 'N/A'}"
            )


class TestBackendBuildHeaderUnits(BaseCompileToolsTestCase):
    """Build a C++20 header-units project with each available backend.

    Mirrors :class:`TestBackendBuildPCH` -- this is the cross-backend
    correctness check for the prebuild step in
    ``BuildBackend._prebuild_aux_artefacts``. Without that step the
    cmake and bazel backends never run the HEADER_UNIT precompile
    rules, so the consumer compile commands' ``-fmodule-file=`` /
    ``-fmodule-mapper=`` flags resolve to non-existent paths and the
    build fails with "module file not found" (clang) or
    "could not find module" (gcc).
    """

    @uth.requires_functional_compiler
    @uth.requires_backend_tool()
    @pytest.mark.parametrize("backend_name", available_backends())
    def test_build_header_units(self, backend_name, tmp_path, monkeypatch, capfd, capped_parallel_argv):
        """Build cxx_modules_header_units sample with each registered backend."""
        from compiletools.test_cxx_modules import (
            _clang_supports_header_units,
            _detected_gcc_supports_modules,
            _gcc_supports_header_units,
            _which,
        )

        cxx = compiletools.apptools.get_functional_cxx_compiler()
        if (
            cxx
            and compiletools.apptools.compiler_kind(cxx) == "gcc"
            and _detected_gcc_supports_modules()
            and _gcc_supports_header_units()
        ):
            module_cxx = cxx
        elif _clang_supports_header_units():
            module_cxx = _which("clang++")
        else:
            pytest.skip("No compiler on PATH supports C++20 header units")

        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp, uth.ParserContext():
            effective_tmp = pathlib.Path(effective_tmp)
            sample = uth.example_path("cxx_modules_header_units")
            for f in os.listdir(sample):
                shutil.copy2(os.path.join(sample, f), effective_tmp)

            source_path = os.path.realpath(os.path.join(effective_tmp, "main.cpp"))
            objdir = os.path.join(str(effective_tmp), "obj")
            bindir = os.path.join(str(effective_tmp), "bin")
            pcmdir = os.path.join(str(effective_tmp), "pcm")
            argv = capped_parallel_argv + [
                "--include",
                str(effective_tmp),
                "--cas-objdir",
                objdir,
                "--bindir",
                bindir,
                "--cas-pcmdir",
                pcmdir,
                source_path,
            ]

            with uth.CompilerEnvContext(module_cxx):
                cap = compiletools.apptools.create_parser("Header units integration test", argv=argv)
                uth.add_backend_arguments(cap)
                ctx = BuildContext()
                args = compiletools.apptools.parseargs(cap, argv, context=ctx)
                headerdeps = compiletools.headerdeps.create(args, context=ctx)
                magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
                hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

                BackendClass = get_backend_class(backend_name)
                backend = BackendClass(args=args, hunter=hunter, context=ctx)
                graph = backend.build_graph()

                # The graph must carry header-unit producer rules for at
                # least the imported headers (vector, cstdio). Without
                # them the prebuild step has nothing to do and the test
                # would silently degenerate to "compiles without modules"
                # on backends that ignore module flags.
                from compiletools.build_graph import RuleType

                hu_rules = graph.rules_by_type(RuleType.HEADER_UNIT)
                clang_module_precompile = [
                    r
                    for r in graph.rules_by_type(RuleType.COMPILE)
                    if r.command and "--precompile" in r.command and r.output.endswith(".pcm")
                ]
                aux_producers = hu_rules + clang_module_precompile
                assert aux_producers, (
                    f"{backend_name}: graph has no HEADER_UNIT or clang --precompile rules; "
                    f"sample probably did not detect the imports."
                )

                os.makedirs(bindir, exist_ok=True)
                _run_backend_build(
                    backend, backend_name, graph, effective_tmp, monkeypatch=monkeypatch, capfd=capfd, timeout=60
                )

            # Aux producer artefacts must exist post-build. The prebuild step
            # creates them on cmake/bazel; on make/ninja/shake they're
            # part of the per-rule executor's graph.
            missing = [r.output for r in aux_producers if not os.path.isfile(r.output)]
            assert not missing, (
                f"{backend_name}: aux producer outputs missing post-build: {missing}\n"
                f"pcmdir contents: {list(pathlib.Path(pcmdir).rglob('*')) if os.path.isdir(pcmdir) else 'N/A'}"
            )

            candidates = _find_built_executable(effective_tmp, "main", exact=True)
            assert candidates, (
                f"{backend_name}: no executable 'main' found.\n"
                f"Files in bindir: {list(os.listdir(bindir)) if os.path.isdir(bindir) else 'N/A'}"
            )
            run_result = subprocess.run([candidates[0]], capture_output=True, text=True, timeout=10)
            assert run_result.returncode == 0, (
                f"{backend_name}: executable failed:\nstdout: {run_result.stdout}\nstderr: {run_result.stderr}"
            )
            assert "vec_size=5" in run_result.stdout, (
                f"{backend_name}: expected 'vec_size=5' in output, got: {run_result.stdout}"
            )
            assert "front=2" in run_result.stdout, (
                f"{backend_name}: expected 'front=2' in output, got: {run_result.stdout}"
            )


class TestBackendBuildGraphWithTests(BaseCompileToolsTestCase):
    """Verify build_graph() includes test targets for all backends."""

    @uth.requires_functional_compiler
    def test_build_graph_includes_runtests(self, tmp_path):
        """build_graph() with tests should include 'runtests' phony target."""
        with uth.ParserContext():
            # Use helloworld_cpp.cpp as both a regular and test target to verify graph structure
            shutil.copy2(uth.example_file("simple/helloworld_cpp.cpp"), tmp_path)
            source_path = os.path.realpath(os.path.join(tmp_path, "helloworld_cpp.cpp"))

            objdir = os.path.join(str(tmp_path), "obj")
            bindir = os.path.join(str(tmp_path), "bin")
            argv = [
                "--include",
                str(tmp_path),
                "--cas-objdir",
                objdir,
                "--bindir",
                bindir,
                "--tests",
                source_path,
            ]

            cap = compiletools.apptools.create_parser("Backend integration test", argv=argv)
            uth.add_backend_arguments(cap)
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
        content, graph = _render_backend_to_string("make", tmp_path)
        assert ".DELETE_ON_ERROR:" in content
        assert ".PHONY: build" in content
        assert ".PHONY: all" in content
        assert "\t" in content  # Tab-indented recipes
        for rule in graph.rules_by_type("compile"):
            assert rule.output in content

    def test_ninja_output_has_ninja_syntax(self, tmp_path):
        content, graph = _render_backend_to_string("ninja", tmp_path)
        assert "rule compile_cmd" in content
        assert "command = $cmd" in content
        assert "build build: phony" in content
        for rule in graph.rules_by_type("compile"):
            assert f"build {rule.output}:" in content

    def test_bazel_output_has_build_syntax(self, tmp_path):
        content, _graph = _render_backend_to_string("bazel", tmp_path)
        assert "cc_binary(" in content
        assert "srcs = [" in content
        assert "BUILD.bazel generated by compiletools" in content
        # Should have at least one source file referenced
        assert ".cpp" in content

    def test_cmake_output_has_cmake_syntax(self, tmp_path):
        content, _graph = _render_backend_to_string("cmake", tmp_path)
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
