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


def _cmd(rule: BuildRule) -> list[str]:
    """Return ``rule.command`` after asserting it is non-None.

    Concrete rules (compile / link / test / mkdir / ar) always carry a
    command; only phony rules have ``command=None``. The asserts in this
    module exercise concrete rules, so the runtime check is structurally
    always true — it exists to narrow ``list[str] | None`` to
    ``list[str]`` for the type checker.
    """
    assert rule.command is not None, f"rule {rule.output!r} has no command"
    return rule.command


class TestBuildBackendContract:
    """Verify the ABC contract: cannot instantiate, must implement all abstract methods."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract"):
            BuildBackend(args=MagicMock(), hunter=MagicMock())  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_generate(self):
        class Incomplete(BuildBackend):
            pass

        with pytest.raises(TypeError, match="abstract"):
            Incomplete(args=MagicMock(), hunter=MagicMock())  # type: ignore[abstract]

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


class TestPrebuildAuxArtefacts:
    """_prebuild_aux_artefacts: pre-lock fast-path, skip_if_exists kwarg,
    and the directory-only assertion on order_only_deps."""

    def _make_backend(self, tmp_path):
        return make_stub_backend_class()(args=make_backend_args(tmp_path), hunter=make_mock_hunter())

    def _attach_rule(self, backend, rule):
        graph = BuildGraph()
        graph.add_rule(rule)
        backend._graph = graph

    @staticmethod
    def _pch_rule(gch, bucket):
        from compiletools.build_graph import BuildRule, RuleType

        return BuildRule(
            output=str(gch),
            inputs=["/some/header.h"],
            command=["g++", "-x", "c++-header", "-c", "/some/header.h", "-o", str(gch)],
            rule_type=RuleType.COMPILE,
            order_only_deps=[str(bucket)],
        )

    @staticmethod
    def _header_unit_rule(pcm, bucket):
        from compiletools.build_graph import BuildRule, RuleType

        return BuildRule(
            output=str(pcm),
            inputs=["<vector>"],
            command=["clang++", "-x", "c++-system-header", "<vector>", "-o", str(pcm)],
            rule_type=RuleType.HEADER_UNIT,
            order_only_deps=[str(bucket)],
        )

    def test_pre_lock_fast_path_skips_helper_when_pch_exists(self, tmp_path):
        backend = self._make_backend(tmp_path)
        bucket = tmp_path / "pch-bucket"
        bucket.mkdir()
        gch = bucket / "header.h.gch"
        gch.write_bytes(b"PEER_PRODUCED_PCH")
        self._attach_rule(backend, self._pch_rule(gch, bucket))

        with patch("compiletools.build_backend.execute_compile_rule") as mock_compile:
            backend._prebuild_aux_artefacts()

        mock_compile.assert_not_called()
        assert gch.read_bytes() == b"PEER_PRODUCED_PCH"

    def test_invokes_helper_with_skip_if_exists_true(self, tmp_path):
        backend = self._make_backend(tmp_path)
        bucket = tmp_path / "pch-bucket"
        gch = bucket / "header.h.gch"
        self._attach_rule(backend, self._pch_rule(gch, bucket))

        with patch("compiletools.build_backend.execute_compile_rule") as mock_compile:
            backend._prebuild_aux_artefacts()

        mock_compile.assert_called_once()
        assert mock_compile.call_args.kwargs.get("skip_if_exists") is True

    def test_header_unit_routed_through_link_helper_with_skip(self, tmp_path):
        backend = self._make_backend(tmp_path)
        bucket = tmp_path / "pcm-bucket"
        pcm = bucket / "vector.pcm"
        self._attach_rule(backend, self._header_unit_rule(pcm, bucket))

        with (
            patch("compiletools.build_backend.execute_link_rule") as mock_link,
            patch("compiletools.build_backend.execute_compile_rule") as mock_compile,
        ):
            backend._prebuild_aux_artefacts()

        mock_compile.assert_not_called()
        mock_link.assert_called_once()
        assert mock_link.call_args.kwargs.get("skip_if_exists") is True

    def test_assertion_when_order_only_dep_is_a_file(self, tmp_path):
        backend = self._make_backend(tmp_path)
        bucket = tmp_path / "pcm-bucket"
        bucket.mkdir()
        bogus_file_dep = bucket / "leaked.pcm"
        bogus_file_dep.write_bytes(b"i am a file, not a directory")
        pcm = bucket / "vector.pcm"
        from compiletools.build_graph import BuildRule, RuleType

        rule = BuildRule(
            output=str(pcm),
            inputs=["<vector>"],
            command=["clang++", "-x", "c++-system-header", "<vector>", "-o", str(pcm)],
            rule_type=RuleType.HEADER_UNIT,
            order_only_deps=[str(bogus_file_dep)],
        )
        self._attach_rule(backend, rule)

        with pytest.raises(AssertionError, match="must be a directory"):
            backend._prebuild_aux_artefacts()

    def test_no_op_when_graph_has_no_aux_rules(self, tmp_path):
        backend = self._make_backend(tmp_path)
        from compiletools.build_graph import BuildRule, RuleType

        self._attach_rule(
            backend,
            BuildRule(
                output=str(tmp_path / "main.o"),
                inputs=[str(tmp_path / "main.cpp")],
                command=["g++", "-c", "main.cpp", "-o", "main.o"],
                rule_type=RuleType.COMPILE,
            ),
        )

        with (
            patch("compiletools.build_backend.execute_link_rule") as mock_link,
            patch("compiletools.build_backend.execute_compile_rule") as mock_compile,
        ):
            backend._prebuild_aux_artefacts()

        mock_compile.assert_not_called()
        mock_link.assert_not_called()


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


class TestUseMtimeFlagPlumbing:
    """Verify ``--use-mtime`` is honored only by Make/Ninja and warns elsewhere.

    Pre-fix, ``--use-mtime=True`` was silently ignored on cmake / bazel /
    shake / slurm — those backends use content-hash or self-managed
    change detection, so a touched-but-otherwise-unchanged source can't
    force a rebuild. CLAUDE.md claimed every backend honored the flag.
    The fix makes the silent no-op explicit: a stderr warning fires on
    instantiation when the user opts in but the backend can't deliver.
    """

    def _make_make_backend(self, tmp_path, **overrides):
        from compiletools.makefile_backend import MakefileBackend

        args = make_backend_args(tmp_path, **overrides)
        hunter = make_mock_hunter()
        return MakefileBackend(args=args, hunter=hunter)

    def _make_ninja_backend(self, tmp_path, **overrides):
        from compiletools.ninja_backend import NinjaBackend

        args = make_backend_args(tmp_path, **overrides)
        hunter = make_mock_hunter()
        return NinjaBackend(args=args, hunter=hunter)

    def _make_stub_backend(self, tmp_path, **overrides):
        StubClass = make_stub_backend_class()
        args = make_backend_args(tmp_path, **overrides)
        hunter = make_mock_hunter()
        return StubClass(args=args, hunter=hunter)

    def test_makefile_backend_honors_use_mtime(self, tmp_path):
        backend = self._make_make_backend(tmp_path)
        assert backend._honors_use_mtime() is True

    def test_ninja_backend_honors_use_mtime(self, tmp_path):
        backend = self._make_ninja_backend(tmp_path)
        assert backend._honors_use_mtime() is True

    def test_cmake_backend_does_not_honor_use_mtime(self, tmp_path):
        from compiletools.cmake_backend import CMakeBackend

        args = make_backend_args(tmp_path)
        backend = CMakeBackend(args=args, hunter=make_mock_hunter())
        assert backend._honors_use_mtime() is False

    def test_bazel_backend_does_not_honor_use_mtime(self, tmp_path):
        from compiletools.bazel_backend import BazelBackend

        args = make_backend_args(tmp_path)
        backend = BazelBackend(args=args, hunter=make_mock_hunter())
        assert backend._honors_use_mtime() is False

    def test_shake_backend_does_not_honor_use_mtime(self, tmp_path):
        from compiletools.trace_backend import ShakeBackend

        args = make_backend_args(tmp_path)
        backend = ShakeBackend(args=args, hunter=make_mock_hunter())
        assert backend._honors_use_mtime() is False

    def test_slurm_backend_does_not_honor_use_mtime(self, tmp_path):
        from compiletools.trace_backend import SlurmBackend

        args = make_backend_args(tmp_path)
        backend = SlurmBackend(args=args, hunter=make_mock_hunter())
        assert backend._honors_use_mtime() is False

    def test_warning_emitted_when_use_mtime_true_on_non_honoring_backend(self, tmp_path, capsys):
        """User opts into legacy mtime semantics on a backend that can't
        deliver them — must surface as a stderr warning, not a silent no-op."""
        self._make_stub_backend(tmp_path, use_mtime=True)
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "--use-mtime" in captured.err
        assert "stub_test" in captured.err

    def test_no_warning_when_use_mtime_default(self, tmp_path, capsys):
        """Default ``use_mtime=False`` (CAS-only) — no warning anywhere,
        regardless of backend."""
        self._make_stub_backend(tmp_path)
        captured = capsys.readouterr()
        assert "use-mtime" not in captured.err

    def test_no_warning_when_make_backend_with_use_mtime_true(self, tmp_path, capsys):
        """Make honors the flag — no warning when the user opts in."""
        self._make_make_backend(tmp_path, use_mtime=True)
        captured = capsys.readouterr()
        assert "use-mtime" not in captured.err

    def test_no_warning_when_ninja_backend_with_use_mtime_true(self, tmp_path, capsys):
        """Ninja honors the flag — no warning when the user opts in."""
        self._make_ninja_backend(tmp_path, use_mtime=True)
        captured = capsys.readouterr()
        assert "use-mtime" not in captured.err

    def test_no_warning_when_use_mtime_explicitly_false(self, tmp_path, capsys):
        """Explicitly setting ``use_mtime=False`` (the default) on a
        non-honoring backend is fine — the backend's natural behavior
        already matches CAS-only semantics for content-tracked tools."""
        self._make_stub_backend(tmp_path, use_mtime=False)
        captured = capsys.readouterr()
        assert "use-mtime" not in captured.err


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
        assert any("-c" in arg for arg in _cmd(rule))
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
        assert "mkdir" in " ".join(_cmd(objdir_rules[0])), "objdir rule should use mkdir"

    def test_no_sources_produces_empty_graph(self, tmp_path):
        args = make_backend_args(tmp_path, filename=[], tests=[], static=[], dynamic=[])
        hunter = make_mock_hunter(sources=[])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(compile_rules) == 0
        assert len(link_rules) == 0

    def test_compile_rule_order_only_dep_is_bucket_dir(self, tmp_path):
        """Each compile rule depends on its sharded bucket directory, not the
        bare ``args.cas_objdir``. Pre-sharding, every compile rule serialized on
        the single ``mkdir $objdir`` node and every concurrent
        ``rename(.tmp -> .o)`` contended on the same directory inode —
        cheap when the cache is small, increasingly expensive once entry
        counts cross the per-filesystem sweet spot or peer writers pile
        up. With per-bucket order-only deps the same contention is
        spread across 256 inodes.
        """
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp", "/src/util.cpp"],
        )
        hunter = make_mock_hunter(sources=["/src/main.cpp", "/src/util.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        objdir = str(tmp_path / "obj")
        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        assert len(compile_rules) >= 2

        for rule in compile_rules:
            assert rule.order_only_deps, (
                f"compile rule {rule.output!r} must declare its bucket dir as an "
                f"order-only dep so the sharded mkdir runs first"
            )
            bucket_dir = rule.order_only_deps[0]
            expected_bucket_dir = os.path.dirname(rule.output)
            assert bucket_dir == expected_bucket_dir, (
                f"order_only_deps[0]={bucket_dir!r} must match the rule output's "
                f"parent dir {expected_bucket_dir!r} so each compile is gated only "
                f"on its own bucket's mkdir, not the object CAS root"
            )
            assert bucket_dir != objdir, (
                f"order_only_deps[0]={bucket_dir!r} should be a sharded bucket under "
                f"{objdir!r}, not the bare objdir — sharing the bare objdir defeats "
                f"the whole point of bucket sharding"
            )
            assert bucket_dir.startswith(objdir + os.sep), (
                f"bucket dir {bucket_dir!r} must sit directly under objdir {objdir!r}"
            )

    def test_per_bucket_mkdir_rule_emitted_for_each_used_bucket(self, tmp_path):
        """One ``mkdir`` rule per *used* bucket — not one per possible
        bucket, and not a single mkdir for the bare objdir. Only-used
        keeps the cold-cache build's mkdir count proportional to source
        breadth (~50-100 buckets typical) rather than the full 256.
        """
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp", "/src/util.cpp", "/src/other.cpp"],
        )
        hunter = make_mock_hunter(sources=["/src/main.cpp", "/src/util.cpp", "/src/other.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        objdir = str(tmp_path / "obj")
        compile_rules = [r for r in graph.rules if r.rule_type == "compile"]
        used_buckets = {os.path.dirname(r.output) for r in compile_rules}
        assert used_buckets, "fixture should produce at least one compile rule"

        mkdir_rules = [r for r in graph.rules if r.rule_type == "mkdir" and r.output.startswith(objdir + os.sep)]
        emitted_bucket_dirs = {r.output for r in mkdir_rules}

        assert emitted_bucket_dirs == used_buckets, (
            f"build_graph must emit exactly one mkdir per used bucket. "
            f"Used: {sorted(used_buckets)}; emitted: {sorted(emitted_bucket_dirs)}"
        )
        for rule in mkdir_rules:
            assert "mkdir" in " ".join(_cmd(rule)), f"sharded bucket mkdir rule {rule.output!r} must invoke mkdir"

    def test_compile_command_uses_tokens_from_args(self, tmp_path):
        """build_graph() compile commands must contain the tokens from
        args.CXXFLAGS_tokens verbatim. This proves that build_backend
        consumes the pre-tokenized cache instead of re-running
        split_command_cached on the raw string at each call site.
        """
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp"],
            CXXFLAGS="-O2 -std=c++17 -DSPECIAL_TOKEN",
        )
        # TOKEN-2: callers populate this on real args; tests using
        # make_backend_args get it via the same helper.
        assert hasattr(args, "CXXFLAGS_tokens"), "make_backend_args must populate CXXFLAGS_tokens"
        backend = self._make_backend(tmp_path, args=args)

        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile" and r.output.endswith("main.o")]
        assert len(compile_rules) == 1
        cmd = _cmd(compile_rules[0])
        # Each token from CXXFLAGS_tokens must appear in the compile cmd.
        for token in args.CXXFLAGS_tokens:
            assert token in cmd, f"CXXFLAGS token {token!r} missing from compile command {cmd!r}"

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

        graph = backend.build_graph()

        assert "runtests" in graph.outputs
        runtests_rule = graph.get_rule("runtests")
        assert runtests_rule is not None
        assert runtests_rule.rule_type == "phony"
        # runtests depends on .result files. In CAS-only mode (default) the
        # .result lives next to the cas-exedir entry; assert via the test
        # rule's output rather than hard-coding the path.
        test_rules = graph.rules_by_type("test")
        assert test_rules, "no test rule emitted"
        for rule in test_rules:
            assert rule.output in runtests_rule.inputs
            assert rule.output.endswith(".result")

    def test_test_result_rules_created(self, tmp_path):
        """build_graph() should create test result rules with execution commands.

        In CAS-only mode (default), the rule output is keyed at the CAS exe
        path (``<cas-exedir>/<shard>/<name>_<linkkey>.exe.result``), the
        published exe ``<bindir>/<name>`` is an order-only dep so make
        builds it but does NOT consult its mtime, and ``inputs`` is empty
        so existence of the marker is sufficient to mark the rule
        up-to-date. The command argv still invokes the user-facing exe.
        """
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        rule = test_rules[0]
        # Pin the marker path against the publish-symlink rule's CAS input so
        # the assertion travels with the namer's contract: any rename of the
        # cas-exedir layout is caught at the namer layer, not by this regex.
        publish_rule = graph.get_rule(f"{bindir}/test_foo")
        assert publish_rule is not None and publish_rule.rule_type == "symlink", (
            f"expected publish-symlink rule for the test exe; got {publish_rule}"
        )
        cas_exe_path = publish_rule.inputs[0]
        assert rule.output == cas_exe_path + ".result", (
            f"test rule output must be sibling to the cas-exedir entry; got {rule.output}, "
            f"expected {cas_exe_path}.result"
        )
        assert rule.inputs == [], (
            f"test rule must not declare normal prereqs in CAS mode (the .result is content-keyed); got {rule.inputs}"
        )
        assert rule.order_only_deps == [f"{bindir}/test_foo"], (
            f"test rule must order on the published exe via order_only_deps; got {rule.order_only_deps}"
        )
        # The recipe must NOT self-delete its own output.
        assert "rm" not in _cmd(rule)
        assert f"{bindir}/test_foo" in _cmd(rule)

    def test_test_result_rule_command_is_pure_argv(self, tmp_path):
        """Test rule command must be argv-only — no shell metacharacters.

        Touch-on-success is modeled via success_marker, not embedded in command
        tokens. argv-executing backends (Shake, Slurm) pass command straight to
        subprocess.run and would otherwise feed '&&' / 'touch' to the test exe
        as ignored argv parameters.
        """
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        rule = test_rules[0]
        assert "&&" not in _cmd(rule)
        assert "touch" not in _cmd(rule)
        assert _cmd(rule) == [f"{bindir}/test_foo"]

    def test_test_result_rule_carries_success_marker(self, tmp_path):
        """Test rule must declare its .result file as success_marker.

        In CAS-only mode the marker is the CAS-side ``<cas_path>.result``
        (same as ``rule.output``), so the marker is content-keyed and
        survives across workspaces sharing a cas-exedir.
        """
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)
        bindir = str(tmp_path / "bin")

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        rule = test_rules[0]
        publish_rule = graph.get_rule(f"{bindir}/test_foo")
        assert publish_rule is not None and publish_rule.rule_type == "symlink"
        cas_exe_path = publish_rule.inputs[0]
        assert rule.success_marker == rule.output
        assert rule.success_marker == cas_exe_path + ".result"

    def test_test_result_rules_include_testprefix(self, tmp_path):
        """Test result rules should include TESTPREFIX when set."""
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"])
        args.TESTPREFIX = "valgrind --leak-check=full"
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        assert "valgrind" in _cmd(test_rules[0])
        assert "--leak-check=full" in _cmd(test_rules[0])

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
        assert "-x" in _cmd(gch_rule)
        assert "c++-header" in _cmd(gch_rule)
        assert "/src/stdafx.h" in _cmd(gch_rule)

        # PCH rule should include magic CPPFLAGS from the header
        assert "-DPCH_ACTIVE" in _cmd(gch_rule)

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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir)
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=None)
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir)
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
        cmd = _cmd(source_rules[0])
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir)
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir)
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir)
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
        cmd = _cmd(source_rules[0])
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
        args = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=None)
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
        assert "-I" not in _cmd(source_rules[0])


class TestCompilerWrapperSplit:
    """args.CC / args.CXX / args.LD may carry a wrapper prefix like
    ``ccache g++``. The kernel's execve() needs ['ccache', 'g++', ...],
    not the literal string 'ccache g++' as argv[0] (that fails ENOENT
    with filename='ccache g++'). Every site that builds a command list
    from these args must split via shlex/split_command_cached."""

    def _build(
        self,
        tmp_path,
        *,
        per_file_magicflags=None,
        sources=None,
        filename=None,
        tests=None,
        dynamic=None,
        CC="ccache gcc",
        CXX="ccache g++",
        LD="ccache g++",
    ):
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

        compile_rules = [r for r in graph.rules if r.rule_type == "compile" and r.output.endswith("main.o")]
        assert len(compile_rules) == 1
        cmd = _cmd(compile_rules[0])
        assert cmd[0] == "ccache", f"argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"argv[1] should be 'g++', got {cmd[1]!r}"

    def test_c_compile_command_splits_wrapper(self, tmp_path):
        backend = self._build(
            tmp_path,
            filename=["/src/main.c"],
            sources=["/src/main.c"],
        )
        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile" and "/src/main.c" in r.inputs]
        assert len(compile_rules) == 1
        cmd = _cmd(compile_rules[0])
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
        cmd = _cmd(gch_rules[0])
        assert cmd[0] == "ccache", f"PCH argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"PCH argv[1] should be 'g++', got {cmd[1]!r}"

    def test_link_command_splits_wrapper(self, tmp_path):
        backend = self._build(tmp_path)
        graph = backend.build_graph()

        link_rules = [r for r in graph.rules if r.rule_type == "link"]
        assert len(link_rules) >= 1
        cmd = _cmd(link_rules[0])
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
        cmd = _cmd(lib_rules[0])
        assert cmd[0] == "ccache", f"shared-lib argv[0] should be 'ccache', got {cmd[0]!r}"
        assert cmd[1] == "g++", f"shared-lib argv[1] should be 'g++', got {cmd[1]!r}"

    def test_plain_compiler_unchanged(self, tmp_path):
        """Without a wrapper prefix, command should still start with the bare compiler."""
        backend = self._build(tmp_path, CC="gcc", CXX="g++", LD="g++")
        graph = backend.build_graph()

        compile_rules = [r for r in graph.rules if r.rule_type == "compile" and r.output.endswith("main.o")]
        assert len(compile_rules) == 1
        assert _cmd(compile_rules[0])[0] == "g++"


class TestGccCppmExtensionRecognition:
    """gcc < 14 does NOT recognize the ``.cppm`` extension as C++ source.
    Without an explicit ``-x c++`` coercion the driver treats ``math.cppm``
    as a linker input and emits::

        g++: warning: math.cppm: linker input file unused because linking not done

    leaving no ``.o`` for the producer-side rename to land. gcc 14+ added
    native ``.cppm`` recognition, so a developer on a recent toolchain
    sees the test pass without the coercion -- masking the bug for any
    CI that still runs gcc 13 (e.g. ubuntu-24.04). The probe at
    ``test_cxx_modules.py:_probe_modules_support`` already passes ``-x c++``
    when checking gcc support; the build must do the same for the
    probe-build alignment to hold.
    """

    def _make_gcc_module_backend(self, tmp_path, source):
        """Backend wired to compile ``source`` (a ``.cppm``) as a gcc module
        interface unit. The hunter is mocked to declare that the file
        ``export module``s a name, which is what triggers the modules
        flags via ``_compiler_module_flags_for``."""
        from types import SimpleNamespace

        args = make_backend_args(tmp_path, CXX="g++")
        hunter = make_mock_hunter(sources=[source])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend._module_compiler_kind = "gcc"
        # _compiler_module_flags_for keys off this hunter call to decide
        # whether the TU touches the module graph.
        result = SimpleNamespace(
            module_exports=("math",),
            module_implements=(),
            module_imports=(),
            module_header_imports=(),
        )
        hunter._file_analysis_result = MagicMock(return_value=result)
        # No system-modules wiring needed for a user-authored .cppm.
        hunter.system_modules = MagicMock(return_value={})
        return backend

    def test_gcc_cppm_compile_passes_x_cxx_before_filename(self, tmp_path):
        backend = self._make_gcc_module_backend(tmp_path, "/src/math.cppm")
        rule = backend._create_compile_rule("/src/math.cppm")

        cmd = _cmd(rule)
        assert "/src/math.cppm" in cmd, f"compile command must reference the .cppm source; got: {cmd!r}"
        idx = cmd.index("/src/math.cppm")
        # `-x c++` must appear as two adjacent tokens immediately preceding
        # the source path. The "-x ARG" form applies to subsequent inputs
        # until overridden, so positioning right before the filename is
        # the safest scope.
        assert idx >= 2 and cmd[idx - 2] == "-x" and cmd[idx - 1] == "c++", (
            f"gcc compile of a .cppm source must pass `-x c++` immediately "
            f"before the filename so gcc < 14 (no native .cppm recognition) "
            f"treats it as C++ source instead of a linker input. "
            f"Got command: {cmd!r}"
        )


class TestModuleFlagsAreShellNaive:
    """`_compiler_module_flags_for` returns flag tokens that go straight into
    ``BuildRule.command``. Argv-executing backends (Shake / Slurm) hand that
    list to ``subprocess.Popen(shell=False)``, so any token must be the
    literal compiler argument — not pre-shell-quoted text. The makefile and
    ninja backends are responsible for shell-quoting at the rendering layer
    (so `<vector>` / `>` etc. survive the recipe), not the flag-emission
    layer."""

    def _backend_with_clang_header_unit(self, tmp_path):
        args = make_backend_args(tmp_path, CXX="clang++")
        hunter = make_mock_hunter(sources=["/src/main.cpp"])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend._module_compiler_kind = "clang"
        backend._header_unit_artefact = {"<vector>": "/cache/abc/vector.pcm"}

        # Stand in for the FileAnalyzer result. Only the module_* fields
        # are read by _compiler_module_flags_for.
        from types import SimpleNamespace

        result = SimpleNamespace(
            module_exports=(),
            module_implements=(),
            module_imports=(),
            module_header_imports=("<vector>",),
        )
        hunter._file_analysis_result = MagicMock(return_value=result)
        return backend

    def test_clang_header_unit_flag_has_no_embedded_shell_quotes(self, tmp_path):
        backend = self._backend_with_clang_header_unit(tmp_path)
        flags = backend._compiler_module_flags_for("/src/main.cpp")

        for token in flags:
            assert "'" not in token, (
                f"flag token {token!r} contains a shell-quote artifact; "
                "argv backends will pass the literal quote to clang"
            )

    def test_clang_header_unit_flag_has_unquoted_form(self, tmp_path):
        backend = self._backend_with_clang_header_unit(tmp_path)
        flags = backend._compiler_module_flags_for("/src/main.cpp")

        assert "-fmodule-file=<vector>=/cache/abc/vector.pcm" in flags


class TestMakefileBackendShellQuotesCompileTokens:
    """The makefile backend is the one responsible for shell-quoting compile
    tokens that contain shell-active characters (``<``, ``>``, spaces). With
    the unquoted-emission contract above, the recipe rendering layer must
    pick up the slack so /bin/sh doesn't redirect ``<vector>`` into a file."""

    def test_compile_recipe_quotes_header_unit_flag(self, tmp_path):
        args = make_backend_args(tmp_path)
        backend = MakefileBackend(args=args, hunter=MagicMock())
        backend._filesystem_type = None
        cmd = [
            "clang++",
            "-O2",
            "-fmodules",
            "-fmodule-file=<vector>=/cache/vec.pcm",
            "-c",
            "/src/main.cpp",
            "-o",
            "/tmp/test_obj/main.o",
        ]
        recipe = backend._wrap_compile_cmd(cmd)
        assert "'-fmodule-file=<vector>=/cache/vec.pcm'" in recipe, (
            f"recipe {recipe!r} did not shell-quote the header-unit flag"
        )


