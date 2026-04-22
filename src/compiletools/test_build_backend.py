import json
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_backend import (
    BuildBackend,
    _gch_path,
    _pch_command_hash,
    available_backends,
    compute_link_signature,
    get_backend_class,
    register_backend,
)
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.makefile_backend import MakefileBackend
from compiletools.testhelper import (
    BackendTestContext,
    make_backend_args,
    make_mock_hunter,
    make_mock_namer,
    make_stub_backend_class,
)


class TestBuildBackendContract:
    """Verify the ABC contract: cannot instantiate, must implement all abstract methods."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract"):
            BuildBackend(args=MagicMock(), hunter=MagicMock())

    def test_concrete_subclass_must_implement_generate(self):
        class Incomplete(BuildBackend):
            pass

        with pytest.raises(TypeError, match="abstract"):
            Incomplete(args=MagicMock(), hunter=MagicMock())

    def test_concrete_subclass_works(self):
        class Minimal(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "minimal"

            @staticmethod
            def build_filename():
                return "Minimalfile"

        backend = Minimal(args=MagicMock(), hunter=MagicMock())
        assert backend.name() == "minimal"

    def test_must_supply_hunter_or_context(self):
        """Silent fallback to a fresh BuildContext defeated the
        BuildContext-mandatory refactor (commit e352d20c). Constructing a
        backend with no hunter and no context must raise so the caller
        is forced to thread the right one through."""

        class Minimal(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "minimal-orphan"

            @staticmethod
            def build_filename():
                return "Minimalfile"

        with pytest.raises(ValueError, match="BuildContext"):
            Minimal(args=MagicMock(), hunter=None)

    def test_explicit_context_without_hunter_works(self):
        """A caller may legitimately pass a context with no hunter
        (e.g. compilation_database stand-alone uses)."""
        from compiletools.build_context import BuildContext

        class Minimal(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "minimal-ctx"

            @staticmethod
            def build_filename():
                return "Minimalfile"

        ctx = BuildContext()
        backend = Minimal(args=MagicMock(), hunter=None, context=ctx)
        assert backend.context is ctx
        assert backend.build_filename() == "Minimalfile"

    def test_generate_accepts_output_none(self):
        """ABC generate() must accept output=None keyword argument."""

        class WithOutput(BuildBackend):
            def generate(self, graph, output=None):
                self.received_output = output

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "with_output"

            @staticmethod
            def build_filename():
                return "WithOutputFile"

        backend = WithOutput(args=MagicMock(), hunter=MagicMock())
        graph = BuildGraph()
        backend.generate(graph, output=None)
        assert backend.received_output is None

    def test_generate_accepts_output_filelike(self):
        """ABC generate() must accept a file-like output argument."""
        import io

        class WithOutput(BuildBackend):
            def generate(self, graph, output=None):
                self.received_output = output

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "with_output2"

            @staticmethod
            def build_filename():
                return "WithOutputFile2"

        backend = WithOutput(args=MagicMock(), hunter=MagicMock())
        graph = BuildGraph()
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        assert backend.received_output is buf


class TestBuildBackendCommon:
    """Test the common (non-abstract) methods provided by BuildBackend."""

    def test_build_graph_construction(self):
        """The base class should provide build_graph() that populates a BuildGraph
        from the hunter/namer data, reusable across all backends."""
        with BackendTestContext() as (backend, _args, _tmpdir):
            graph = backend.build_graph()
            assert isinstance(graph, BuildGraph)


class TestBackendRegistry:
    def test_register_and_retrieve(self):
        class FakeBackend(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "fake"

            @staticmethod
            def build_filename():
                return "Fakefile"

        register_backend(FakeBackend)
        assert get_backend_class("fake") is FakeBackend

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            get_backend_class("nonexistent_backend_xyz")

    def test_available_backends_returns_list(self):
        result = available_backends()
        assert isinstance(result, list)


class TestBuildGraphPopulation:
    """Test that build_graph() correctly populates a BuildGraph from hunter/namer data."""

    def _make_backend(self, tmp_path, args=None, hunter=None):
        StubClass = make_stub_backend_class()
        args = args or make_backend_args(tmp_path, filename=["/src/main.cpp"], CXXFLAGS="-O2 -std=c++17")
        hunter = hunter or make_mock_hunter(sources=["/src/main.cpp"], headers=["/src/util.h"])
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend

    def test_single_source_produces_compile_and_link_rules(self, tmp_path):
        backend = self._make_backend(tmp_path)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) >= 1, "Should have at least one compile rule"
        assert len(link_rules) >= 1, "Should have at least one link rule"

    def test_compile_rule_has_correct_command(self, tmp_path):
        backend = self._make_backend(tmp_path)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        assert len(compile_rules) >= 1
        rule = compile_rules[0]
        assert any("-c" in arg for arg in rule.command)
        assert rule.inputs[0] == "/src/main.cpp"

    def test_link_rule_references_object_outputs(self, tmp_path):
        backend = self._make_backend(tmp_path)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        object_outputs = {r.output for r in compile_rules}
        link_inputs = set(link_rules[0].inputs)
        assert object_outputs & link_inputs, "Link rule should reference compiled objects"

    def test_phony_targets_created(self, tmp_path):
        backend = self._make_backend(tmp_path)

        graph = backend.build_graph()

        phony_rules = [r for r in graph.rules if r.rule_type == "phony"]
        phony_names = {r.output for r in phony_rules}
        assert "all" in phony_names
        assert "build" in phony_names

    def test_objdir_creation_rule_exists(self, tmp_path):
        """build_graph() must include a rule to create the object directory."""
        backend = self._make_backend(tmp_path)

        graph = backend.build_graph()

        objdir = str(tmp_path / "obj")
        objdir_rules = [r for r in graph.rules if r.output == objdir]
        assert len(objdir_rules) == 1, (
            f"Expected a rule to create objdir {objdir!r}, got rules for: {[r.output for r in graph.rules]}"
        )
        assert "mkdir" in " ".join(objdir_rules[0].command), "objdir rule should use mkdir"

    def test_no_sources_produces_empty_graph(self, tmp_path):
        args = make_backend_args(tmp_path, filename=[], tests=[], static=[], dynamic=[])
        hunter = make_mock_hunter(sources=[])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) == 0
        assert len(link_rules) == 0

    def test_tests_produce_link_rules(self, tmp_path):
        """build_graph() should create link rules for args.tests."""
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1, "Should have link rule for test target"

    def test_tests_included_in_build_phony(self, tmp_path):
        """Test executables should be included in 'build' phony target deps."""
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/main.cpp", "/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        build_rule = graph.get_rule("build")
        assert build_rule is not None
        assert f"{bindir}/main" in build_rule.inputs
        assert f"{bindir}/test_foo" in build_rule.inputs

    def test_runtests_phony_created_when_tests_exist(self, tmp_path):
        """build_graph() should create 'runtests' phony when tests exist."""
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/main.cpp", "/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        assert "runtests" in graph.outputs
        runtests_rule = graph.get_rule("runtests")
        assert runtests_rule is not None
        assert runtests_rule.rule_type == "phony"
        # runtests depends on .result files, not raw executables
        assert f"{bindir}/test_foo.result" in runtests_rule.inputs

    def test_test_result_rules_created(self, tmp_path):
        """build_graph() should create test result rules with execution commands."""
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        rule = test_rules[0]
        assert rule.output == f"{bindir}/test_foo.result"
        assert rule.inputs == [f"{bindir}/test_foo"]
        # The recipe must NOT self-delete its own output (Fix 2): make's
        # mtime check skips re-running the test only when .result is fresher
        # than the executable.
        assert "rm" not in rule.command
        assert "touch" in rule.command
        assert f"{bindir}/test_foo" in rule.command

    def test_test_result_rules_include_testprefix(self, tmp_path):
        """Test result rules should include TESTPREFIX when set."""
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        args.TESTPREFIX = "valgrind --leak-check=full"
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        assert "valgrind" in test_rules[0].command
        assert "--leak-check=full" in test_rules[0].command

    def test_runtests_not_created_when_no_tests(self, tmp_path):
        """build_graph() should NOT create 'runtests' phony when no tests."""
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], tests=[])
        backend = self._make_backend(tmp_path, args=args)

        graph = backend.build_graph()

        assert "runtests" not in graph.outputs

    def test_runtests_in_all_deps(self, tmp_path):
        """'all' phony should include 'runtests' when tests exist."""
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/main.cpp", "/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        all_rule = graph.get_rule("all")
        assert all_rule is not None
        assert "runtests" in all_rule.inputs

    def test_pch_header_creates_gch_compile_rule(self, tmp_path):
        """PCH magic flag creates a compile rule for the .gch file."""
        import stringzilla as sz

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {sz.Str("CPPFLAGS"): [sz.Str("-DPCH_ACTIVE")]},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], CXXFLAGS="-O2")
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        graph = backend.build_graph()

        # Should have a compile rule producing the .gch file
        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1, f"Expected one .gch rule, got {[r.output for r in gch_rules]}"
        gch_rule = gch_rules[0]
        assert gch_rule.rule_type == "compile"
        assert "-x" in gch_rule.command
        assert "c++-header" in gch_rule.command
        assert "/src/stdafx.h" in gch_rule.command

        # PCH rule should include magic CPPFLAGS from the header
        assert "-DPCH_ACTIVE" in gch_rule.command

        # Source compile rule should depend on the .gch file
        source_rules = [r for r in graph.rules if r.output.endswith("main.o")]
        assert len(source_rules) == 1
        assert gch_rule.output in source_rules[0].inputs

    def test_pch_with_pchdir_creates_content_addressable_gch(self, tmp_path):
        """When pchdir is set, .gch files are placed under <pchdir>/<hash>/."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        gch_path = gch_rules[0].output
        assert gch_path.startswith(pchdir + "/")
        # Layout: <pchdir>/<hash>/stdafx.h.gch
        parts = gch_path[len(pchdir) + 1 :].split("/")
        assert len(parts) == 2
        assert len(parts[0]) == 16  # 16-char hex hash
        assert parts[1] == "stdafx.h.gch"

    def test_pch_without_pchdir_uses_legacy_path(self, tmp_path):
        """When pchdir is None, .gch files are placed next to the header."""
        import stringzilla as sz

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=None)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        assert gch_rules[0].output == "/src/stdafx.h.gch"

    def test_source_compile_includes_pch_include_dir(self, tmp_path):
        """Source compile commands get -I <pchdir>/<hash>/ when pchdir is set."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        source_rules = [r for r in graph.rules if r.output.endswith("main.o")]
        assert len(source_rules) == 1
        cmd = source_rules[0].command
        i_idx = cmd.index("-I")
        include_dir = cmd[i_idx + 1]
        assert include_dir.startswith(pchdir + "/")
        assert len(include_dir.split("/")[-1]) == 16  # hash directory

    def test_pchdir_mkdir_rule_created(self, tmp_path):
        """A mkdir rule is created for each PCH hash subdirectory."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        mkdir_rules = [r for r in graph.rules if r.rule_type == "mkdir" and r.output.startswith(pchdir)]
        assert len(mkdir_rules) == 1
        assert mkdir_rules[0].output.startswith(pchdir + "/")

    def test_multiple_pch_headers_different_hashes(self, tmp_path):
        """Two PCH headers with different magic flags get distinct hash dirs."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/alpha.h"), sz.Str("/src/beta.h")]},
            "/src/alpha.h": {sz.Str("CXXFLAGS"): [sz.Str("-DALPHA")]},
            "/src/beta.h": {sz.Str("CXXFLAGS"): [sz.Str("-DBETA")]},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 2, f"Expected 2 .gch rules, got {[r.output for r in gch_rules]}"
        hash_dirs = [os.path.basename(os.path.dirname(r.output)) for r in gch_rules]
        assert hash_dirs[0] != hash_dirs[1], "Different magic flags should produce different hash dirs"
        basenames = sorted(os.path.basename(r.output) for r in gch_rules)
        assert basenames == ["alpha.h.gch", "beta.h.gch"]

    def test_multiple_pch_headers_source_gets_multiple_include_dirs(self, tmp_path):
        """Source using multiple PCH headers gets -I for each."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/alpha.h"), sz.Str("/src/beta.h")]},
            "/src/alpha.h": {},
            "/src/beta.h": {sz.Str("CXXFLAGS"): [sz.Str("-DBETA")]},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        source_rules = [r for r in graph.rules if r.output.endswith("main.o")]
        assert len(source_rules) == 1
        cmd = source_rules[0].command
        i_indices = [i for i, v in enumerate(cmd) if v == "-I"]
        assert len(i_indices) == 2, f"Expected 2 -I flags, got {len(i_indices)}"
        include_dirs = [cmd[i + 1] for i in i_indices]
        assert all(d.startswith(pchdir + "/") for d in include_dirs)

    def test_no_pch_include_dir_when_pchdir_unset(self, tmp_path):
        """When pchdir is None, no -I flags are injected for PCH."""
        import stringzilla as sz

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=None)
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        graph = backend.build_graph()

        source_rules = [r for r in graph.rules if r.output.endswith("main.o")]
        assert len(source_rules) == 1
        assert "-I" not in source_rules[0].command


