from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from compiletools.build_backend import BuildBackend, available_backends, get_backend_class, register_backend
from compiletools.build_graph import BuildGraph


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
            def generate(self, graph):
                pass

            def execute(self, target="build"):
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


class TestBuildBackendCommon:
    """Test the common (non-abstract) methods provided by BuildBackend."""

    def _make_backend(self):
        class Stub(BuildBackend):
            def generate(self, graph):
                self.last_graph = graph

            def execute(self, target="build"):
                pass

            @staticmethod
            def name():
                return "stub"

            @staticmethod
            def build_filename():
                return "Stubfile"

        return Stub

    def test_build_graph_construction(self):
        """The base class should provide build_graph() that populates a BuildGraph
        from the hunter/namer data, reusable across all backends."""
        StubClass = self._make_backend()
        args = MagicMock()
        args.filename = []
        args.tests = []
        args.static = []
        args.dynamic = []
        args.verbose = 0
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = StubClass(args=args, hunter=hunter)
        graph = backend.build_graph()
        assert isinstance(graph, BuildGraph)


class TestBackendRegistry:
    def test_register_and_retrieve(self):
        class FakeBackend(BuildBackend):
            def generate(self, graph):
                pass

            def execute(self, target="build"):
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


def _make_stub_backend_class():
    """Create a concrete BuildBackend subclass for testing."""

    class StubBackend(BuildBackend):
        def generate(self, graph):
            self.last_graph = graph

        def execute(self, target="build"):
            pass

        @staticmethod
        def name():
            return "stub_test"

        @staticmethod
        def build_filename():
            return "Stubfile"

    return StubBackend


class TestBuildGraphPopulation:
    """Test that build_graph() correctly populates a BuildGraph from hunter/namer data."""

    def _make_args(self, **overrides):
        defaults = dict(
            filename=["/src/main.cpp"],
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
            shared_objects=False,
            serialisetests=False,
            build_only_changed=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _make_hunter(self, sources=None, headers=None, magicflags_map=None):
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        sources = sources or ["/src/main.cpp"]
        hunter.getsources = MagicMock(return_value=sources)
        hunter.required_source_files = MagicMock(side_effect=lambda s: sources)
        headers = headers or ["/src/util.h"]
        hunter.header_dependencies = MagicMock(return_value=headers)
        default_magic = magicflags_map or {}
        hunter.magicflags = MagicMock(return_value=default_magic)
        hunter.macro_state_hash = MagicMock(return_value="abcdef1234567890")
        return hunter

    def _make_namer(self):
        namer = MagicMock()
        namer.object_pathname = MagicMock(
            side_effect=lambda f, mh, dh: f"/tmp/obj/{f.split('/')[-1].replace('.cpp', '.o')}"
        )
        namer.executable_pathname = MagicMock(side_effect=lambda f: f"/tmp/bin/{f.split('/')[-1].replace('.cpp', '')}")
        namer.compute_dep_hash = MagicMock(return_value="dep_hash_12345")
        return namer

    def _make_backend(self, args=None, hunter=None):
        StubClass = _make_stub_backend_class()
        args = args or self._make_args()
        hunter = hunter or self._make_hunter()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = self._make_namer()
        return backend

    def test_single_source_produces_compile_and_link_rules(self):
        backend = self._make_backend()

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) >= 1, "Should have at least one compile rule"
        assert len(link_rules) >= 1, "Should have at least one link rule"

    def test_compile_rule_has_correct_command(self):
        backend = self._make_backend()

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        assert len(compile_rules) >= 1
        rule = compile_rules[0]
        # Command should contain the compiler and -c flag
        assert any("-c" in arg for arg in rule.command)
        assert rule.inputs[0] == "/src/main.cpp"  # Source is first input

    def test_link_rule_references_object_outputs(self):
        backend = self._make_backend()

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        # The link rule's inputs should include the compile rule's output
        object_outputs = {r.output for r in compile_rules}
        link_inputs = set(link_rules[0].inputs)
        assert object_outputs & link_inputs, "Link rule should reference compiled objects"

    def test_phony_targets_created(self):
        backend = self._make_backend()

        graph = backend.build_graph()

        phony_rules = [r for r in graph.rules if r.rule_type == "phony"]
        phony_names = {r.output for r in phony_rules}
        assert "all" in phony_names
        assert "build" in phony_names

    def test_no_sources_produces_empty_graph(self):
        args = self._make_args(filename=[], tests=[], static=[], dynamic=[])
        hunter = self._make_hunter(sources=[])
        backend = self._make_backend(args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) == 0
        assert len(link_rules) == 0