class TestGccHeaderUnitProducerSideRename:
    """gcc cache-mode header-unit precompiles must use producer-side temp+rename.
    Two concurrent ct-cake invocations would otherwise both write the .gcm at
    the same cache path; without temp+rename, an importer could mmap a
    half-written .gcm. CLAUDE.md flags this invariant as universal across
    every code path that emits compile/link commands."""

    def _make_gcc_cache_backend(self, tmp_path):
        args = make_backend_args(
            tmp_path,
            CXX="g++",
            cas_pcmdir=str(tmp_path / "cas-pcmdir"),
            makefilename=str(tmp_path / "Makefile"),
        )
        hunter = make_mock_hunter(sources=[])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend._module_compiler_kind = "gcc"
        backend._module_pcm_cache_root = str(tmp_path / "cas-pcmdir")
        backend._gcc_module_mapper_path = str(tmp_path / ".module-mapper.txt")
        return backend

    def test_gcc_cache_mode_header_unit_recipe_has_temp_then_rename(self, tmp_path):
        from compiletools.build_graph import render_shell_recipe

        backend = self._make_gcc_cache_backend(tmp_path)
        backend._gcc_header_unit_resolved = {"<vector>": "/usr/include/vector"}
        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.gcm")
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        recipe = render_shell_recipe(rule)
        assert "mv -f" in recipe, (
            f"gcc cache-mode header-unit recipe must rename a tmp into the cache "
            f"target so concurrent peer builds can't observe a half-written .gcm. "
            f"Got recipe: {recipe!r}"
        )

    def test_gcc_cache_mode_tmp_path_includes_pid(self, tmp_path):
        """The tmp path is suffixed with the build_graph process's PID so two
        concurrent peer ct-cake invocations don't share an inode under
        O_TRUNC. Each invocation's gcc writes to its own tmp; the mv into
        the shared destination converges on identical bytes (deterministic
        from the cmd_hash inputs)."""
        backend = self._make_gcc_cache_backend(tmp_path)
        backend._gcc_header_unit_resolved = {"<vector>": "/usr/include/vector"}
        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.gcm")
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        # The recipe text mentions the tmp path with a PID suffix, distinct
        # from the bare "<artefact>.compiletools.tmp" form.
        pipeline = _cmd(rule)[2]  # ["sh", "-c", "<pipeline>"]
        assert f"{artefact_path}.compiletools.tmp." in pipeline, (
            f"tmp path should appear in pipeline; got: {pipeline!r}"
        )
        # Bare (un-suffixed) form must NOT be the destination — would
        # collide with a peer ct-cake invocation.
        assert f"{artefact_path}.compiletools.tmp " not in pipeline, (
            f"tmp path must be PID-suffixed, not bare; pipeline: {pipeline!r}"
        )

    def test_gcc_cache_mode_per_rule_mapper_steers_to_tmp(self, tmp_path):
        """The precompile rule writes a per-rule mini-mapper (suffixed with
        the build's PID so concurrent peer ct-cake invocations don't share
        an inode) that points the resolved header at the matching .tmp
        path. The global mapper still names <artefact> for importers, so
        reads stay valid."""
        backend = self._make_gcc_cache_backend(tmp_path)
        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.gcm")
        backend._gcc_header_unit_resolved = {"<vector>": "/usr/include/vector"}
        backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        backend._write_gcc_module_mapper()

        # Per-rule mini-mapper: <artefact>.precompile-mapper.<pid>.txt
        cmd_dir = tmp_path / "cas-pcmdir" / "abc"
        mini_files = list(cmd_dir.glob("vector.gcm.precompile-mapper.*.txt"))
        assert len(mini_files) == 1, (
            f"expected one PID-suffixed mini-mapper in {cmd_dir}, got: {sorted(cmd_dir.iterdir())}"
        )
        mini = mini_files[0]
        # Same PID suffix on the .tmp the mapper steers gcc to.
        suffix = mini.name[len("vector.gcm.precompile-mapper.") : -len(".txt")]
        line = mini.read_text().strip()
        expected_tmp = f"{artefact_path}.compiletools.tmp.{suffix}"
        assert line == f"/usr/include/vector {expected_tmp}", (
            f"mini-mapper {line!r} should point gcc at the PID-suffixed .tmp companion"
        )

        # Global mapper still names the final artefact (importer-facing).
        global_mapper = (tmp_path / ".module-mapper.txt").read_text()
        for ln in global_mapper.splitlines():
            if ln.startswith("/usr/include/vector"):
                _, dest = ln.split(None, 1)
                assert dest == artefact_path, (
                    f"global mapper for importers should name the final {artefact_path!r}, "
                    f"got {dest!r} -- importers would read from the .tmp."
                )
                break
        else:
            assert False, f"global mapper missing /usr/include/vector entry:\n{global_mapper}"

    def test_gcc_header_unit_precompile_uses_fmodules_ts(self, tmp_path):
        """gcc accepts ``-fmodules-ts`` for C++20 modules; ``-fmodules`` is a
        clang flag that gcc < 14 rejects with::

            g++: error: unrecognized command-line option '-fmodules';
                       did you mean '-Mmodules'?

        Local gcc 14+ silently accepts both, masking the bug for developers
        on a recent toolchain. The rest of the gcc path (e.g.
        ``_compiler_module_flags_for``) already uses ``-fmodules-ts``; the
        header-unit precompile must match. This test exercises both paths
        through ``_create_header_unit_precompile_rule`` -- the cache-mode
        sh-pipeline form and the fallback argv form -- so neither can
        regress.
        """
        backend = self._make_gcc_cache_backend(tmp_path)
        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.gcm")

        # Cache-mode path with resolved abs header => recipe is `sh -c <pipeline>`.
        backend._gcc_header_unit_resolved = {"<vector>": "/usr/include/vector"}
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        pipeline = _cmd(rule)[2]  # ["sh", "-c", "<pipeline>"]
        assert "-fmodules-ts" in pipeline, (
            f"cache-mode gcc header-unit precompile must pass -fmodules-ts; got pipeline: {pipeline!r}"
        )
        assert " -fmodules " not in pipeline and not pipeline.startswith("-fmodules "), (
            f"cache-mode gcc header-unit precompile must not pass -fmodules "
            f"(clang-only flag, gcc < 14 rejects it); got pipeline: {pipeline!r}"
        )

        # Fallback path (no resolved abs header) => command is plain argv.
        backend._gcc_header_unit_resolved = {}
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        assert "-fmodules-ts" in _cmd(rule), (
            f"fallback gcc header-unit precompile must pass -fmodules-ts; got command: {_cmd(rule)!r}"
        )
        assert "-fmodules" not in _cmd(rule), (
            f"fallback gcc header-unit precompile must not pass -fmodules "
            f"(clang-only flag, gcc < 14 rejects it); got command: {_cmd(rule)!r}"
        )