class TestCompilerWrapperSplit:
    """args.CC / args.CXX / args.LD may carry a wrapper prefix like
    ``ccache g++``. The kernel's execve() needs ['ccache', 'g++', ...],
    not the literal string 'ccache g++' as argv[0] (that fails ENOENT
    with filename='ccache g++'). Every site that builds a command list
    from these args must split via shlex/split_command_cached."""

    def _build(self, tmp_path, *, per_file_magicflags=None, sources=None,
               filename=None, tests=None, dynamic=None,
               CC="ccache gcc", CXX="ccache g++", LD="ccache g++"):
        args = make_backend_args(
            tmp_path,
            filename=filename if filename is not None else ["/src/main.cpp"],
            tests=tests or [],
            dynamic=dynamic or [],
            CC=CC,
            CXX=CXX,
            LD=LD,
            CXXFLAGS="-O2",
            CFLAGS="-O2",
        )
        hunter = make_mock_hunter(
            sources=sources if sources is not None else ["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=per_file_magicflags,
        )
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend

    def test_cxx_compile_command_splits_wrapper(self, tmp_path):
        backend = self._build(tmp_path)
        graph = backend.build_graph()

        compile_rules = [
            r for r in graph.rules
            if r.rule_type == "compile" and r.output.endswith("main.o")
        ]
        assert len(compile_rules) == 1
        cmd = compile_rules[0].command
        assert cmd[0] == "ccache", f"argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"argv[1] should be 'g++', got {cmd[1]!r}"

    def test_c_compile_command_splits_wrapper(self, tmp_path):
        backend = self._build(
            tmp_path,
            filename=["/src/main.c"],
            sources=["/src/main.c"],
        )
        graph = backend.build_graph()

        compile_rules = [
            r for r in graph.rules
            if r.rule_type == "compile" and "/src/main.c" in r.inputs
        ]
        assert len(compile_rules) == 1
        cmd = compile_rules[0].command
        assert cmd[0] == "ccache", f"argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "gcc", f"argv[1] should be 'gcc', got {cmd[1]!r}"

    def test_pch_compile_command_splits_wrapper(self, tmp_path):
        import stringzilla as sz

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        backend = self._build(tmp_path, per_file_magicflags=pch_flags)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        cmd = gch_rules[0].command
        assert cmd[0] == "ccache", f"PCH argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"PCH argv[1] should be 'g++', got {cmd[1]!r}"

    def test_link_command_splits_wrapper(self, tmp_path):
        backend = self._build(tmp_path)
        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        cmd = link_rules[0].command
        assert cmd[0] == "ccache", f"link argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"link argv[1] should be 'g++', got {cmd[1]!r}"

    def test_dynamic_lib_link_command_splits_wrapper(self, tmp_path):
        backend = self._build(
            tmp_path,
            filename=[],
            dynamic=["/src/mylib.cpp"],
            sources=["/src/mylib.cpp"],
        )
        graph = backend.build_graph()

        lib_rules = [r for r in graph.rules if r.rule_type == "shared_library"]
        assert len(lib_rules) == 1
        cmd = lib_rules[0].command
        assert cmd[0] == "ccache", f"shared-lib argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"shared-lib argv[1] should be 'g++', got {cmd[1]!r}"

    def test_plain_compiler_unchanged(self, tmp_path):
        """Without a wrapper prefix, command should still start with the bare compiler."""
        backend = self._build(tmp_path, CC="gcc", CXX="g++", LD="g++")
        graph = backend.build_graph()

        compile_rules = [
            r for r in graph.rules
            if r.rule_type == "compile" and r.output.endswith("main.o")
        ]
        assert len(compile_rules) == 1
        assert compile_rules[0].command[0] == "g++"


class TestPchManifest:
    def test_manifest_records_header_realpath_and_compiler_identity(self, tmp_path, monkeypatch):
        from compiletools.build_backend import _write_pch_manifest
        from compiletools.build_context import BuildContext
        pchdir = tmp_path / "pch"
        cmd_hash = "a" * 16
        header = tmp_path / "stdafx.h"
        header.write_text("#pragma once\n")

        _write_pch_manifest(
            pchdir=str(pchdir),
            cmd_hash=cmd_hash,
            pch_header=str(header),
            transitive_headers=[],
            cxx_command="g++",
            context=BuildContext(),
        )

        manifest_path = pchdir / cmd_hash / "manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["header_realpath"] == os.path.realpath(str(header))
        assert manifest["compiler"] == "g++"
        assert "compiler_identity" in manifest
        assert manifest["transitive_hashes"] == {}

    def test_manifest_write_is_atomic(self, tmp_path):
        from compiletools.build_backend import _write_pch_manifest
        from compiletools.build_context import BuildContext
        pchdir = tmp_path / "pch"
        cmd_hash = "b" * 16
        header = tmp_path / "stdafx.h"
        header.write_text("#pragma once\n")

        ctx = BuildContext()
        _write_pch_manifest(str(pchdir), cmd_hash, str(header), [], "g++", context=ctx)
        # Second write replaces, never leaves .tmp behind.
        _write_pch_manifest(str(pchdir), cmd_hash, str(header), [], "clang++", context=ctx)

        manifest_dir = pchdir / cmd_hash
        assert (manifest_dir / "manifest.json").is_file()
        leftovers = [p for p in manifest_dir.iterdir() if ".tmp" in p.name]
        assert leftovers == []
        manifest = json.loads((manifest_dir / "manifest.json").read_text())
        assert manifest["compiler"] == "clang++"

    def test_pch_rule_emission_writes_manifest(self, tmp_path):
        """Building a graph with a PCH header writes the manifest eagerly.

        Uses the real ``samples/pch/`` project with the full
        Hunter/headerdeps/magicflags chain — same pattern as
        ``test_backend_integration.TestBackendBuildPCH`` — so the test
        exercises the actual rule-emission path end to end.
        """
        pch_sample = os.path.join(uth.samplesdir(), "pch")
        for f in os.listdir(pch_sample):
            shutil.copy2(os.path.join(pch_sample, f), tmp_path)

        source_path = os.path.realpath(str(tmp_path / "pch_user.cpp"))
        pchdir = str(tmp_path / "pch")
        argv = [
            "--include", str(tmp_path),
            "--objdir", str(tmp_path / "obj"),
            "--bindir", str(tmp_path / "bin"),
            "--pchdir", pchdir,
            source_path,
        ]

        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("PCH manifest test", argv=argv)
            uth.add_backend_arguments(cap)
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

            backend = MakefileBackend(args=args, hunter=hunter, context=ctx)
            backend.build_graph()

        cmd_hash_dirs = [d for d in os.listdir(pchdir) if len(d) == 16]
        assert len(cmd_hash_dirs) == 1, f"expected one cmd_hash dir, got: {os.listdir(pchdir)}"
        with open(os.path.join(pchdir, cmd_hash_dirs[0], "manifest.json")) as f:
            manifest = json.loads(f.read())

        # The hunter may pick either the tmp copy or the sample copy of
        # stdafx.h depending on configured include order — either is a
        # legitimate manifest entry.
        assert manifest["header_realpath"].endswith("/stdafx.h")
        assert os.path.isfile(manifest["header_realpath"])
        assert manifest["compiler_identity"]
        assert isinstance(manifest["transitive_hashes"], dict)


class TestWarnIfPchdirNotCrossUserSafe:
    """The cross-user-safety warning is noise when pchdir is per-user
    (cwd-relative or under the build's bin tree)."""

    def setup_method(self):
        from compiletools.build_backend import _PCHDIR_WARNED
        _PCHDIR_WARNED.clear()

    def test_warns_for_shared_path(self, tmp_path, capsys):
        from compiletools.build_backend import _warn_if_pchdir_not_cross_user_safe
        shared = tmp_path / "shared_pch"
        shared.mkdir(mode=0o755)  # not group-writable, no SGID
        _warn_if_pchdir_not_cross_user_safe(str(shared), verbose=1)
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_skips_warning_for_cwd_relative_path(self, tmp_path, capsys, monkeypatch):
        from compiletools.build_backend import _warn_if_pchdir_not_cross_user_safe
        monkeypatch.chdir(tmp_path)
        cwd_pch = tmp_path / "bin" / "gcc.debug" / "pch"
        cwd_pch.mkdir(parents=True, mode=0o755)
        _warn_if_pchdir_not_cross_user_safe(str(cwd_pch), verbose=1)
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err

    def test_skips_warning_for_relative_path(self, tmp_path, capsys, monkeypatch):
        from compiletools.build_backend import _warn_if_pchdir_not_cross_user_safe
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bin" / "gcc.debug" / "pch").mkdir(parents=True, mode=0o755)
        _warn_if_pchdir_not_cross_user_safe("bin/gcc.debug/pch", verbose=1)
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err


class TestPchCommandHash:
    """Test _pch_command_hash() determinism and sensitivity."""

    def test_deterministic(self):
        from types import SimpleNamespace

        args = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(args, "/src/foo.h", [], [])
        h2 = _pch_command_hash(args, "/src/foo.h", [], [])
        assert h1 == h2

    def test_differs_for_different_flags(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="g++", CXXFLAGS="-O3")
        h1 = _pch_command_hash(args1, "/src/foo.h", [], [])
        h2 = _pch_command_hash(args2, "/src/foo.h", [], [])
        assert h1 != h2

    def test_differs_for_different_compiler(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="clang++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(args1, "/src/foo.h", [], [])
        h2 = _pch_command_hash(args2, "/src/foo.h", [], [])
        assert h1 != h2

    def test_includes_magic_flags(self):
        from types import SimpleNamespace

        import stringzilla as sz

        args = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(args, "/src/foo.h", [sz.Str("-DFOO")], [])
        h2 = _pch_command_hash(args, "/src/foo.h", [], [])
        assert h1 != h2


class TestGchPath:
    """Test _gch_path() with and without pchdir."""

    def test_legacy_path(self):
        assert _gch_path("/src/foo.h") == "/src/foo.h.gch"

    def test_pchdir_path(self):
        result = _gch_path("/src/foo.h", pchdir="/cache/pch", command_hash="abc123")
        assert result == "/cache/pch/abc123/foo.h.gch"

    def test_pchdir_without_hash_falls_back(self):
        assert _gch_path("/src/foo.h", pchdir="/cache/pch") == "/src/foo.h.gch"


class TestPchFileLocking:
    """Test that PCH compile rules are wrapped with file-locking like other compiles."""

    def _make_backend_with_locking(self, tmp_path, pchdir=None):
        import stringzilla as sz

        StubClass = make_stub_backend_class()
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp"],
            pchdir=pchdir,
            file_locking=True,
            sleep_interval_lockdir=0.1,
            sleep_interval_cifs=0.2,
            sleep_interval_flock_fallback=0.1,
            lock_warn_interval=60,
            lock_cross_host_timeout=600,
        )
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend

    def test_pch_compile_rule_has_compile_type(self, tmp_path):
        """PCH rules have rule_type='compile' so backends apply lock-wrapping."""
        pchdir = str(tmp_path / "pch")
        backend = self._make_backend_with_locking(tmp_path, pchdir=pchdir)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        assert gch_rules[0].rule_type == "compile"

    def test_pch_compile_command_has_output_flag(self, tmp_path):
        """PCH compile command ends with -o target, needed for _wrap_compile_cmd()."""
        pchdir = str(tmp_path / "pch")
        backend = self._make_backend_with_locking(tmp_path, pchdir=pchdir)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        cmd = gch_rules[0].command
        o_idx = cmd.index("-o")
        assert cmd[o_idx + 1] == gch_rules[0].output


class TestPchIncrementalHash:
    """Test that hash changes correctly track flag/compiler changes."""

    def _build_graph_with_flags(self, tmp_path, cxxflags, pchdir):
        import stringzilla as sz

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp"],
            pchdir=pchdir,
            CXXFLAGS=cxxflags,
        )
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend.build_graph()

    def test_same_flags_produce_same_gch_path(self, tmp_path):
        """Identical flags produce the same .gch path (cache hit)."""
        pchdir = str(tmp_path / "pch")
        g1 = self._build_graph_with_flags(tmp_path, "-O2", pchdir)
        g2 = self._build_graph_with_flags(tmp_path, "-O2", pchdir)

        gch1 = [r.output for r in g1.rules if r.output.endswith(".gch")]
        gch2 = [r.output for r in g2.rules if r.output.endswith(".gch")]
        assert gch1 == gch2

    def test_different_flags_produce_different_gch_path(self, tmp_path):
        """Different flags produce different .gch paths (cache miss)."""
        pchdir = str(tmp_path / "pch")
        g1 = self._build_graph_with_flags(tmp_path, "-O2", pchdir)
        g2 = self._build_graph_with_flags(tmp_path, "-O3 -DNDEBUG", pchdir)

        gch1 = [r.output for r in g1.rules if r.output.endswith(".gch")]
        gch2 = [r.output for r in g2.rules if r.output.endswith(".gch")]
        assert gch1 != gch2

    def test_different_compiler_produces_different_gch_path(self, tmp_path):
        """Different compiler produces different .gch paths."""
        import stringzilla as sz

        pchdir = str(tmp_path / "pch")
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }

        args1 = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir, CXX="g++")
        args2 = make_backend_args(tmp_path, filename=["/src/main.cpp"], pchdir=pchdir, CXX="clang++")
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            headers=["/src/util.h"],
            per_file_magicflags=pch_flags,
        )
        StubClass = make_stub_backend_class()

        b1 = StubClass(args=args1, hunter=hunter)
        b1.namer = make_mock_namer(args1)
        b2 = StubClass(args=args2, hunter=hunter)
        b2.namer = make_mock_namer(args2)

        g1 = b1.build_graph()
        g2 = b2.build_graph()

        gch1 = [r.output for r in g1.rules if r.output.endswith(".gch")]
        gch2 = [r.output for r in g2.rules if r.output.endswith(".gch")]
        assert gch1 != gch2

    def test_different_compiler_binary_at_same_path_produces_different_gch_path(self, tmp_path):
        """Regression: two compilers identifying as ``g++`` but
        resolving to *different* binaries (e.g. different versions on
        different users' $PATH) must NOT collide on the same cache key."""
        from compiletools.build_backend import _compiler_identity, _pch_command_hash

        # Build two distinct fake compiler binaries
        cxx_a = tmp_path / "cxx_a"
        cxx_b = tmp_path / "cxx_b"
        cxx_a.write_text("#!/bin/sh\necho A\n")
        cxx_b.write_text("#!/bin/sh\necho B - much longer body to ensure size differs\n")
        cxx_a.chmod(0o755)
        cxx_b.chmod(0o755)

        # Different identity — same logical command name resolves differently.
        id_a = _compiler_identity(str(cxx_a))
        id_b = _compiler_identity(str(cxx_b))
        assert id_a != id_b

        from types import SimpleNamespace

        args_a = SimpleNamespace(CXX=str(cxx_a), CXXFLAGS="-O2")
        args_b = SimpleNamespace(CXX=str(cxx_b), CXXFLAGS="-O2")

        hash_a = _pch_command_hash(args_a, "/src/stdafx.h", [], [])
        hash_b = _pch_command_hash(args_b, "/src/stdafx.h", [], [])
        assert hash_a != hash_b, (
            "Compilers with different binary identity must produce distinct "
            "PCH cache keys to prevent silent cross-user PCH-stamp rejection."
        )

    def test_compiler_identity_uses_nanosecond_mtime(self, tmp_path):
        """Sub-second compiler swap must not collide on the cache key.

        With int(st_mtime) (one-second resolution), two compiler binaries
        written within the same second to the same path of the same size
        produce identical identities — silently using a stale PCH. With
        st_mtime_ns the two writes are distinguishable.
        """
        from compiletools.build_backend import _compiler_identity

        cxx = tmp_path / "fastswap_cxx"
        cxx.write_text("#!/bin/sh\necho A\n")  # same content size A
        cxx.chmod(0o755)
        # Pin mtime to a known nanosecond value. lru_cache means we must
        # reset between calls.
        os.utime(cxx, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        _compiler_identity.cache_clear()
        id_a = _compiler_identity(str(cxx))

        # Same content (same size), same path, but mtime advanced by
        # 500ms — int(st_mtime) would collide here.
        os.utime(cxx, ns=(1_700_000_000_500_000_000, 1_700_000_000_500_000_000))
        _compiler_identity.cache_clear()
        id_b = _compiler_identity(str(cxx))

        assert id_a != id_b, (
            "compiler identity must include nanosecond mtime so a "
            "sub-second swap is detected; "
            f"got id_a={id_a!r}, id_b={id_b!r}"
        )

    def test_pch_cache_key_distinguishes_quoted_flag_values(self, tmp_path):
        """Cache key must distinguish ``-DFOO="a b"`` (one flag with embedded
        space) from ``-DFOO=a -Db`` (two flags). The pre-fix space-join
        collided these."""
        from types import SimpleNamespace

        import stringzilla as sz

        from compiletools.build_backend import _pch_command_hash

        args = SimpleNamespace(CXX="cc", CXXFLAGS="-O2")
        flags_one = [sz.Str('-DFOO="a b"')]
        flags_two = [sz.Str("-DFOO=a"), sz.Str("-Db")]
        h1 = _pch_command_hash(args, "/src/stdafx.h", [], flags_one)
        h2 = _pch_command_hash(args, "/src/stdafx.h", [], flags_two)
        assert h1 != h2

    def test_warns_when_pchdir_not_group_writable(self, tmp_path, capsys):
        """Regression: a one-time stderr warning is emitted when
        the pchdir parent is not group-writable + SGID, so cross-user
        cache misses don't surprise operators."""
        import stringzilla as sz

        from compiletools.build_backend import _PCHDIR_WARNED

        pchdir = tmp_path / "pch"
        pchdir.mkdir(mode=0o700)  # missing group-write AND SGID
        _PCHDIR_WARNED.discard(str(pchdir))  # reset one-time guard

        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp"],
            pchdir=str(pchdir),
            verbose=1,
        )
        hunter = make_mock_hunter(
            sources=["/src/main.cpp"],
            per_file_magicflags=pch_flags,
        )
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend.build_graph()

        err = capsys.readouterr().err
        assert "PCH directory" in err
        assert "group-writable" in err or "SGID" in err


