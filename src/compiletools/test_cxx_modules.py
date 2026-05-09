"""Tests for C++20 modules support.

Phase 1: GCC-only (-fmodules-ts), auto-detected via FileAnalyzer.
See docs/superpowers/specs/2026-05-07-cxx-modules-design.md
"""

import functools
import os
import subprocess
import tempfile

import pytest
import stringzilla as sz

import compiletools.testhelper as uth

# ---------------------------------------------------------------------------
# Unit tests: pure helper for module-declaration extraction.
# These run without any compiler.
# ---------------------------------------------------------------------------


class TestExtractModuleDeclarations:
    """Test _extract_module_declarations on hand-crafted source strings."""

    def _classify(self, source: str):
        """Run ``_extract_module_declarations`` on a source-text snippet."""
        from compiletools.file_analyzer import (
            _compute_line_byte_offsets,
            _extract_module_declarations,
        )

        str_text = sz.Str(source)
        line_byte_offsets = _compute_line_byte_offsets(str_text)
        return _extract_module_declarations(str_text, line_byte_offsets)

    def test_export_module_declares_interface(self):
        source = "export module math;\nexport int add(int, int);\n"
        result = self._classify(source)
        assert result["export_module"] == ["math"]
        assert result["module"] == []
        assert result["import"] == []

    def test_module_declares_implementation_unit(self):
        source = "module math;\nint add(int a, int b) { return a + b; }\n"
        result = self._classify(source)
        assert result["export_module"] == []
        assert result["module"] == ["math"]
        assert result["import"] == []

    def test_import_collected(self):
        source = "import math;\nimport util;\nint main() { return 0; }\n"
        result = self._classify(source)
        assert result["export_module"] == []
        assert result["module"] == []
        assert result["import"] == ["math", "util"]

    def test_dotted_module_name(self):
        source = "export module my.lib.math;\n"
        result = self._classify(source)
        assert result["export_module"] == ["my.lib.math"]

    def test_global_module_fragment_opener_ignored(self):
        # `module;` with no name opens a global module fragment - it is NOT a
        # module-name declaration and must not be reported as one.
        source = "module;\n#include <vector>\nexport module m;\n"
        result = self._classify(source)
        assert result["module"] == []
        assert result["export_module"] == ["m"]

    def test_line_comments_ignored(self):
        source = "// export module fake;\n// import bogus;\nexport module real;\n"
        result = self._classify(source)
        assert result["export_module"] == ["real"]
        assert result["import"] == []

    def test_block_comments_ignored(self):
        source = "/* export module fake;\n   import bogus; */\nexport module real;\n"
        result = self._classify(source)
        assert result["export_module"] == ["real"]
        assert result["import"] == []

    def test_string_literal_ignored(self):
        source = 'const char* s = "export module fake;";\nimport real;\n'
        result = self._classify(source)
        assert result["export_module"] == []
        assert result["import"] == ["real"]

    def test_partition_export_recorded(self):
        # Phase 3: `export module M:P;` is an interface partition unit.
        source = "export module math:basic;\nexport int add(int,int);\n"
        result = self._classify(source)
        assert result["export_module"] == ["math:basic"]
        assert result["module"] == []
        assert result["import"] == []

    def test_partition_implementation_recorded(self):
        # `module M:P;` is a partition implementation unit.
        source = "module math:basic;\nint add(int a, int b) { return a + b; }\n"
        result = self._classify(source)
        assert result["module"] == ["math:basic"]
        assert result["export_module"] == []

    def test_partition_local_import_recorded(self):
        # `import :P;` inside a module M means import math:P. The
        # partition-relative form is preserved in the imports list; the
        # consumer (Hunter) is responsible for resolving it against the
        # importer's own module.
        source = "export module math;\nimport :basic;\nimport :advanced;\n"
        result = self._classify(source)
        assert result["export_module"] == ["math"]
        assert result["import"] == [":basic", ":advanced"]

    def test_partition_qualified_import_recorded(self):
        # `import M:P;` is the fully-qualified form, also legal.
        source = "module math;\nimport math:basic;\n"
        result = self._classify(source)
        assert result["module"] == ["math"]
        assert result["import"] == ["math:basic"]

    def test_export_import_partition(self):
        # Primary interface units commonly do `export import :P;` so
        # downstream `import math;` sees the partition's exports too.
        source = "export module math;\nexport import :basic;\n"
        result = self._classify(source)
        assert result["export_module"] == ["math"]
        assert result["import"] == [":basic"]

    def test_header_unit_imports_collected(self):
        # Phase 5: `import <h>;` and `import "h";` are header units. The
        # token form (with brackets/quotes) is preserved so build_backend
        # can re-emit it on the precompile / -fmodule-file= flags. They
        # are stored separately from named-module imports.
        source = 'import <vector>;\nimport "x.h";\nimport math;\n'
        result = self._classify(source)
        assert result["import"] == ["math"]
        assert result["header_import"] == ["<vector>", '"x.h"']


# ---------------------------------------------------------------------------
# FileAnalysisResult exposes the new fields.
# ---------------------------------------------------------------------------