class TestClangHeaderUnitStdlibSymmetry:
    """`_compute_clang_header_unit_command_hash` folds ``-stdlib=libc++`` into
    its hash inputs when the build imports std, so the actual precompile
    command MUST also carry that flag. Otherwise importers (which DO get
    ``-stdlib=libc++`` via ``_compiler_module_flags_for``) would mismatch
    the precompile's BMI on the stdlib axis -- clang's BMI verification
    would reject the import and force a slow re-precompile or a hard error."""

    def test_clang_header_unit_precompile_carries_stdlib_libcxx_when_imports_std(self, tmp_path):
        args = make_backend_args(tmp_path, CXX="clang++")
        hunter = make_mock_hunter(sources=[])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend._module_compiler_kind = "clang"
        backend._module_pcm_cache_root = str(tmp_path / "cas-pcmdir")
        backend._module_pcm_dir = str(tmp_path / "cas-objdir" / ".pcm")
        backend._build_imports_std_cached = True

        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.pcm")
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        assert "-stdlib=libc++" in _cmd(rule), (
            f"clang header-unit precompile must carry -stdlib=libc++ when build "
            f"imports std (matches the cmd_hash's extra_flags). Got command: "
            f"{_cmd(rule)!r}"
        )

    def test_clang_header_unit_precompile_skips_stdlib_libcxx_without_imports_std(self, tmp_path):
        args = make_backend_args(tmp_path, CXX="clang++")
        hunter = make_mock_hunter(sources=[])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend._module_compiler_kind = "clang"
        backend._module_pcm_cache_root = str(tmp_path / "cas-pcmdir")
        backend._module_pcm_dir = str(tmp_path / "cas-objdir" / ".pcm")
        backend._build_imports_std_cached = False

        artefact_path = str(tmp_path / "cas-pcmdir" / "abc" / "vector.pcm")
        rule = backend._create_header_unit_precompile_rule("<vector>", artefact_path)
        # When the build doesn't import std, we don't inject -stdlib=libc++.
        # (User's CXXFLAGS may still set it; this test runs without that.)
        assert "-stdlib=libc++" not in _cmd(rule), (
            f"-stdlib=libc++ should not appear when build doesn't import std "
            f"and CXXFLAGS doesn't have it. Got: {_cmd(rule)!r}"
        )


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
            anchor_root="",
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
        _write_pch_manifest(str(pchdir), cmd_hash, str(header), [], "g++", context=ctx, anchor_root="")
        # Second write replaces, never leaves .tmp behind.
        _write_pch_manifest(str(pchdir), cmd_hash, str(header), [], "clang++", context=ctx, anchor_root="")

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
            "--include",
            str(tmp_path),
            "--cas-objdir",
            str(tmp_path / "obj"),
            "--bindir",
            str(tmp_path / "bin"),
            "--cas-pchdir",
            pchdir,
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

        shared = tmp_path / "cas_pchdir"
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
    """Test _pch_command_hash() determinism and sensitivity.

    Calls pass ``cxxflags_tokens`` and ``scope_macro_hash`` explicitly
    -- the two new parameters added to capture structured CXXFLAGS
    (with -D/-U stripped) and the per-PCH-header cmdline-D scope
    digest. See test_pch_cache_scoping.py for the integration tests
    that exercise the scope-filter path end-to-end.
    """

    _SCOPE_ZERO = "0" * 16

    def test_deterministic(self):
        from types import SimpleNamespace

        args = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
        h2 = _pch_command_hash(args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
        assert h1 == h2

    def test_differs_for_different_flags(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="g++", CXXFLAGS="-O3")
        h1 = _pch_command_hash(args1, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
        h2 = _pch_command_hash(args2, "/src/foo.h", [], [], cxxflags_tokens=["-O3"], scope_macro_hash=self._SCOPE_ZERO)
        assert h1 != h2

    def test_differs_for_different_compiler(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="clang++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(args1, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
        h2 = _pch_command_hash(args2, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
        assert h1 != h2

    def test_includes_magic_flags(self):
        from types import SimpleNamespace

        import stringzilla as sz

        args = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(
            args,
            "/src/foo.h",
            [sz.Str("-DFOO")],
            [],
            cxxflags_tokens=["-O2"],
            scope_macro_hash=self._SCOPE_ZERO,
        )
        h2 = _pch_command_hash(args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO)
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

    def _make_backend_with_locking(self, tmp_path, cas_pchdir=None):
        import stringzilla as sz

        StubClass = make_stub_backend_class()
        pch_flags = {
            "/src/main.cpp": {sz.Str("PCH"): [sz.Str("/src/stdafx.h")]},
            "/src/stdafx.h": {},
        }
        args = make_backend_args(
            tmp_path,
            filename=["/src/main.cpp"],
            cas_pchdir=cas_pchdir,
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
        backend = self._make_backend_with_locking(tmp_path, cas_pchdir=pchdir)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        assert gch_rules[0].rule_type == "compile"

    def test_pch_compile_command_has_output_flag(self, tmp_path):
        """PCH compile command ends with -o target, needed for _wrap_compile_cmd()."""
        pchdir = str(tmp_path / "pch")
        backend = self._make_backend_with_locking(tmp_path, cas_pchdir=pchdir)
        graph = backend.build_graph()

        gch_rules = [r for r in graph.rules if r.output.endswith(".gch")]
        assert len(gch_rules) == 1
        cmd = _cmd(gch_rules[0])
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
            cas_pchdir=pchdir,
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

        args1 = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir, CXX="g++")
        args2 = make_backend_args(tmp_path, filename=["/src/main.cpp"], cas_pchdir=pchdir, CXX="clang++")
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

        hash_a = _pch_command_hash(args_a, "/src/stdafx.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16)
        hash_b = _pch_command_hash(args_b, "/src/stdafx.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16)
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
        h1 = _pch_command_hash(args, "/src/stdafx.h", [], flags_one, cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16)
        h2 = _pch_command_hash(args, "/src/stdafx.h", [], flags_two, cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16)
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
            cas_pchdir=str(pchdir),
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
        assert "PCH CAS" in err
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

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_calls_subprocess(self, mock_realpath, tmp_path):
        backend = self._make_backend(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args == [f"{tmp_path}/bin/test_foo"]

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_raises_on_failure(self, mock_realpath, tmp_path):
        backend = self._make_backend(tmp_path)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="test failed")):
            with pytest.raises(RuntimeError, match="Test failures"):
                backend._run_tests()

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_no_tests_is_noop(self, mock_realpath, tmp_path):
        args = make_backend_args(tmp_path, tests=[])
        backend = self._make_backend(tmp_path, args=args)
        with patch("subprocess.run") as mock_run:
            backend._run_tests()
            mock_run.assert_not_called()

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_records_per_test_timing(self, mock_realpath, tmp_path):
        """When ``--timing`` is enabled, every test invocation must add a
        per-test rule on the BuildTimer so the post-build summary breaks
        ``test_execution`` down per test exe.
        """
        from compiletools.build_timer import BuildTimer

        args = make_backend_args(tmp_path, tests=["/src/test_a.cpp", "/src/test_b.cpp"])
        backend = self._make_backend(tmp_path, args=args)
        backend.context.timer = BuildTimer(enabled=True, backend="stub")

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            backend._run_tests()

        # _run_tests is called outside any phase context (cake.py wraps
        # it in a ``test_execution`` phase, but the unit test exercises
        # _run_tests directly), so rules attach to the root.
        rules = [c for c in backend.context.timer._root.children if c.category == "test"]
        assert len(rules) == 2, (
            f"expected 2 per-test rules; got {len(rules)}: {[(r.target, r.category) for r in rules]}"
        )
        assert {r.target for r in rules} == {f"{tmp_path}/bin/test_a", f"{tmp_path}/bin/test_b"}
        assert all(r.elapsed_s >= 0 for r in rules)

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_no_timing_when_disabled(self, mock_realpath, tmp_path):
        """No BuildTimer in context (or disabled) must not raise and must
        not affect test execution.
        """
        backend = self._make_backend(tmp_path)
        # Default context has no timer; this must not raise.
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()
            mock_run.assert_called_once()

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_run_tests_runs_when_result_is_newer_than_exe(self, mock_realpath, tmp_path):
        """Regression: a republished exe (e.g. cas-exedir hit) carries the
        original CAS entry's mtime; the success marker for a prior run lives
        next to that CAS entry, not next to the published user-facing exe.
        A stale ``<user_exe>.result`` left over from a pre-fix install must
        no longer suppress re-execution.

        Under the fix, in CAS-only mode the success marker is content-keyed
        and lives at ``<cas_path>.result``. ``_run_tests`` resolves
        ``user_exe -> cas_path`` via the publish-symlink rule on the build
        graph and ignores the legacy ``<user_exe>.result`` entirely.
        """
        from compiletools.build_graph import BuildGraph, BuildRule

        backend = self._make_backend(tmp_path)

        exe_path = backend.namer.executable_pathname("/src/test_foo.cpp")
        cas_exe_path = str(tmp_path / "cas-exe" / "ab" / "test_foo_deadbeef.exe")
        os.makedirs(os.path.dirname(exe_path), exist_ok=True)
        os.makedirs(os.path.dirname(cas_exe_path), exist_ok=True)

        # Real on-disk exe with mtime in the distant past (the cached
        # CAS entry's creation time, preserved by the hard-link publish).
        with open(exe_path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        long_ago = os.path.getmtime(exe_path) - 1000.0
        os.utime(exe_path, (long_ago, long_ago))

        # Stale legacy <user_exe>.result from a pre-fix install. The fix
        # must NOT consult this path; it should look for <cas_path>.result
        # (which we deliberately do not create).
        with open(exe_path + ".result", "w") as f:
            f.write("0\n")

        # Wire up the publish-symlink rule the way ``_build_graph`` would
        # in production, so ``_result_marker_path`` resolves ``exe_path``
        # to its CAS sibling.
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=exe_path,
                inputs=[cas_exe_path],
                command=["ct-cas-publish", "--cas-path", cas_exe_path, "--user-path", exe_path],
                rule_type="symlink",
            )
        )
        backend._graph = graph

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            invocations_for_exe = [c for c in mock_run.call_args_list if c.args and exe_path in c.args[0]]
            assert invocations_for_exe, (
                f"test was skipped despite no <cas_path>.result marker existing -- "
                f"the legacy <user_exe>.result must be ignored in CAS mode. "
                f"All subprocess.run calls during _run_tests: {mock_run.call_args_list}"
            )

        # And the success-touch must land at the CAS-side path, not the
        # legacy user-facing path.
        assert os.path.exists(cas_exe_path + ".result"), "successful test run did not touch the CAS-side success marker"


class TestRunTestsXmlOutput:
    """Test ``--test-xml-dir`` integration in _run_tests().

    Verifies the four behaviours called out in the design doc:
      1. The right framework-specific XML argv is appended after exe_path.
      2. A test whose .result is current but whose XML file was deleted
         is re-run to regenerate the XML.
      3. Adding --test-xml-dir on a subsequent invocation triggers a re-run
         on tests whose .result is otherwise current.
      4. A test with no detected framework runs but produces no XML and
         does not error.
    """

    def _make_backend(self, tmp_path, headers, args=None):
        """Construct a stub backend whose hunter reports the given
        transitive header set for any test source it's asked about."""
        StubClass = make_stub_backend_class()
        args = args or make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
            test_xml_dir=str(tmp_path / "junit"),
            variant="gcc.debug",
        )
        hunter = MagicMock()
        hunter.header_dependencies = MagicMock(return_value=headers)
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        return backend

    @staticmethod
    def _test_call_argv(mock_run, exe_basename):
        """Return the subprocess.run argv that ran ``exe_basename``.

        Scans every token, not just argv[0], because a TESTPREFIX like
        ``valgrind --quiet`` pushes the exe to argv[2+]."""
        for call in mock_run.call_args_list:
            argv = call.args[0]
            if not argv:
                continue
            for token in argv:
                if isinstance(token, str) and token.endswith("/" + exe_basename):
                    return argv
        raise AssertionError(
            f"no subprocess.run call ran {exe_basename}; saw {[c.args[0] for c in mock_run.call_args_list]}"
        )

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_appends_gtest_xml_argv(self, mock_realpath, tmp_path):
        """gtest gets a single ``--gtest_output=xml:PATH`` token after
        exe_path. PATH is ``<test-xml-dir>/<variant>/<exe>.xml`` and the
        variant subdir is created lazily."""
        backend = self._make_backend(
            tmp_path,
            headers=["/usr/include/iostream", "third_party/googletest/include/gtest/gtest.h"],
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = self._test_call_argv(mock_run, "test_foo")
        expected_xml = str(tmp_path / "junit" / "gcc.debug" / "test_foo.xml")
        assert argv == [f"{tmp_path}/bin/test_foo", f"--gtest_output=xml:{expected_xml}"]
        assert (tmp_path / "junit" / "gcc.debug").is_dir(), (
            "variant subdir must be pre-created so parallel workers don't race"
        )

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_appends_doctest_xml_argv(self, mock_realpath, tmp_path):
        backend = self._make_backend(
            tmp_path,
            headers=["vendor/doctest.h"],
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = self._test_call_argv(mock_run, "test_foo")
        expected_xml = str(tmp_path / "junit" / "gcc.debug" / "test_foo.xml")
        assert argv == [
            f"{tmp_path}/bin/test_foo",
            "--reporters=junit",
            f"--out={expected_xml}",
        ]

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_appends_catch2_xml_argv_after_testprefix(self, mock_realpath, tmp_path):
        """Catch2's argv is four tokens (``--reporter junit --out PATH``).
        With TESTPREFIX set, the order must be:
        ``<prefix tokens> <exe> <framework xml argv>`` so prefixes like
        valgrind that forward trailing argv to the child get the XML
        flag onto the test, not onto themselves."""
        args = make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
            test_xml_dir=str(tmp_path / "junit"),
            variant="gcc.debug",
            TESTPREFIX="valgrind --quiet",
        )
        backend = self._make_backend(
            tmp_path,
            headers=["/usr/include/catch2/catch_all.hpp"],
            args=args,
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = self._test_call_argv(mock_run, "test_foo")
        expected_xml = str(tmp_path / "junit" / "gcc.debug" / "test_foo.xml")
        assert argv == [
            "valgrind",
            "--quiet",
            f"{tmp_path}/bin/test_foo",
            "--reporter",
            "junit",
            "--out",
            expected_xml,
        ]

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_unknown_framework_runs_test_without_xml_argv(self, mock_realpath, tmp_path, capsys):
        """A test whose transitive headers contain none of the recognised
        framework headers is run normally (no XML argv appended) and a
        warning is emitted at verbose >= 1. No XML file is produced."""
        args = make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
            test_xml_dir=str(tmp_path / "junit"),
            variant="gcc.debug",
            verbose=1,
        )
        backend = self._make_backend(
            tmp_path,
            headers=["/usr/include/string.h", "vendor/myhelper.h"],
            args=args,
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = self._test_call_argv(mock_run, "test_foo")
        assert argv == [f"{tmp_path}/bin/test_foo"], "unknown framework must not append any XML argv"
        err = capsys.readouterr().err
        assert "no known unit-test framework detected" in err
        assert "/src/test_foo.cpp" in err

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_multi_framework_match_raises(self, mock_realpath, tmp_path):
        """A test pulling in two framework headers must hard-error rather
        than silently picking one (which would lose data)."""
        backend = self._make_backend(
            tmp_path,
            headers=[
                "/inc/gtest/gtest.h",
                "/inc/doctest/doctest.h",
            ],
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            with pytest.raises(ValueError, match="multiple test frameworks"):
                backend._run_tests()

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_skips_when_result_current_and_xml_exists(self, mock_realpath, tmp_path):
        """The classical mtime skip still applies when both the .result
        marker is current AND the expected XML file is present."""
        backend = self._make_backend(
            tmp_path,
            headers=["/inc/gtest/gtest.h"],
        )
        # Create a fake exe with .result newer than it, plus the XML.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        exe = bin_dir / "test_foo"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        result = bin_dir / "test_foo.result"
        result.write_text("ok")
        # Bump result mtime above exe mtime to simulate "current"
        os.utime(result, (os.path.getmtime(exe) + 10, os.path.getmtime(exe) + 10))
        xml_dir = tmp_path / "junit" / "gcc.debug"
        xml_dir.mkdir(parents=True)
        (xml_dir / "test_foo.xml").write_text("<testsuite/>")

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            # No subprocess.run call ran the test exe.
            test_calls = [
                c
                for c in mock_run.call_args_list
                if c.args and c.args[0] and isinstance(c.args[0][0], str) and c.args[0][0].endswith("test_foo")
            ]
            assert test_calls == [], "test must be skipped when .result and XML are both current"

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_reruns_when_result_current_but_xml_missing(self, mock_realpath, tmp_path):
        """When the .result marker is current but the XML file was
        deleted (someone rm-rf'd the XML dir, or asked for a different
        DIR), the test must re-run to regenerate the XML."""
        backend = self._make_backend(
            tmp_path,
            headers=["/inc/gtest/gtest.h"],
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        exe = bin_dir / "test_foo"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        result = bin_dir / "test_foo.result"
        result.write_text("ok")
        os.utime(result, (os.path.getmtime(exe) + 10, os.path.getmtime(exe) + 10))
        # Note: deliberately do NOT create the XML file.

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = TestRunTestsXmlOutput._test_call_argv(mock_run, "test_foo")
        assert "--gtest_output=xml:" in argv[-1], f"missing XML must trigger re-run; got argv={argv}"

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_unknown_framework_skips_normally_when_result_current(self, mock_realpath, tmp_path):
        """A test with no detected framework keeps the legacy skip rule:
        if .result is current it is NOT re-run, even though no XML file
        exists (one would never be produced). Otherwise such tests would
        re-run forever."""
        backend = self._make_backend(
            tmp_path,
            headers=["/usr/include/string.h"],  # no framework
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        exe = bin_dir / "test_foo"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        result = bin_dir / "test_foo.result"
        result.write_text("ok")
        os.utime(result, (os.path.getmtime(exe) + 10, os.path.getmtime(exe) + 10))

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            test_calls = [
                c
                for c in mock_run.call_args_list
                if c.args and c.args[0] and isinstance(c.args[0][0], str) and c.args[0][0].endswith("test_foo")
            ]
            assert test_calls == [], "unknown-framework test must keep legacy skip behaviour, not loop on missing XML"

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_no_xml_dir_means_no_detection_and_no_warning(self, mock_realpath, tmp_path, capsys):
        """When --test-xml-dir is unset, behaviour must be byte-identical
        to today: no detection runs (so an unknown-framework test produces
        no warning), no XML argv is appended, no XML directory is created."""
        args = make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
            verbose=2,
            # test_xml_dir intentionally absent
        )
        backend = self._make_backend(
            tmp_path,
            headers=["/usr/include/string.h"],
            args=args,
        )

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            argv = TestRunTestsXmlOutput._test_call_argv(mock_run, "test_foo")
        assert argv == [f"{tmp_path}/bin/test_foo"]
        err = capsys.readouterr().err
        assert "no known unit-test framework" not in err

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_mixed_frameworks_in_same_build_each_get_own_argv(self, mock_realpath, tmp_path):
        """Two tests in the same build, fileA using gtest and fileB using
        Catch2, must each receive their own framework's XML argv. This
        pins the per-exe cache so a future refactor can't collapse it
        into a build-wide single-framework lookup."""
        # Different header sets per test source -- gtest vs Catch2.
        headers_per_source = {
            "/src/test_alpha.cpp": ["/usr/include/gtest/gtest.h"],
            "/src/test_beta.cpp": ["/usr/include/catch2/catch_all.hpp"],
        }

        args = make_backend_args(
            tmp_path,
            tests=list(headers_per_source.keys()),
            test_xml_dir=str(tmp_path / "junit"),
            variant="gcc.debug",
        )
        StubClass = make_stub_backend_class()
        hunter = MagicMock()
        hunter.header_dependencies = MagicMock(side_effect=lambda src: headers_per_source[src])
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend._run_tests()

            alpha_argv = TestRunTestsXmlOutput._test_call_argv(mock_run, "test_alpha")
            beta_argv = TestRunTestsXmlOutput._test_call_argv(mock_run, "test_beta")
        alpha_xml = str(tmp_path / "junit" / "gcc.debug" / "test_alpha.xml")
        beta_xml = str(tmp_path / "junit" / "gcc.debug" / "test_beta.xml")

        assert alpha_argv == [
            f"{tmp_path}/bin/test_alpha",
            f"--gtest_output=xml:{alpha_xml}",
        ], "test_alpha (gtest) must get the gtest single-token argv"

        assert beta_argv == [
            f"{tmp_path}/bin/test_beta",
            "--reporter",
            "junit",
            "--out",
            beta_xml,
        ], "test_beta (Catch2) must get Catch2's four-token argv, not gtest's"

    @patch("compiletools.wrappedos.realpath", side_effect=lambda x: x)
    def test_xml_dir_set_after_initial_run_triggers_rerun(self, mock_realpath, tmp_path):
        """End-to-end of two consecutive _run_tests invocations:
        the first WITHOUT --test-xml-dir creates the .result marker, the
        second WITH --test-xml-dir must re-run because no XML file
        exists yet."""
        # First invocation: no --test-xml-dir. Test runs, .result created.
        args1 = make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
        )
        backend1 = self._make_backend(
            tmp_path,
            headers=["/inc/gtest/gtest.h"],
            args=args1,
        )
        # Pre-create exe so the .result write later is meaningful.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        exe = bin_dir / "test_foo"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            backend1._run_tests()
        assert (bin_dir / "test_foo.result").exists()

        # Second invocation: --test-xml-dir added. Must re-run.
        args2 = make_backend_args(
            tmp_path,
            tests=["/src/test_foo.cpp"],
            test_xml_dir=str(tmp_path / "junit"),
            variant="gcc.debug",
        )
        backend2 = self._make_backend(
            tmp_path,
            headers=["/inc/gtest/gtest.h"],
            args=args2,
        )
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_run:
            backend2._run_tests()

            argv = TestRunTestsXmlOutput._test_call_argv(mock_run, "test_foo")
        assert "--gtest_output=xml:" in argv[-1], (
            f"adding --test-xml-dir must trigger re-run to populate XML; got argv={argv}"
        )


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
        assert obj_dir.exists(), "object CAS itself should be preserved"

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
        """realclean() should remove .gch files from PCH CAS."""
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
        # Pin ``use_mtime`` to False so the CAS-only short-circuit path is
        # exercised — without this the bare MagicMock returns a truthy
        # sentinel for ``args.use_mtime`` and ``_all_outputs_current``
        # bails out unconditionally (the legacy mtime-driven mode where
        # make/ninja decides currency for itself).
        args.use_mtime = False
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

    def test_returns_false_on_dangling_publish_symlink(self, tmp_path):
        """C1 regression: a publish symlink whose CAS target was trimmed
        away must force a rebuild. Documents that ``os.path.exists``
        follows symlinks and returns False on a dangling one — which is
        the correct answer here, since the publish recipe needs to
        re-run to either re-link or re-build the CAS target. Using
        ``os.path.lexists`` here would be WRONG (would treat dangling
        symlink as 'present' and skip the rebuild that repairs it).
        """
        backend = self._make_backend()
        cas_target = tmp_path / "cas" / "ab" / "main_KEY.exe"
        bin_path = tmp_path / "bin" / "main"
        bin_path.parent.mkdir(parents=True)
        bin_path.symlink_to(cas_target)  # cas_target deliberately absent
        assert bin_path.is_symlink()
        assert not bin_path.exists()  # dangling

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=str(bin_path),
                inputs=[str(cas_target)],
                command=["sh", "-c", "ln -f " + str(cas_target) + " " + str(bin_path)],
                rule_type="symlink",
            )
        )
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
        link_cmd = _cmd(link_rules[0])
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
        link_cmd = _cmd(link_rules[0])
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
        link_cmd = _cmd(link_rules[0])
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