class TestRunTests:
    """Test the _run_tests() method."""

    def _make_backend(self, tmp_path, args=None):
        StubClass = make_stub_backend_class()
        args = args or make_backend_args(tmp_path, tests=["/src/test_foo.cpp"])
        hunter = MagicMock()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend

    @patch("subprocess.run")
    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_calls_subprocess(self, mock_realpath, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backend = self._make_backend(tmp_path)

        backend._run_tests()

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == [f"{tmp_path}/bin/test_foo"]

    @patch("subprocess.run")
    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_raises_on_failure(self, mock_realpath, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="test failed")
        backend = self._make_backend(tmp_path)

        with pytest.raises(RuntimeError, match="Test failures"):
            backend._run_tests()

    @patch("subprocess.run")
    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_no_tests_is_noop(self, mock_realpath, mock_run, tmp_path):
        args = make_backend_args(tmp_path, tests=[])
        backend = self._make_backend(tmp_path, args=args)

        backend._run_tests()

        mock_run.assert_not_called()


class TestExtractLinkopts:
    """extract_linkopts must normalise paths before stripping object files."""

    def test_normalises_dot_slash_prefix(self):
        from compiletools.build_backend import extract_linkopts

        cmd = ["g++", "-o", "bin/foo", "./obj/foo.o", "obj/bar.o", "-lm"]
        # Object set uses bare form; cmd uses ./obj/foo.o
        objs = {"obj/foo.o", "obj/bar.o"}
        result = extract_linkopts(cmd, objs)
        assert result == ["-lm"], (
            f"extract_linkopts must normalise so ./obj/foo.o and obj/foo.o compare equal; got {result}"
        )

    def test_normalises_with_redundant_slashes(self):
        from compiletools.build_backend import extract_linkopts

        cmd = ["g++", "-o", "bin/foo", "obj//foo.o", "-lm"]
        objs = {"obj/foo.o"}
        result = extract_linkopts(cmd, objs)
        assert result == ["-lm"]

    def test_normalises_when_object_set_uses_dot_slash(self):
        from compiletools.build_backend import extract_linkopts

        cmd = ["g++", "-o", "bin/foo", "obj/foo.o", "-lm"]
        # Object set has ./ prefix
        objs = {"./obj/foo.o"}
        result = extract_linkopts(cmd, objs)
        assert result == ["-lm"]


class TestComputeLinkSignature:
    """Test compute_link_signature determinism and sensitivity."""

    def test_deterministic(self):
        rule = BuildRule(
            output="bin/main",
            inputs=["obj/a.o", "obj/b.o"],
            command=["g++", "-o", "bin/main", "obj/a.o", "obj/b.o"],
            rule_type="link",
        )
        sig1 = compute_link_signature(rule)
        sig2 = compute_link_signature(rule)
        assert sig1 == sig2

    def test_input_order_irrelevant(self):
        rule1 = BuildRule(
            output="bin/main",
            inputs=["obj/b.o", "obj/a.o"],
            command=["g++", "-o", "bin/main", "obj/a.o", "obj/b.o"],
            rule_type="link",
        )
        rule2 = BuildRule(
            output="bin/main",
            inputs=["obj/a.o", "obj/b.o"],
            command=["g++", "-o", "bin/main", "obj/a.o", "obj/b.o"],
            rule_type="link",
        )
        assert compute_link_signature(rule1) == compute_link_signature(rule2)

    def test_differs_for_different_inputs(self):
        rule1 = BuildRule(
            output="bin/main",
            inputs=["obj/a.o"],
            command=["g++", "-o", "bin/main", "obj/a.o"],
            rule_type="link",
        )
        rule2 = BuildRule(
            output="bin/main",
            inputs=["obj/b.o"],
            command=["g++", "-o", "bin/main", "obj/b.o"],
            rule_type="link",
        )
        assert compute_link_signature(rule1) != compute_link_signature(rule2)

    def test_differs_for_different_command(self):
        rule1 = BuildRule(
            output="bin/main",
            inputs=["obj/a.o"],
            command=["g++", "-o", "bin/main", "obj/a.o"],
            rule_type="link",
        )
        rule2 = BuildRule(
            output="bin/main",
            inputs=["obj/a.o"],
            command=["g++", "-O2", "-o", "bin/main", "obj/a.o"],
            rule_type="link",
        )
        assert compute_link_signature(rule1) != compute_link_signature(rule2)


class TestDefaultClean:
    """Test the default clean() method on BuildBackend."""

    def _make_backend(self, exe_dir, obj_dir):
        StubClass = make_stub_backend_class()
        args = MagicMock()
        hunter = MagicMock()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = MagicMock()
        backend.namer.executable_dir.return_value = str(exe_dir)
        backend.namer.object_dir.return_value = str(obj_dir)
        return backend

    def test_default_clean_removes_directories(self, tmp_path):
        """clean() should remove both objdir and exedir."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        exe_dir.mkdir()
        obj_dir.mkdir()
        (exe_dir / "main").write_text("binary")
        (obj_dir / "main.o").write_text("object")

        backend = self._make_backend(exe_dir, obj_dir)
        backend.clean()

        assert not exe_dir.exists()
        assert not obj_dir.exists()

    def test_clean_skips_nonexistent_directories(self, tmp_path):
        """clean() should not error when directories don't exist."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"

        backend = self._make_backend(exe_dir, obj_dir)
        # Should not raise
        backend.clean()


class TestDefaultRealclean:
    """Test the default realclean() method on BuildBackend."""

    def _make_backend(self, exe_dir, obj_dir):
        StubClass = make_stub_backend_class()
        args = MagicMock()
        hunter = MagicMock()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = MagicMock()
        backend.namer.executable_dir.return_value = str(exe_dir)
        backend.namer.object_dir.return_value = str(obj_dir)
        return backend

    def test_realclean_removes_exe_dir_entirely(self, tmp_path):
        """realclean() should rm -rf the exe dir."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        exe_dir.mkdir()
        obj_dir.mkdir()
        (exe_dir / "main").write_text("binary")

        graph = BuildGraph()
        backend = self._make_backend(exe_dir, obj_dir)
        backend.realclean(graph)

        assert not exe_dir.exists()

    def test_realclean_selectively_removes_objects(self, tmp_path):
        """realclean() should only remove objects listed in the build graph."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        exe_dir.mkdir()
        obj_dir.mkdir()

        # This build's object
        our_obj = obj_dir / "main.o"
        our_obj.write_text("our object")
        # Another sub-project's object
        other_obj = obj_dir / "other.o"
        other_obj.write_text("other object")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=str(our_obj),
                inputs=["main.cpp"],
                command=["g++", "-c", "main.cpp"],
                rule_type="compile",
            )
        )

        backend = self._make_backend(exe_dir, obj_dir)
        backend.realclean(graph)

        assert not our_obj.exists(), "our object should be removed"
        assert other_obj.exists(), "other sub-project's object should be preserved"
        assert obj_dir.exists(), "shared objdir itself should be preserved"

    def test_realclean_removes_link_outputs(self, tmp_path):
        """realclean() should also remove link outputs from the graph."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        exe_dir.mkdir()
        obj_dir.mkdir()

        linked = obj_dir / "main"
        linked.write_text("linked binary")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=str(linked),
                inputs=["main.o"],
                command=["g++", "-o", str(linked), "main.o"],
                rule_type="link",
            )
        )

        backend = self._make_backend(exe_dir, obj_dir)
        backend.realclean(graph)

        assert not linked.exists()

    def test_realclean_skips_nonexistent_files(self, tmp_path):
        """realclean() should not error when build products don't exist yet."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        obj_dir.mkdir()

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=str(obj_dir / "missing.o"),
                inputs=["missing.cpp"],
                command=["g++", "-c", "missing.cpp"],
                rule_type="compile",
            )
        )

        backend = self._make_backend(exe_dir, obj_dir)
        # Should not raise
        backend.realclean(graph)

    def test_realclean_removes_pch_gch_from_pchdir(self, tmp_path):
        """realclean() should remove .gch files from shared pchdir."""
        exe_dir = tmp_path / "exe"
        obj_dir = tmp_path / "obj"
        pch_dir = tmp_path / "pch" / "abc123"
        exe_dir.mkdir()
        obj_dir.mkdir()
        pch_dir.mkdir(parents=True)

        gch_file = pch_dir / "stdafx.h.gch"
        gch_file.write_text("precompiled header")

        # Another project's gch in a different hash dir — should NOT be removed
        other_hash_dir = tmp_path / "pch" / "def456"
        other_hash_dir.mkdir(parents=True)
        other_gch = other_hash_dir / "stdafx.h.gch"
        other_gch.write_text("other project's precompiled header")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=str(gch_file),
                inputs=["stdafx.h"],
                command=["g++", "-x", "c++-header", "stdafx.h", "-o", str(gch_file)],
                rule_type="compile",
            )
        )

        backend = self._make_backend(exe_dir, obj_dir)
        backend.realclean(graph)

        assert not gch_file.exists(), ".gch from this build should be removed"
        assert other_gch.exists(), "other project's .gch should be preserved"