class TestCompilerKindClassification:
    """apptools.compiler_kind classifies common compiler invocations."""

    def test_bare_gpp(self):
        from compiletools.apptools import compiler_kind

        # Even if g++ doesn't resolve, fallback should still recognize the name.
        assert compiler_kind("g++") == "gcc"

    def test_bare_gcc(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind("gcc") == "gcc"

    def test_versioned_clangpp(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind("/opt/llvm/bin/clang++-22.1.3") == "clang"

    def test_versioned_gpp(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind("/usr/bin/g++-15") == "gcc"

    def test_ccache_wrapper_gcc(self):
        from compiletools.apptools import compiler_kind

        # Pre-resolution string has the toolchain hint; the helper should
        # see it via the raw-string fallback.
        assert compiler_kind("ccache g++") == "gcc"

    def test_ccache_wrapper_clang(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind("ccache clang++") == "clang"

    def test_empty_or_none(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind(None) == "unknown"
        assert compiler_kind("") == "unknown"

    def test_unknown_basename(self):
        from compiletools.apptools import compiler_kind

        assert compiler_kind("/usr/bin/some-shim") == "unknown"


class TestFileAnalysisResultModuleFields:
    """Test that the new module-related fields land on FileAnalysisResult."""

    def test_default_fields_empty(self):
        from compiletools.file_analyzer import FileAnalysisResult

        result = FileAnalysisResult(
            line_count=0,
            line_byte_offsets=[],
            include_positions=[],
            magic_positions=[],
            directive_positions={},
            directives=[],
            directive_by_line={},
            bytes_analyzed=0,
            was_truncated=False,
        )
        assert result.module_exports == ()
        assert result.module_implements == ()
        assert result.module_imports == ()
        assert result.module_header_imports == ()


# ---------------------------------------------------------------------------
# Hunter-level tests: module-name -> interface-file lookup, and that an
# importer's `import M;` pulls in the interface unit as a source dep.
# These don't require a working compiler.
# ---------------------------------------------------------------------------


class TestHunterModuleGraph:
    """Verify Hunter discovers module interfaces and treats them as deps."""

    def _make_hunter(self, workdir):
        """Build a fully-wired Hunter rooted at `workdir`."""
        import configargparse

        import compiletools.apptools
        import compiletools.headerdeps
        import compiletools.hunter
        import compiletools.magicflags
        from compiletools.build_context import BuildContext

        argv = ["--include", str(workdir)]
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.hunter.add_arguments(cap)
        ctx = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        hdeps = compiletools.headerdeps.create(args, context=ctx)
        mfs = compiletools.magicflags.create(args, hdeps, context=ctx)
        return compiletools.hunter.Hunter(args, hdeps, mfs, context=ctx), ctx

    def test_module_interface_pulled_in_as_source_dep(self, tmp_path, monkeypatch):
        """An importer's `import math;` must add math.cppm to required sources."""
        # Lay down a minimal sample.
        (tmp_path / "math.cppm").write_text("export module math;\nexport int add(int,int);\n")
        (tmp_path / "main.cpp").write_text("// ct-exemarker\nimport math;\nint main(){return add(1,2);}\n")
        monkeypatch.chdir(tmp_path)

        hunter, _ = self._make_hunter(tmp_path)
        sources = hunter.required_source_files(str(tmp_path / "main.cpp"))
        sources_basenames = {os.path.basename(s) for s in sources}
        assert "math.cppm" in sources_basenames, (
            f"main.cpp imports `math` but math.cppm wasn't pulled in. required_source_files returned: {sources}"
        )

    def test_partition_imports_resolved_against_own_module(self, tmp_path, monkeypatch):
        """`import :basic;` inside the primary `export module math;` must
        pull math-basic.cppm into the dependency set."""
        (tmp_path / "math-basic.cppm").write_text("export module math:basic;\nexport int add(int,int) { return 0; }\n")
        (tmp_path / "math.cppm").write_text("export module math;\nexport import :basic;\n")
        (tmp_path / "main.cpp").write_text("// ct-exemarker\nimport math;\nint main(){return add(1,2);}\n")
        monkeypatch.chdir(tmp_path)

        hunter, _ = self._make_hunter(tmp_path)
        sources = hunter.required_source_files(str(tmp_path / "main.cpp"))
        names = {os.path.basename(s) for s in sources}
        assert "math.cppm" in names, f"primary not pulled in: {sources}"
        assert "math-basic.cppm" in names, f"partition not pulled in: {sources}"

    def test_qualified_partition_import_resolved(self, tmp_path, monkeypatch):
        """`import math:basic;` (fully qualified) must pull the partition in
        even from a TU that doesn't itself belong to the math module."""
        (tmp_path / "math-basic.cppm").write_text("export module math:basic;\nexport int add(int,int) { return 0; }\n")
        (tmp_path / "math.cppm").write_text("export module math;\nexport import :basic;\n")
        (tmp_path / "main.cpp").write_text("// ct-exemarker\nimport math:basic;\nint main(){return add(1,2);}\n")
        monkeypatch.chdir(tmp_path)

        hunter, _ = self._make_hunter(tmp_path)
        sources = hunter.required_source_files(str(tmp_path / "main.cpp"))
        names = {os.path.basename(s) for s in sources}
        assert "math-basic.cppm" in names, f"partition not pulled in: {sources}"

    def test_duplicate_module_exporters_when_imported_raises(self, tmp_path, monkeypatch):
        """Two files exporting the same module name raises at LOOKUP time
        (when an importer actually depends on that name).

        Registry-build-time tolerance lets a monorepo with multiple
        unrelated subtrees coexist (e.g. compiletools' own samples
        directory). A real conflict is surfaced when an importer
        references the ambiguous name -- the diagnostic carries the
        importer's path and the candidate sources, which is more
        useful than an eager error at registry build.
        """
        (tmp_path / "math.cppm").write_text("export module math;\nexport int x();\n")
        (tmp_path / "math2.cppm").write_text("export module math;\nexport int y();\n")
        (tmp_path / "main.cpp").write_text("// ct-exemarker\nimport math;\nint main(){return 0;}\n")
        monkeypatch.chdir(tmp_path)

        hunter, _ = self._make_hunter(tmp_path)
        with pytest.raises(Exception, match=r"(?i)duplicate.*module"):
            hunter.required_source_files(str(tmp_path / "main.cpp"))

    def test_repo_samples_with_shared_module_names_do_not_conflict(self, tmp_path, monkeypatch):
        """compiletools' own samples have THREE files exporting `module
        math` (cxx_modules/, cxx_modules_split/, cxx_modules_partitions/).
        The registry must tolerate this -- only an actual importer
        whose dep walk reaches multiple exporters should raise.

        This guards against the registry-build-time strictness regression
        that earlier development hit when ``trim_pchdir``-style scans
        unconditionally raised on any duplicate.
        """
        # Build a Hunter pointed at the real repo's samples dir.
        samples = uth.samplesdir()
        # Use a TU under cxx_modules/ that imports `math` -- this forces
        # the lookup. The bundled sample's math.cppm IS the unique
        # exporter reachable from this importer's project subtree, but
        # the registry will see all three sample dirs' math.cppm. The
        # current implementation picks the lex-first path; functionally,
        # the test demonstrates the registry build itself does not
        # raise, which is the regression we're guarding against.
        hunter, _ = self._make_hunter(samples)
        # Force registry build to verify no exception.
        hunter._module_interface_registry()
        # The full multi-exporter map should record `math` as
        # multiply-defined, available for a downstream importer's
        # use-time check.
        conflicts = getattr(hunter, "_module_export_conflicts", {})
        assert "math" in conflicts, (
            f"expected 'math' in _module_export_conflicts (samples have three math.cppm files); got {list(conflicts)}"
        )
        assert len(conflicts["math"]) >= 3, (
            f"expected at least 3 exporters of 'math' in samples; got {conflicts['math']}"
        )

    def test_duplicate_module_exporters_when_unimported_is_tolerated(self, tmp_path, monkeypatch):
        """Two files exporting the same module name DON'T raise when no
        importer references that name. Lets unrelated subtrees in a
        monorepo coexist (e.g. test sample directories that all happen
        to define `module math`)."""
        (tmp_path / "math.cppm").write_text("export module math;\nexport int x();\n")
        (tmp_path / "math2.cppm").write_text("export module math;\nexport int y();\n")
        # main.cpp does NOT import math.
        (tmp_path / "main.cpp").write_text("// ct-exemarker\nint main(){return 0;}\n")
        monkeypatch.chdir(tmp_path)

        hunter, _ = self._make_hunter(tmp_path)
        # Should succeed without raising.
        sources = hunter.required_source_files(str(tmp_path / "main.cpp"))
        assert "main.cpp" in os.path.basename(sources[0])


# ---------------------------------------------------------------------------
# End-to-end tests: build and run the cxx_modules sample(s).
# Skipped automatically if the toolchain doesn't support C++20 modules.
# ---------------------------------------------------------------------------


def _which(name: str) -> str | None:
    """Locate `name` on PATH, or return None if not present."""
    import shutil as _shutil

    return _shutil.which(name)


# Six requires_* markers below compose ``uth.skipif_e2e_unavailable``
# (which gates on venv-mismatch first, then the feature probe). See
# ``compiletools.testhelper.skipif_e2e_unavailable`` and
# ``compiletools.check_venv`` for the underlying machinery.


@functools.lru_cache(maxsize=16)
def _probe_modules_support(cxx: str | None, kind: str) -> bool:
    """Probe whether ``cxx`` accepts the right C++20 module flags for ``kind``.

    ``kind`` is one of ``"gcc"`` or ``"clang"`` and selects the flag set
    handed to the probe compiler. Returns False when ``cxx`` is missing
    or rejects the probe TU. ``lru_cache`` is keyed on the pair so the
    same compiler is only probed once per session; passing
    ``functools.lru_cache`` rather than a module-level dict makes the
    "this is a cache" intent explicit and gives the standard
    ``cache_clear()`` escape hatch should a test need to force a re-probe.
    """
    if not cxx:
        return False
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "probe.cppm")
        with open(src, "w") as f:
            f.write("export module probe;\nexport int answer() { return 42; }\n")
        if kind == "gcc":
            cmd = [cxx, "-std=c++20", "-fmodules-ts", "-x", "c++", "-c", src, "-o", os.path.join(td, "probe.o")]
        elif kind == "clang":
            # Probe partition support too: clang 13's --precompile accepts the
            # trivial primary-interface case but explicitly refuses partitions
            # ("sorry, module partitions are not yet supported"), and likewise
            # mishandles the impl-unit form used by the cxx_modules_split sample.
            # Falsely advertising clang 13 here makes the partition / split
            # tests fail rather than skip. Combine both into one TU so a single
            # invocation rules out clang versions that can't drive any of the
            # samples in this suite.
            with open(src, "w") as f:
                f.write("export module probe;\nexport import :part;\nexport int answer();\n")
            part_src = os.path.join(td, "probe-part.cppm")
            with open(part_src, "w") as f:
                f.write("export module probe:part;\nexport int answer() { return 42; }\n")
            try:
                rp = subprocess.run(
                    [
                        cxx,
                        "-std=c++20",
                        "-x",
                        "c++-module",
                        "--precompile",
                        part_src,
                        "-o",
                        os.path.join(td, "probe-part.pcm"),
                    ],
                    capture_output=True,
                    text=True,
                    cwd=td,
                    timeout=30,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False
            if rp.returncode != 0:
                return False
            cmd = [
                cxx,
                "-std=c++20",
                "-x",
                "c++-module",
                "--precompile",
                "-fmodule-file=probe:part=" + os.path.join(td, "probe-part.pcm"),
                src,
                "-o",
                os.path.join(td, "probe.pcm"),
            ]
        else:
            raise ValueError(f"unknown probe kind: {kind!r}")
        try:
            r = subprocess.run(cmd, capture_output=True, cwd=td, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0


def _detected_gcc_supports_modules() -> bool:
    """Probe modules support against the auto-detected functional g++."""
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        return False
    return _probe_modules_support(cxx, "gcc")


def _clang_path_for_modules() -> str | None:
    """Return a clang++ on PATH that accepts C++20 modules, or None."""
    cand = _which("clang++")
    if cand and _probe_modules_support(cand, "clang"):
        return cand
    return None


requires_cxx_modules = uth.skipif_e2e_unavailable(
    _detected_gcc_supports_modules,
    "C++20 modules (-fmodules-ts) not supported by detected g++",
)

requires_clang_modules = uth.skipif_e2e_unavailable(
    lambda: _clang_path_for_modules() is not None,
    "No clang++ on PATH that supports C++20 modules with partitions "
    "(clang 13 accepts --precompile but rejects partitions; need clang >=16)",
)


@requires_cxx_modules
def test_cxx_modules_simple_sample_builds_and_runs(tmp_path, monkeypatch):
    """Build the cxx_modules sample with ct-cake and run the resulting exe."""
    sample_src = uth.samplesdir() + "/cxx_modules"
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    # Copy sample into tmp_path so the build artifacts don't pollute the source.
    import shutil

    workdir = tmp_path / "cxx_modules"
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    # Run ct-cake --auto via subprocess so we exercise the same CLI users do.
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
    )
    assert r.returncode == 0, f"ct-cake --auto failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    # ct-cake copies executables to `bin/<exe>` next to the workdir.
    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced: stdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"executable returned {run.returncode}:\n{run.stdout}\n{run.stderr}"
    assert "add(2,3)=5" in run.stdout, f"unexpected output: {run.stdout!r}"


def _run_sample_with_compiler(sample_name: str, cxx: str, tmp_path, monkeypatch):
    """Copy a sample into tmp_path, run ct-cake --auto with CXX overridden,
    and assert the produced exe runs and prints the expected message.

    Uses environment overrides (CXX/CC/CPP) so this exercises exactly the
    same compile-rule-emission code path as a user invoking ct-cake from
    a config that pins the compiler.
    """
    import shutil

    sample_src = os.path.join(uth.samplesdir(), sample_name)
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"
    workdir = tmp_path / sample_name
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    # CC stays unchanged: the samples are pure C++.
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
        env=env,
    )
    assert r.returncode == 0, f"ct-cake --auto (CXX={cxx}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced (CXX={cxx}):\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"executable returned {run.returncode} (CXX={cxx}):\n{run.stdout}\n{run.stderr}"
    assert "add(2,3)=5" in run.stdout, f"unexpected output: {run.stdout!r}"


@requires_clang_modules
def test_cxx_modules_simple_sample_builds_with_clang(tmp_path, monkeypatch):
    """Same as the gcc simple test but force CXX=clang++."""
    cxx = _clang_path_for_modules()
    assert cxx, "requires_clang_modules guard should have skipped"
    _run_sample_with_compiler("cxx_modules", cxx, tmp_path, monkeypatch)


@requires_clang_modules
def test_cxx_modules_split_sample_builds_with_clang(tmp_path, monkeypatch):
    """Same as the gcc split-impl test but force CXX=clang++."""
    cxx = _clang_path_for_modules()
    assert cxx, "requires_clang_modules guard should have skipped"
    _run_sample_with_compiler("cxx_modules_split", cxx, tmp_path, monkeypatch)


def _run_partitions_sample_with(cxx: str, tmp_path, monkeypatch):
    """Build the cxx_modules_partitions sample and assert
    ``add(2,3)=5 mul(2,3)=6`` on stdout."""
    import shutil

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules_partitions")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"
    workdir = tmp_path / "cxx_modules_partitions"
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
        env=env,
    )
    assert r.returncode == 0, f"ct-cake --auto (CXX={cxx}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced (CXX={cxx}):\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    # The partition sample exercises both interface partitions; assert each
    # one's symbol contributed to the output so a partial-link bug fails
    # loudly rather than printing a confusing partial number.
    assert "add(2,3)=5" in run.stdout, f"add() missing/wrong: {run.stdout!r}"
    assert "mul(2,3)=6" in run.stdout, f"mul() missing/wrong: {run.stdout!r}"


@requires_cxx_modules
def test_cxx_modules_partitions_sample_builds_with_gcc(tmp_path, monkeypatch):
    """End-to-end build of the partitions sample under gcc."""
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    _run_partitions_sample_with(cxx, tmp_path, monkeypatch)


@requires_clang_modules
def test_cxx_modules_partitions_sample_builds_with_clang(tmp_path, monkeypatch):
    """End-to-end build of the partitions sample under clang."""
    cxx = _clang_path_for_modules()
    assert cxx, "requires_clang_modules guard should have skipped"
    _run_partitions_sample_with(cxx, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Phase 4: `import std;` end-to-end
# ---------------------------------------------------------------------------


def _run_import_std_sample_with(cxx: str, tmp_path, monkeypatch):
    """Build the cxx_modules_import_std sample and assert the program
    prints `add(2,3)=5`. Exercises the system-provided std module path."""
    import shutil

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules_import_std")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"
    workdir = tmp_path / "cxx_modules_import_std"
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"ct-cake --auto (CXX={cxx}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced (CXX={cxx}):\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "add(2,3)=5" in run.stdout, f"unexpected output: {run.stdout!r}"


def _gcc_supports_import_std() -> bool:
    """Probe the auto-detected g++ for `import std;` capability.

    Compiles ``<gcc-include>/c++/<ver>/bits/std.cc`` if present; returns
    True only when both the source exists and the compile succeeds.
    """
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        return False
    src = compiletools.apptools.find_system_std_module_source(cxx, "gcc")
    if not src:
        return False
    with tempfile.TemporaryDirectory() as td:
        try:
            # Match the sample's standard (-std=c++23 -- the floor for
            # std::println, which the sample uses). A compiler that
            # accepts the standard module source under c++23 will also
            # accept the sample, so probe and sample stay aligned.
            r = subprocess.run(
                [cxx, "-std=c++23", "-fmodules", "-c", src, "-o", os.path.join(td, "std.o")],
                capture_output=True,
                cwd=td,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0


def _clang_supports_import_std() -> bool:
    """Probe the clang++ on PATH for `import std;` capability via libc++."""
    import compiletools.apptools

    cxx = _which("clang++")
    if not cxx:
        return False
    src = compiletools.apptools.find_system_std_module_source(cxx, "clang")
    if not src:
        return False
    with tempfile.TemporaryDirectory() as td:
        try:
            r = subprocess.run(
                [
                    cxx,
                    "-std=c++23",
                    "-stdlib=libc++",
                    "-Wno-reserved-module-identifier",
                    "--precompile",
                    src,
                    "-o",
                    os.path.join(td, "std.pcm"),
                ],
                capture_output=True,
                cwd=td,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0


requires_gcc_import_std = uth.skipif_e2e_unavailable(
    _gcc_supports_import_std,
    "`import std;` not supported by detected g++ (no bits/std.cc)",
)

requires_clang_import_std = uth.skipif_e2e_unavailable(
    _clang_supports_import_std,
    "`import std;` not supported by detected clang++ (no libc++ std.cppm)",
)


@requires_gcc_import_std
def test_cxx_modules_import_std_builds_with_gcc(tmp_path, monkeypatch):
    """End-to-end `import std;` build with gcc."""
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    _run_import_std_sample_with(cxx, tmp_path, monkeypatch)


@requires_clang_import_std
def test_cxx_modules_import_std_builds_with_clang(tmp_path, monkeypatch):
    """End-to-end `import std;` build with clang."""
    cxx = _which("clang++")
    assert cxx, "requires_clang_import_std should have skipped"
    _run_import_std_sample_with(cxx, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Phase 5: header units (`import <vector>;`)
# ---------------------------------------------------------------------------


def _gcc_supports_header_units() -> bool:
    """Probe the auto-detected g++ for header-unit precompile support."""
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        return False
    with tempfile.TemporaryDirectory() as td:
        try:
            # Match the sample standard (header units are a C++20
            # feature; the sample's ct.conf pins -std=c++20).
            r = subprocess.run(
                [cxx, "-std=c++20", "-fmodules", "-c", "-x", "c++-system-header", "vector"],
                capture_output=True,
                cwd=td,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0


def _clang_supports_header_units() -> bool:
    """Probe clang++ on PATH for header-unit precompile *and consume* support.

    Just precompiling ``vector`` is not enough: on some libc++ builds (e.g.
    Termux) the precompile succeeds but consuming ``import <vector>;`` fails
    when clang internally tries to build module ``'std'`` against a libc++
    whose headers aren't module-clean. Probe the full round-trip so the
    decorator skips on broken stdlibs instead of failing the test.
    """
    cxx = _which("clang++")
    if not cxx:
        return False
    with tempfile.TemporaryDirectory() as td:
        pcm_path = os.path.join(td, "vector.pcm")
        try:
            r = subprocess.run(
                [
                    cxx,
                    "-std=c++20",
                    "-stdlib=libc++",
                    "-xc++-system-header",
                    "--precompile",
                    "vector",
                    "-o",
                    pcm_path,
                ],
                capture_output=True,
                cwd=td,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
        src_path = os.path.join(td, "probe.cpp")
        with open(src_path, "w") as f:
            f.write("import <vector>;\nint main(){std::vector<int> v{1};return v.empty();}\n")
        try:
            # `-fmodules` matches what build_backend.py:_clang_module_extras()
            # injects for any TU that mentions header units; it is what
            # turns the libc++ Clang module-map's top-level ``std`` module
            # into a build dependency on some libc++ installs (e.g. Termux),
            # so the probe must include it to detect the failure.
            r = subprocess.run(
                [
                    cxx,
                    "-std=c++20",
                    "-stdlib=libc++",
                    "-fmodules",
                    "-fmodule-file=<vector>=" + pcm_path,
                    "-c",
                    src_path,
                    "-o",
                    os.path.join(td, "probe.o"),
                ],
                capture_output=True,
                cwd=td,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0


requires_gcc_header_units = uth.skipif_e2e_unavailable(
    _gcc_supports_header_units,
    "header units (-fmodules + -x c++-system-header) not supported by detected g++",
)

requires_clang_header_units = uth.skipif_e2e_unavailable(
    _clang_supports_header_units,
    "header units (--precompile -xc++-system-header) not supported by detected clang++",
)


def _run_header_units_sample_with(cxx: str, tmp_path, monkeypatch):
    """Build the cxx_modules_header_units sample and assert it prints
    `vec_size=5 front=2`."""
    import shutil

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules_header_units")
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"
    workdir = tmp_path / "cxx_modules_header_units"
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"ct-cake --auto (CXX={cxx}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced (CXX={cxx}):\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "vec_size=5" in run.stdout, f"vector size missing/wrong: {run.stdout!r}"
    assert "front=2" in run.stdout, f"front element missing/wrong: {run.stdout!r}"


@requires_gcc_header_units
def test_cxx_modules_header_units_builds_with_gcc(tmp_path, monkeypatch):
    """End-to-end header-unit build with gcc."""
    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    _run_header_units_sample_with(cxx, tmp_path, monkeypatch)


@requires_clang_header_units
def test_cxx_modules_header_units_builds_with_clang(tmp_path, monkeypatch):
    """End-to-end header-unit build with clang."""
    cxx = _which("clang++")
    assert cxx, "requires_clang_header_units should have skipped"
    _run_header_units_sample_with(cxx, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Phase 6: cas-pcmdir cache hit on rebuild
# ---------------------------------------------------------------------------


@requires_clang_modules
def test_cas_pcmdir_clang_pcm_survives_rebuild(tmp_path, monkeypatch):
    """When ct-cake builds, deletes bin+cas-objdir, then rebuilds, the
    cached .pcm under cas-pcmdir must NOT be re-precompiled.

    Verifies the content-addressed-store contract: identical input
    (source content + compiler + flags) maps to the same on-disk path,
    and make's mtime check then skips the precompile.
    """
    import shutil

    cxx = _which("clang++")
    assert cxx, "requires_clang_modules guard should have skipped"

    # Need a real git root for the default cas-pcmdir resolution to land
    # under {git_root}/cas-pcmdir/{variant}/. `git init` in tmp_path is
    # enough; we don't even need a commit.
    sample_src = os.path.join(uth.samplesdir(), "cxx_modules")
    workdir = tmp_path / "cxx_modules"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx

    # First build.
    r1 = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r1.returncode == 0, f"first build failed:\n{r1.stdout}\n{r1.stderr}"
    pcm_files = list((workdir / "cas-pcmdir").rglob("*.pcm"))
    assert pcm_files, (
        f"first build produced no .pcm under cas-pcmdir: contents={list((workdir / 'cas-pcmdir').rglob('*'))}"
    )
    # Capture the mtime of every cached .pcm so we can prove none of
    # them got rewritten.
    mtimes_before = {p: p.stat().st_mtime_ns for p in pcm_files}

    # Tear down the per-build artefacts but KEEP cas-pcmdir. Then ensure
    # at least 1ns has passed so any rewrite would be visible in mtime.
    shutil.rmtree(workdir / "bin", ignore_errors=True)
    shutil.rmtree(workdir / "cas-objdir", ignore_errors=True)
    (workdir / "compile_commands.json").unlink(missing_ok=True)

    # Second build.
    r2 = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r2.returncode == 0, f"second build failed:\n{r2.stdout}\n{r2.stderr}"

    # Cache hit: every .pcm path that existed before still exists, with
    # the same mtime (i.e., make didn't re-run the precompile rule).
    for pcm_path, before in mtimes_before.items():
        assert pcm_path.exists(), f"cached .pcm vanished after rebuild: {pcm_path}"
        after = pcm_path.stat().st_mtime_ns
        assert before == after, (
            f"cas-pcmdir cache miss: {pcm_path} mtime changed "
            f"({before} -> {after}); the precompile re-ran when it should "
            f"have hit the cache."
        )

    # Final functional check: the executable still works.
    exe = workdir / "bin" / "main"
    assert exe.exists()
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert "add(2,3)=5" in run.stdout


@requires_cxx_modules
def test_gcc_mapper_records_partition_names_with_colon(tmp_path, monkeypatch):
    """The gcc -fmodule-mapper file maps module names verbatim, so
    partition names containing ``:`` (e.g. ``math:basic``) must appear
    unescaped in the mapper file -- gcc keys the lookup off the literal
    name. The .gcm filename on disk uses the ``^^`` escape (make-target
    safety) but the mapper key does NOT.

    Regression: an earlier draft applied the ``^^`` escape on both
    sides, which broke gcc's lookup for partitions.
    """
    import shutil

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        pytest.skip("no g++ on PATH")

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules_partitions")
    workdir = tmp_path / "cxx_modules_partitions"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"

    # The mapper now lives next to the makefile (was cas-objdir before
    # Phase 9 race fix). Find it under bin/<variant>/.
    mapper_candidates = list(workdir.glob("bin/*/.module-mapper.txt"))
    assert mapper_candidates, (
        f"no .module-mapper.txt under bin/; contents: {sorted(workdir.rglob('.module-mapper.txt'))}"
    )
    mapper = mapper_candidates[0]
    content = mapper.read_text()
    # Each line is "<module-name> <gcm-path>". Partition names use ':'.
    partition_lines = [ln for ln in content.splitlines() if "math:" in ln]
    assert partition_lines, f"no partition entries (math:basic / math:advanced) in mapper:\n{content}"
    for line in partition_lines:
        name = line.split()[0]
        assert ":" in name, f"partition name in mapper key lost its colon: {line!r}"
        # The on-disk .gcm path uses the ^^ escape.
        path = line.split(None, 1)[1]
        if "math:" in name:
            stem = name.replace(":", "^^")
            assert stem in path, f"path {path!r} should reference escaped stem {stem!r} for mapper line {line!r}"


@requires_cxx_modules
def test_cas_pcmdir_gcc_gcm_lands_in_cache(tmp_path, monkeypatch):
    """With gcc + cas-pcmdir, named-module .gcm artefacts land under
    ``<cas-pcmdir>/<variant>/<hash>/<name>.gcm`` (not gcc's default
    per-cwd ``gcm.cache/``).

    The gcc cache differs from clang's: gcc's single compile produces
    BOTH the .o (under cas-objdir) and the .gcm together, so the .gcm
    can't survive a wipe of cas-objdir. What the cache DOES give is
    per-variant isolation and the same content-addressed layout as
    clang -- variant switches don't mutually evict, and the trim tool
    can reason about both compilers' caches uniformly.
    """
    import shutil

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        pytest.skip("no g++ on PATH")

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules")
    workdir = tmp_path / "cxx_modules"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"

    # The .gcm must land under cas-pcmdir (proves the mapper redirected
    # it away from gcc's default `gcm.cache/`).
    gcm_files = list((workdir / "cas-pcmdir").rglob("*.gcm"))
    assert gcm_files, (
        f"first build produced no .gcm under cas-pcmdir: contents={list((workdir / 'cas-pcmdir').rglob('*'))}"
    )

    # And gcc's default gcm.cache/ should NOT have been used for the
    # mapped modules (we don't claim it's empty -- transitive deps gcc
    # discovers may still land there -- but `math.gcm` specifically
    # should be in cas-pcmdir, not the default).
    default_cache = workdir / "gcm.cache"
    if default_cache.exists():
        for stray in default_cache.rglob("math.gcm"):
            raise AssertionError(
                f"math.gcm landed in gcc's default gcm.cache/ at {stray}; "
                f"the -fmodule-mapper redirection didn't take effect."
            )

    # Functional check.
    exe = workdir / "bin" / "main"
    assert exe.exists()
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert "add(2,3)=5" in run.stdout


@requires_cxx_modules
def test_cas_pcmdir_gcc_header_unit_gcm_survives_rebuild(tmp_path, monkeypatch):
    """gcc header-unit .gcm survives a wipe of bin/ + cas-objdir/.

    Header units differ from named modules: gcc's header-unit
    precompile produces ONLY a .gcm (no companion .o), so the cache
    artefact is independent of cas-objdir. Removing cas-objdir doesn't
    trigger a header-unit re-precompile -- the cached .gcm is enough.
    """
    import shutil

    import compiletools.apptools

    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx or "g++" not in os.path.basename(cxx):
        pytest.skip("no g++ on PATH")

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules_header_units")
    workdir = tmp_path / "cxx_modules_header_units"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r1 = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r1.returncode == 0, f"first build failed:\n{r1.stdout}\n{r1.stderr}"
    gcm_files = list((workdir / "cas-pcmdir").rglob("*.gcm"))
    assert gcm_files, (
        f"first build produced no header-unit .gcm under cas-pcmdir: "
        f"contents={list((workdir / 'cas-pcmdir').rglob('*'))}"
    )
    mtimes_before = {p: p.stat().st_mtime_ns for p in gcm_files}

    shutil.rmtree(workdir / "bin", ignore_errors=True)
    shutil.rmtree(workdir / "cas-objdir", ignore_errors=True)
    (workdir / "compile_commands.json").unlink(missing_ok=True)

    r2 = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r2.returncode == 0, f"second build failed:\n{r2.stdout}\n{r2.stderr}"

    for gcm_path, before in mtimes_before.items():
        assert gcm_path.exists(), f"cached header-unit .gcm vanished: {gcm_path}"
        after = gcm_path.stat().st_mtime_ns
        assert before == after, (
            f"header-unit cache miss: {gcm_path} mtime changed "
            f"({before} -> {after}); precompile re-ran when it should "
            f"have hit the cache."
        )

    exe = workdir / "bin" / "main"
    assert exe.exists()
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert "vec_size=5" in run.stdout


@requires_clang_modules
def test_ct_trim_cache_evicts_old_pcm_entries(tmp_path, monkeypatch):
    """ct-trim-cache --cas-pcmdir-only evicts older cmd_hash dirs while
    keeping the current build's entries.

    Validates the build_backend + trim_cache contract: the manifests
    written during build are read by trim_cache to bucket entries
    correctly, so a faked-old cmd_hash dir gets dropped without
    touching the current build's cache.
    """
    import shutil

    cxx = _which("clang++")
    assert cxx, "requires_clang_modules guard should have skipped"

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules")
    workdir = tmp_path / "cxx_modules"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx

    # First, do a real build so the current cmd_hash dir is populated
    # with a manifest.
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"
    pcmdir_root = workdir / "cas-pcmdir" / "blank"
    real_dirs = [p for p in pcmdir_root.iterdir() if p.is_dir()]
    assert real_dirs, f"no cmd_hash dirs under {pcmdir_root}"
    real_dir = real_dirs[0]

    # Plant a fake stale cmd_hash dir, same bucket via manifest, aged
    # so the keep_count=1 policy retains the real entry over the fake.
    import time

    fake = pcmdir_root / ("0" * 16)
    fake.mkdir()
    (fake / "math.pcm").write_bytes(b"\x00" * 100)
    fake_manifest = {
        "bucket_key": str(workdir / "math.cppm"),  # same bucket as real
        "stage": "clang_module_interface",
        "compiler": cxx,
        "compiler_identity": "fake|0|0",
        "transitive_hashes": {},
    }
    import json

    (fake / "manifest.json").write_text(json.dumps(fake_manifest))
    old_mtime = time.time() - 86400  # one day old
    os.utime(fake, (old_mtime, old_mtime))

    r = subprocess.run(
        ["ct-trim-cache", "--cas-pcmdir-only", "--keep-count", "1", "--cas-pcmdir", str(pcmdir_root)],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=30,
        env=env,
    )
    assert r.returncode == 0, f"trim failed:\n{r.stdout}\n{r.stderr}"

    # Real (current) dir survives; fake (older, same bucket) was evicted.
    assert real_dir.exists(), f"current build's cmd_hash dir was evicted: {real_dir}\ntrim output:\n{r.stdout}"
    assert not fake.exists(), (
        f"older cmd_hash dir in same bucket should have been evicted: {fake}\ntrim output:\n{r.stdout}"
    )


@requires_clang_modules
def test_cas_pcmdir_path_layout_is_content_addressed(tmp_path, monkeypatch):
    """cas-pcmdir uses single-command_hash layout, mirroring cas-pchdir.

    Path: ``<root>/<variant>/<command_hash>/<name>.pcm`` where
    ``<command_hash>`` is 16 hex chars of sha256 over every input that
    affects the BMI bytes (compiler identity, flags, source content,
    transitive headers, stage marker).

    PCM deliberately does NOT use the object cache's 3-hash filename
    structure -- the compiler verifies BMIs at consume time, so a
    hypothetical 64-bit collision degrades to a slow re-precompile,
    never a miscompile. See ``_pcm_command_hash`` for the rationale.
    """
    import re
    import shutil

    cxx = _which("clang++")
    assert cxx, "requires_clang_modules guard should have skipped"

    sample_src = os.path.join(uth.samplesdir(), "cxx_modules")
    workdir = tmp_path / "cxx_modules"
    shutil.copytree(sample_src, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    monkeypatch.chdir(workdir)

    env = os.environ.copy()
    env["CXX"] = cxx
    env["CPP"] = cxx
    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=180,
        env=env,
    )
    assert r.returncode == 0, f"build failed:\n{r.stdout}\n{r.stderr}"

    pcm_files = list((workdir / "cas-pcmdir").rglob("*.pcm"))
    assert pcm_files, "no .pcm files materialized under cas-pcmdir"
    cmd_hash_re = re.compile(r"^[0-9a-f]{16}$")
    for p in pcm_files:
        # Expect: <workdir>/cas-pcmdir/<variant>/<command_hash>/<name>.pcm
        rel = p.relative_to(workdir / "cas-pcmdir")
        parts = rel.parts
        assert len(parts) == 3, (
            f"unexpected cas-pcmdir layout for {p}: relative parts={parts}; "
            f"expected <variant>/<command_hash>/<name>.pcm"
        )
        _variant, cmd_hash, _name = parts
        assert cmd_hash_re.match(cmd_hash), f"command_hash component {cmd_hash!r} for {p} isn't 16 hex chars"


class TestFindSystemStdModuleSource:
    """apptools.find_system_std_module_source returns a real file or None."""

    def test_gcc_finds_bits_std_cc(self):
        """If a g++ is available and ships bits/std.cc, the helper finds it."""
        import compiletools.apptools

        cxx = compiletools.apptools.get_functional_cxx_compiler()
        if not cxx or "g++" not in os.path.basename(cxx):
            pytest.skip("no g++ on PATH")
        path = compiletools.apptools.find_system_std_module_source(cxx, "gcc")
        if path is None:
            pytest.skip("g++ doesn't ship bits/std.cc on this host")
        assert os.path.isfile(path)
        with open(path) as f:
            content = f.read()
        # The libc++ std.cppm has a long `#include` block before its
        # `export module std;` declaration, so a small head-read isn't
        # sufficient. Read the whole file (~25 KB max).
        assert "module std" in content, f"expected std module declaration in {path}"

    def test_clang_finds_libcxx_std_cppm(self):
        """If a libc++-aware clang++ is on PATH, the helper finds std.cppm."""
        cxx = _which("clang++")
        if not cxx:
            pytest.skip("no clang++ on PATH")
        import compiletools.apptools

        path = compiletools.apptools.find_system_std_module_source(cxx, "clang")
        if path is None:
            pytest.skip("clang doesn't ship libc++ std.cppm on this host")
        assert os.path.isfile(path)
        with open(path) as f:
            content = f.read()
        # The libc++ std.cppm has a long `#include` block before its
        # `export module std;` declaration, so a small head-read isn't
        # sufficient. Read the whole file (~25 KB max).
        assert "module std" in content, f"expected std module declaration in {path}"

    def test_returns_none_for_unknown_kind(self):
        import compiletools.apptools

        assert compiletools.apptools.find_system_std_module_source("/bin/false", "msvc") is None
        assert compiletools.apptools.find_system_std_module_source(None, "gcc") is None


@requires_cxx_modules
def test_cxx_modules_split_implementation_unit(tmp_path, monkeypatch):
    """Same as above but with the implementation in a separate `module M;` TU."""
    sample_src = uth.samplesdir() + "/cxx_modules_split"
    assert os.path.isdir(sample_src), f"sample dir missing: {sample_src}"

    import shutil

    workdir = tmp_path / "cxx_modules_split"
    shutil.copytree(sample_src, workdir)
    monkeypatch.chdir(workdir)

    r = subprocess.run(
        ["ct-cake", "--auto"],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
    )
    assert r.returncode == 0, f"ct-cake --auto failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    exe = workdir / "bin" / "main"
    assert exe.exists(), f"executable not produced: stdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, f"executable returned {run.returncode}:\n{run.stdout}\n{run.stderr}"
    assert "add(2,3)=5" in run.stdout, f"unexpected output: {run.stdout!r}"
