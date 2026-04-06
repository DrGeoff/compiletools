from unittest.mock import MagicMock, patch

import pytest

from compiletools.build_backend import (
    BuildBackend,
    available_backends,
    compute_link_signature,
    get_backend_class,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule
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
        assert f"{bindir}/test_foo" in runtests_rule.inputs

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
        assert idx_next < idx_base, (
            f"Expected -llibnext before -llibbase in link command, got: {l_flags}"
        )

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
        assert l_flags.count("-llibbase") == 1, (
            f"Expected -llibbase exactly once, got: {l_flags}"
        )