class TestAllOutputsCurrent:
    """Test _all_outputs_current pre-check logic."""

    def _make_backend(self):
        StubClass = make_stub_backend_class()
        args = MagicMock()
        hunter = MagicMock()
        return StubClass(args=args, hunter=hunter)

    def test_returns_true_when_all_exist_and_sigs_match(self, tmp_path):
        backend = self._make_backend()
        obj_path = str(tmp_path / "foo.o")
        exe_path = str(tmp_path / "main")

        # Create files
        with open(obj_path, "w") as f:
            f.write("object")
        with open(exe_path, "w") as f:
            f.write("executable")

        graph = BuildGraph()
        graph.add_rule(BuildRule(output=obj_path, inputs=["foo.cpp"], command=["g++"], rule_type="compile"))
        link_rule = BuildRule(
            output=exe_path, inputs=[obj_path], command=["g++", "-o", exe_path, obj_path], rule_type="link"
        )
        graph.add_rule(link_rule)

        # Write matching sig
        sig = compute_link_signature(link_rule)
        with open(exe_path + ".ct-sig", "w") as f:
            f.write(sig)

        assert backend._all_outputs_current(graph) is True

    def test_returns_false_when_compile_output_missing(self, tmp_path):
        backend = self._make_backend()
        exe_path = str(tmp_path / "main")
        with open(exe_path, "w") as f:
            f.write("executable")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(output=str(tmp_path / "foo.o"), inputs=["foo.cpp"], command=["g++"], rule_type="compile")
        )

        assert backend._all_outputs_current(graph) is False

    def test_returns_false_when_link_sig_differs(self, tmp_path):
        backend = self._make_backend()
        obj_path = str(tmp_path / "foo.o")
        exe_path = str(tmp_path / "main")

        with open(obj_path, "w") as f:
            f.write("object")
        with open(exe_path, "w") as f:
            f.write("executable")

        graph = BuildGraph()
        graph.add_rule(BuildRule(output=obj_path, inputs=["foo.cpp"], command=["g++"], rule_type="compile"))
        graph.add_rule(
            BuildRule(output=exe_path, inputs=[obj_path], command=["g++", "-o", exe_path, obj_path], rule_type="link")
        )

        # Write wrong sig
        with open(exe_path + ".ct-sig", "w") as f:
            f.write("wrong_signature")

        assert backend._all_outputs_current(graph) is False

    def test_returns_false_when_link_output_missing(self, tmp_path):
        backend = self._make_backend()
        obj_path = str(tmp_path / "foo.o")
        with open(obj_path, "w") as f:
            f.write("object")

        graph = BuildGraph()
        graph.add_rule(BuildRule(output=obj_path, inputs=["foo.cpp"], command=["g++"], rule_type="compile"))
        graph.add_rule(BuildRule(output=str(tmp_path / "main"), inputs=[obj_path], command=["g++"], rule_type="link"))

        assert backend._all_outputs_current(graph) is False

    def test_returns_false_when_no_compile_or_link_rules(self):
        """Empty graph (e.g. library builds) should not short-circuit."""
        backend = self._make_backend()
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="build", inputs=[], command=None, rule_type="phony"))
        assert backend._all_outputs_current(graph) is False


class TestLinkOrderCorrectness:
    """Test that link rules produce correctly ordered -l flags
    when different source files contribute different LDFLAGS."""

    def test_link_rule_respects_dependency_order(self, tmp_path):
        """When one.cpp has -llibbase and two.cpp has -llibnext -llibbase,
        the link command must have -llibnext before -llibbase."""
        import stringzilla as sz

        sources = ["/src/one.cpp", "/src/two.cpp"]
        per_file = {
            "/src/one.cpp": {sz.Str("LDFLAGS"): [sz.Str("-llibbase")]},
            "/src/two.cpp": {sz.Str("LDFLAGS"): [sz.Str("-llibnext"), sz.Str("-llibbase")]},
        }
        args = make_backend_args(tmp_path, filename=sources, CXXFLAGS="-O2")
        hunter = make_mock_hunter(sources=sources, per_file_magicflags=per_file)
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        link_cmd = link_rules[0].command
        l_flags = [f for f in link_cmd if f.startswith("-l")]
        assert "-llibnext" in l_flags, f"Expected -llibnext in link command, got: {link_cmd}"
        assert "-llibbase" in l_flags, f"Expected -llibbase in link command, got: {link_cmd}"
        idx_next = l_flags.index("-llibnext")
        idx_base = l_flags.index("-llibbase")
        assert idx_next < idx_base, f"Expected -llibnext before -llibbase in link command, got: {l_flags}"

    def test_link_rule_deduplicates_l_flags(self, tmp_path):
        """Same -l flag from multiple files should appear only once."""
        import stringzilla as sz

        sources = ["/src/one.cpp", "/src/two.cpp"]
        per_file = {
            "/src/one.cpp": {sz.Str("LDFLAGS"): [sz.Str("-llibbase")]},
            "/src/two.cpp": {sz.Str("LDFLAGS"): [sz.Str("-llibbase")]},
        }
        args = make_backend_args(tmp_path, filename=sources, CXXFLAGS="-O2")
        hunter = make_mock_hunter(sources=sources, per_file_magicflags=per_file)
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        link_cmd = link_rules[0].command
        l_flags = [f for f in link_cmd if f.startswith("-llib")]
        assert l_flags.count("-llibbase") == 1, f"Expected -llibbase exactly once, got: {l_flags}"

    def test_hard_orderings_win_over_opposing_soft_constraints(self, tmp_path):
        """Regression: a multi-package PKG-CONFIG hard ordering must
        beat a single-file LDFLAGS soft ordering that disagrees with it.
        End-to-end through magicflags -> _merge_ldflags_for_sources ->
        final link line."""
        import stringzilla as sz

        sources = ["/src/one.cpp", "/src/two.cpp"]
        # one.cpp has a single-package PKG-CONFIG worth of soft ordering:
        # libbase before libssh2 (i.e. soft edge libbase -> libssh2)
        # two.cpp has a multi-package PKG-CONFIG hard ordering that says
        # libssh2 before libbase (hard edge libssh2 -> libbase). The hard
        # edge wins; the soft edge is cancelled (or overridden); final
        # link has -llibssh2 before -llibbase.
        from compiletools.magicflags import _HARD_ORDERINGS_KEY

        per_file = {
            "/src/one.cpp": {
                sz.Str("LDFLAGS"): [sz.Str("-llibbase"), sz.Str("-llibssh2")],
            },
            "/src/two.cpp": {
                sz.Str("LDFLAGS"): [sz.Str("-llibssh2"), sz.Str("-llibbase")],
                _HARD_ORDERINGS_KEY: [(sz.Str("libssh2"), sz.Str("libbase"))],
            },
        }
        args = make_backend_args(tmp_path, filename=sources, CXXFLAGS="-O2")
        hunter = make_mock_hunter(sources=sources, per_file_magicflags=per_file)
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        link_cmd = link_rules[0].command
        l_positions = {flag: i for i, flag in enumerate(link_cmd) if flag.startswith("-llib")}
        assert "-llibssh2" in l_positions and "-llibbase" in l_positions
        assert l_positions["-llibssh2"] < l_positions["-llibbase"], (
            f"hard ordering libssh2 -> libbase ignored. Link command: {link_cmd}"
        )

    def test_two_hard_orderings_in_conflict_raise_cycle_error(self, tmp_path):
        """Two hard orderings in opposite directions form a genuine cycle —
        end-to-end this must raise (cycle-error formatter is invoked
        elsewhere)."""
        import stringzilla as sz

        from compiletools.magicflags import _HARD_ORDERINGS_KEY

        sources = ["/src/a.cpp", "/src/b.cpp"]
        per_file = {
            "/src/a.cpp": {
                sz.Str("LDFLAGS"): [sz.Str("-lA"), sz.Str("-lB")],
                _HARD_ORDERINGS_KEY: [(sz.Str("A"), sz.Str("B"))],
            },
            "/src/b.cpp": {
                sz.Str("LDFLAGS"): [sz.Str("-lB"), sz.Str("-lA")],
                _HARD_ORDERINGS_KEY: [(sz.Str("B"), sz.Str("A"))],
            },
        }
        args = make_backend_args(tmp_path, filename=sources, CXXFLAGS="-O2")
        hunter = make_mock_hunter(sources=sources, per_file_magicflags=per_file)
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        with pytest.raises(ValueError, match="cycle"):
            backend.build_graph()
