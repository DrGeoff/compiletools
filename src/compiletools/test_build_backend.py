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

        with pytest.raises(AssertionError, match=r"artefact suffix|must be a directory"):
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

    def test_serialise_tests_chains_rules_in_source_order(self, tmp_path):
        """--serialise-tests injects the preceding test rule's output into the
        next rule's order_only_deps (source-sorted), so each backend's
        scheduler runs only one test at a time."""
        args = make_backend_args(
            tmp_path,
            filename=[],
            tests=["/src/test_foo.cpp", "/src/test_bar.cpp"],
            serialisetests=True,
        )
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp", "/src/test_bar.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 2
        all_test_outputs = {r.output for r in test_rules}

        # Exactly one rule must carry a sibling test output in order_only_deps.
        chained = [r for r in test_rules if all_test_outputs & set(r.order_only_deps)]
        assert len(chained) == 1, (
            f"exactly one rule should have a chain dep; "
            f"test rules: {[(r.output, r.order_only_deps) for r in test_rules]}"
        )
        chained_rule = chained[0]
        predecessor = next(r for r in test_rules if r.output in chained_rule.order_only_deps)
        # The chain dep must be the predecessor's output, not its success_marker:
        # for a framework test output is the JUnit XML and only output is a real
        # graph target that schedulers can resolve.
        assert predecessor.output in chained_rule.order_only_deps
        # Chain is source-sorted: "test_bar" < "test_foo", so the bar-derived
        # test must be the predecessor (runs first) and the foo-derived test
        # must be the one with the chain dep.
        assert "bar" in str(predecessor.command), (
            f"predecessor should be the test_bar rule (alphabetically first); got command {predecessor.command}"
        )
        assert "foo" in str(chained_rule.command), (
            f"chained_rule should be the test_foo rule (alphabetically second); got command {chained_rule.command}"
        )

    def test_serialise_tests_no_chain_with_single_test(self, tmp_path):
        """--serialise-tests with only one test must not add spurious deps."""
        args = make_backend_args(tmp_path, filename=[], tests=["/src/test_foo.cpp"], serialisetests=True)
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 1
        # No other test output in order_only_deps or inputs.
        rule = test_rules[0]
        assert not any(r.output in rule.order_only_deps for r in test_rules)
        assert not any(r.output in rule.inputs for r in test_rules)

    def test_serialise_tests_no_chain_when_flag_unset(self, tmp_path):
        """Without --serialise-tests, test rules must not depend on each other."""
        args = make_backend_args(
            tmp_path,
            filename=[],
            tests=["/src/test_foo.cpp", "/src/test_bar.cpp"],
            serialisetests=False,
        )
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp", "/src/test_bar.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        all_test_outputs = {r.output for r in test_rules}
        for rule in test_rules:
            assert not (all_test_outputs & set(rule.order_only_deps)), (
                f"test rule {rule.output} has a sibling test dep without --serialise-tests"
            )
            assert not (all_test_outputs & set(rule.inputs)), (
                f"test rule {rule.output} has a sibling test dep in inputs without --serialise-tests"
            )

    def test_serialise_tests_chain_uses_inputs_in_mtime_mode(self, tmp_path):
        """Under --use-mtime, the chain dep goes in ``inputs`` (not order_only_deps)
        so the previous marker's mtime can gate the next test."""
        args = make_backend_args(
            tmp_path,
            filename=[],
            tests=["/src/test_foo.cpp", "/src/test_bar.cpp"],
            serialisetests=True,
            use_mtime=True,
        )
        hunter = make_mock_hunter(sources=["/src/test_foo.cpp", "/src/test_bar.cpp"])
        backend = self._make_backend(tmp_path, args=args, hunter=hunter)

        graph = backend.build_graph()

        test_rules = graph.rules_by_type("test")
        assert len(test_rules) == 2
        all_test_outputs = {r.output for r in test_rules}
        # Under use-mtime the chain dep must be in inputs, not order_only_deps.
        chained = [r for r in test_rules if all_test_outputs & set(r.inputs)]
        assert len(chained) == 1, "exactly one rule should carry the chain in inputs"
        chained_rule = chained[0]
        assert not (all_test_outputs & set(chained_rule.order_only_deps)), (
            "chain dep must be in inputs under --use-mtime, not order_only_deps"
        )

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

    def test_source_compile_includes_pch_via_include_flag(self, tmp_path):
        """Source compile commands get ``-include <pchdir>/<hash>/<basename>``
        when pchdir is set.

        Must NOT be ``-I <pchdir>/<hash>``: GCC's quoted-include
        resolution searches the source-file dir first, so an `-I` flag
        is bypassed whenever the PCH header coexists with the consumer
        source. The absolute `-include` form opens the cached `.gch`
        unconditionally. Regression guard for the bug demonstrated in
        examples-features/pch_bypass_bug/."""
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
        inc_idx = cmd.index("-include")
        staged_h = cmd[inc_idx + 1]
        assert staged_h.startswith(pchdir + "/"), staged_h
        hash_dir, basename = staged_h.rsplit("/", 1)
        assert len(hash_dir.split("/")[-1]) == 16  # 16-char hash directory
        assert basename == "stdafx.h"
        # Negative: no PCH-specific -I leak from pre-fix behaviour.
        pch_i_indices = [
            i for i, tok in enumerate(cmd) if tok == "-I" and i + 1 < len(cmd) and cmd[i + 1].startswith(pchdir + "/")
        ]
        assert not pch_i_indices, (
            "PCH wiring regressed to `-I <pchdir>` form, which GCC bypasses "
            f"when the header coexists with the consumer source. cmd={cmd}"
        )

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

    def test_multiple_pch_headers_source_gets_multiple_include_flags(self, tmp_path):
        """Source using multiple PCH headers gets one ``-include`` per
        header, each pointing under ``<pchdir>/<hash>/<basename>``."""
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
        inc_indices = [i for i, v in enumerate(cmd) if v == "-include"]
        assert len(inc_indices) == 2, f"Expected 2 -include flags, got {len(inc_indices)}"
        staged_paths = [cmd[i + 1] for i in inc_indices]
        assert all(p.startswith(pchdir + "/") for p in staged_paths), staged_paths
        basenames = sorted(os.path.basename(p) for p in staged_paths)
        assert basenames == ["alpha.h", "beta.h"]

    def test_no_pch_include_flag_when_pchdir_unset(self, tmp_path):
        """When pchdir is None, no PCH-specific ``-include`` is injected."""
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
        assert "-include" not in _cmd(source_rules[0])


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
        backend._gcc_header_unit_resolved = {"<vector>": ["/usr/include/vector"]}
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
        backend._gcc_header_unit_resolved = {"<vector>": ["/usr/include/vector"]}
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
        backend._gcc_header_unit_resolved = {"<vector>": ["/usr/include/vector"]}
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
        backend._gcc_header_unit_resolved = {"<vector>": ["/usr/include/vector"]}
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

    @uth.requires_functional_compiler
    @uth.requires_compiler_supports_default_std
    def test_pch_rule_emission_writes_manifest(self, tmp_path):
        """Building a graph with a PCH header writes the manifest eagerly.

        Uses the real ``examples-end-to-end/pch/`` project with the full
        Hunter/headerdeps/magicflags chain — same pattern as
        ``test_backend_integration.TestBackendBuildPCH`` — so the test
        exercises the actual rule-emission path end to end.
        """
        pch_sample = uth.example_path("pch")
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
            # cas-pchdir gets /<variant> auto-appended at parse time —
            # use the post-parse value for filesystem assertions.
            pchdir = args.cas_pchdir
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
        h1 = _pch_command_hash(
            args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
        h2 = _pch_command_hash(
            args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
        assert h1 == h2

    def test_differs_for_different_flags(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="g++", CXXFLAGS="-O3")
        h1 = _pch_command_hash(
            args1, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
        h2 = _pch_command_hash(
            args2, "/src/foo.h", [], [], cxxflags_tokens=["-O3"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
        assert h1 != h2

    def test_differs_for_different_compiler(self):
        from types import SimpleNamespace

        args1 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args2 = SimpleNamespace(CXX="clang++", CXXFLAGS="-O2")
        h1 = _pch_command_hash(
            args1, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
        h2 = _pch_command_hash(
            args2, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
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
            anchor_root="",
        )
        h2 = _pch_command_hash(
            args, "/src/foo.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash=self._SCOPE_ZERO, anchor_root=""
        )
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

        hash_a = _pch_command_hash(
            args_a, "/src/stdafx.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16, anchor_root=""
        )
        hash_b = _pch_command_hash(
            args_b, "/src/stdafx.h", [], [], cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16, anchor_root=""
        )
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
        h1 = _pch_command_hash(
            args, "/src/stdafx.h", [], flags_one, cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16, anchor_root=""
        )
        h2 = _pch_command_hash(
            args, "/src/stdafx.h", [], flags_two, cxxflags_tokens=["-O2"], scope_macro_hash="0" * 16, anchor_root=""
        )
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


class TestToposortRules:
    """`_toposort_rules` orders module-interface compile rules so partitions
    precede their importers. Both halves are exercised by the production
    flow, but cycle detection only fires on malformed graphs and so needs
    a direct unit test."""

    def test_orders_dependents_after_their_inputs(self):
        from compiletools.build_backend import _toposort_rules

        rule_a = BuildRule(output="a.pcm", inputs=[], command=["c"], rule_type="compile")
        rule_b = BuildRule(output="b.pcm", inputs=["a.pcm"], command=["c"], rule_type="compile")
        rule_c = BuildRule(output="c.pcm", inputs=["b.pcm"], command=["c"], rule_type="compile")
        ordered = _toposort_rules({"c.pcm": rule_c, "b.pcm": rule_b, "a.pcm": rule_a})
        assert [r.output for r in ordered] == ["a.pcm", "b.pcm", "c.pcm"]

    def test_ignores_inputs_not_in_rules_dict(self):
        """Edges to external artefacts (source files) must not be treated
        as ordering constraints, otherwise the only-modules subset would
        block on prerequisites that aren't part of the toposort scope."""
        from compiletools.build_backend import _toposort_rules

        rule = BuildRule(output="m.pcm", inputs=["/external/src.cpp"], command=["c"], rule_type="compile")
        ordered = _toposort_rules({"m.pcm": rule})
        assert [r.output for r in ordered] == ["m.pcm"]

    def test_raises_on_cycle(self):
        from compiletools.build_backend import _toposort_rules

        rule_a = BuildRule(output="a.pcm", inputs=["b.pcm"], command=["c"], rule_type="compile")
        rule_b = BuildRule(output="b.pcm", inputs=["a.pcm"], command=["c"], rule_type="compile")
        with pytest.raises(ValueError, match="cycle detected"):
            _toposort_rules({"a.pcm": rule_a, "b.pcm": rule_b})


class TestModuleHelperUtilities:
    """Free-function helpers used by the modules path."""

    def test_module_pcm_filename_escapes_partition_separator(self):
        from compiletools.build_backend import _NAME_ESCAPE, _module_pcm_filename

        assert _module_pcm_filename("math") == "math.pcm"
        assert _module_pcm_filename("math:basic") == f"math{_NAME_ESCAPE}basic.pcm"

    def test_header_unit_arg_strips_angles_and_quotes(self):
        from compiletools.build_backend import _header_unit_arg

        assert _header_unit_arg("<vector>") == "vector"
        assert _header_unit_arg('"my.h"') == "my.h"
        # Bare token (already stripped, or some other shape) passes through.
        assert _header_unit_arg("vector") == "vector"
        assert _header_unit_arg("<>") == ""

    def test_header_unit_safe_stem_escapes_slash_and_colon(self):
        from compiletools.build_backend import _NAME_ESCAPE, _header_unit_safe_stem

        assert _header_unit_safe_stem("<vector>") == "vector"
        assert _header_unit_safe_stem("<sys/socket.h>") == f"sys{_NAME_ESCAPE}socket.h"

    def test_read_link_sig_returns_none_when_missing(self, tmp_path):
        from compiletools.build_backend import _read_link_sig

        assert _read_link_sig(str(tmp_path / "does_not_exist")) is None


class TestDetectAvailableBackends:
    """`detect_available_backends` filters a requested list to those whose
    external tool is installed; the warning print path runs when a backend
    is unavailable."""

    def test_unknown_backend_reports_unavailable(self):
        from compiletools.build_backend import backend_tool_command, is_backend_available

        assert is_backend_available("definitely_not_a_backend") is False
        assert backend_tool_command("definitely_not_a_backend") is None

    def test_self_executing_backend_reports_available(self):
        """A backend whose tool_command() returns None has no external
        dependency and so always reports available."""
        from compiletools.build_backend import (
            _REGISTRY,
            BuildBackend,
            is_backend_available,
            register_backend,
        )

        class _SelfExec(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "self_exec_backend_test"

            @staticmethod
            def build_filename():
                return "SelfExecfile"

        register_backend(_SelfExec)
        try:
            assert is_backend_available("self_exec_backend_test") is True
        finally:
            _REGISTRY.pop("self_exec_backend_test", None)

    def test_detect_filters_missing_tool_with_warning(self, capsys):
        """Inject a fake backend that claims a non-existent binary so we
        exercise the print + filter path without depending on the host's
        installed tooling."""
        from compiletools.build_backend import (
            _REGISTRY,
            BuildBackend,
            backend_tool_command,
            detect_available_backends,
            is_backend_available,
            register_backend,
        )

        class _Fake(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "fake_missing_tool"

            @staticmethod
            def build_filename():
                return "Fakefile"

            @staticmethod
            def tool_command():
                return "ct_test_tool_not_on_path_xyzzy"

        register_backend(_Fake)
        try:
            assert backend_tool_command("fake_missing_tool") == "ct_test_tool_not_on_path_xyzzy"
            assert is_backend_available("fake_missing_tool") is False
            kept = detect_available_backends(["fake_missing_tool"])
        finally:
            _REGISTRY.pop("fake_missing_tool", None)
        out = capsys.readouterr().out
        assert kept == []
        assert "Skipping backend 'fake_missing_tool'" in out
        assert "ct_test_tool_not_on_path_xyzzy" in out

    def test_backend_tool_command_returns_first_of_tuple(self):
        """When tool_command() returns a tuple of alternates, the canonical
        name is the first element."""
        from compiletools.build_backend import (
            _REGISTRY,
            BuildBackend,
            backend_tool_command,
            register_backend,
        )

        class _Alts(BuildBackend):
            def generate(self, graph, output=None):
                pass

            def execute(self, target="build"):
                pass

            def _execute_build(self, target):
                pass

            @staticmethod
            def name():
                return "alts_backend_test"

            @staticmethod
            def build_filename():
                return "Altsfile"

            @staticmethod
            def tool_command():
                return ("primary", "fallback")

        register_backend(_Alts)
        try:
            assert backend_tool_command("alts_backend_test") == "primary"
        finally:
            _REGISTRY.pop("alts_backend_test", None)


class TestResolveSystemHeaderAbsPath:
    """`_resolve_system_header_abs_path` is the single-path wrapper around
    the all-spellings probe; it returns the first canonical spelling or
    None when no probe succeeded."""

    def test_returns_none_when_compiler_missing(self):
        from compiletools.build_backend import _resolve_system_header_abs_path

        # The cache key folds in `cxx`; use a unique value so this test
        # never hits a memoised entry from a prior successful probe.
        result = _resolve_system_header_abs_path(
            "/nonexistent/compiler_xyzzy_definitely_missing",
            "<vector>",
        )
        assert result is None

    def test_returns_first_path_when_probe_succeeds(self, monkeypatch):
        """Stub _resolve_system_header_abs_paths so we can exercise the
        wrapper's first-of-list selection without touching a real compiler."""
        import compiletools.build_backend as bb

        monkeypatch.setattr(
            bb,
            "_resolve_system_header_abs_paths",
            lambda *a, **kw: ["/canonical/vector", "/noncanonical/../vector"],
        )
        assert bb._resolve_system_header_abs_path("g++", "<vector>") == "/canonical/vector"


def _make_clang_module_backend(tmp_path, *, cas_pcmdir=None, with_anchor=False):
    """Backend wired to emit clang named-module interface rules. Provides
    just enough state for ``_clang_module_pcm_destination`` /
    ``_create_clang_module_interface_rules`` to run end-to-end without a
    real compiler."""
    args = make_backend_args(
        tmp_path,
        CXX="clang++",
        cas_pcmdir=str(cas_pcmdir) if cas_pcmdir else None,
    )
    hunter = make_mock_hunter(sources=[])
    # Hunter contract used by the precompile path. Magicflags is empty so
    # the command stays predictable; deps are empty (no header walk).
    hunter.magicflags = MagicMock(return_value={})
    hunter.header_dependencies = MagicMock(return_value=[])
    StubClass = make_stub_backend_class()
    backend = StubClass(args=args, hunter=hunter)
    backend.namer = make_mock_namer(args)
    backend._module_compiler_kind = "clang"
    backend._module_pcm_dir = str(tmp_path / "flat-pcm")
    if cas_pcmdir is not None:
        backend._module_pcm_cache_root = str(cas_pcmdir)
    if with_anchor:
        backend._anchor_root = str(tmp_path)
    else:
        backend._anchor_root = ""
    return backend


class TestModulePcmDestinations:
    """`_clang_module_pcm_destination` and `_gcc_module_gcm_destination`
    drive the on-disk layout of the modules CAS. Both have a cache-off
    fallback and a cache-on branch that writes a sidecar manifest --
    coverage of the cache-on branch is what's currently missing."""

    def test_clang_cache_off_returns_flat_dir_path(self, tmp_path):
        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=None)
        pcm_path, pcm_dir = backend._clang_module_pcm_destination("/src/math.cppm", "math")
        assert pcm_dir == backend._module_pcm_dir
        assert backend._module_pcm_dir is not None
        assert pcm_path == os.path.join(backend._module_pcm_dir, "math.pcm")

    def test_clang_cache_on_returns_per_hash_dir_and_writes_manifest(self, tmp_path):
        cache_root = tmp_path / "cas-pcmdir"
        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=cache_root)
        # Source must exist on disk because the hash computation calls
        # global_hash_registry.get_file_hash; a missing path is tolerated
        # via the FileNotFoundError fallback inside _compute_pcm_command_hash.
        source = tmp_path / "math.cppm"
        source.write_text("export module math;\n")
        pcm_path, pcm_dir = backend._clang_module_pcm_destination(str(source), "math")
        # Layout: <cache_root>/<command_hash>/math.pcm
        assert pcm_path.startswith(str(cache_root) + os.sep)
        assert pcm_path.endswith(os.sep + "math.pcm")
        assert pcm_dir == os.path.dirname(pcm_path)
        # The matching manifest.json is written so trim_cache can reason
        # about reachability without needing a successful build first.
        manifest = os.path.join(pcm_dir, "manifest.json")
        assert os.path.isfile(manifest)
        with open(manifest) as f:
            data = json.loads(f.read())
        assert data["stage"] == "clang_module_interface"
        assert data["bucket_key"].endswith("math.cppm")

    def test_clang_partition_separator_escaped_in_pcm_filename(self, tmp_path):
        from compiletools.build_backend import _NAME_ESCAPE

        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=None)
        pcm_path, _ = backend._clang_module_pcm_destination("/src/m.cppm", "math:basic")
        assert os.path.basename(pcm_path) == f"math{_NAME_ESCAPE}basic.pcm"

    def test_gcc_module_gcm_destination_returns_cache_path_and_writes_manifest(self, tmp_path):
        cache_root = tmp_path / "cas-pcmdir"
        args = make_backend_args(tmp_path, CXX="g++", cas_pcmdir=str(cache_root))
        hunter = make_mock_hunter(sources=[])
        hunter.magicflags = MagicMock(return_value={})
        hunter.header_dependencies = MagicMock(return_value=[])
        StubClass = make_stub_backend_class()
        backend = StubClass(args=args, hunter=hunter)
        backend.namer = make_mock_namer(args)
        backend._module_compiler_kind = "gcc"
        backend._module_pcm_cache_root = str(cache_root)
        backend._anchor_root = ""
        source = tmp_path / "math.cppm"
        source.write_text("export module math;\n")

        gcm_path, gcm_dir = backend._gcc_module_gcm_destination(str(source), "math")
        assert gcm_path.endswith(os.sep + "math.gcm"), gcm_path
        assert gcm_path.startswith(str(cache_root) + os.sep)
        assert gcm_dir == os.path.dirname(gcm_path)
        manifest = json.loads((tmp_path / "cas-pcmdir").rglob("manifest.json").__next__().read_text())
        assert manifest["stage"] == "gcc_module_interface"

    def test_compute_pcm_command_hash_tolerates_missing_source(self, tmp_path):
        """`global_hash_registry.get_file_hash` raises FileNotFoundError on
        a path it hasn't ingested; the hash routine downgrades to empty
        source_hash so the cache key is still computable. Without this
        fallback, `_clang_module_pcm_destination` on a synthetic path
        from a test fixture would explode."""
        cache_root = tmp_path / "cas-pcmdir"
        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=cache_root)
        # /src/math.cppm does not exist on disk -- exercises the
        # FileNotFoundError/OSError except branch.
        h = backend._compute_pcm_command_hash("/src/math.cppm", stage="clang_module_interface", extra_flags=[])
        assert isinstance(h, str) and len(h) >= 8


class TestClangModuleInterfaceRules:
    """`_create_clang_module_interface_rules` emits the two-stage
    precompile (source -> .pcm) + compile (.pcm -> .o) pair clang requires
    for named-module interface units."""

    def test_emits_two_rules_with_pcm_as_obj_input(self, tmp_path):
        cache_root = tmp_path / "cas-pcmdir"
        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=cache_root)
        source = tmp_path / "math.cppm"
        source.write_text("export module math;\n")
        # The pre-pass populates _module_iface_pcm before this call in
        # production; replicate that wiring.
        pcm_path, _ = backend._clang_module_pcm_destination(str(source), "math")
        assert isinstance(backend._module_iface_pcm, dict)
        backend._module_iface_pcm["math"] = pcm_path

        pcm_rule, obj_rule = backend._create_clang_module_interface_rules(str(source), "math")

        # Stage 1: precompile produces the .pcm.
        assert pcm_rule.output == pcm_path
        cmd = _cmd(pcm_rule)
        assert "--precompile" in cmd
        assert "c++-module" in cmd
        # Stage 2: compile consumes the .pcm (its only graph input) and
        # produces the standard object-cache .o path. The mock namer's
        # path-shaper does its own basename munging; check that the rule
        # uses the namer's output (i.e. doesn't shadow it with the pcm
        # path) and writes via -o.
        assert obj_rule.inputs == [pcm_path]
        obj_cmd = _cmd(obj_rule)
        assert pcm_path in obj_cmd
        assert obj_cmd[obj_cmd.index(pcm_path) - 1] == "-c"
        # `-o <obj_rule.output>` -- the rule's named output is what the
        # compiler writes to, distinct from the .pcm being consumed.
        assert obj_cmd[-1] == obj_rule.output
        assert obj_cmd[-2] == "-o"
        assert obj_rule.output != pcm_path

    def test_relativises_source_under_anchor_root(self, tmp_path):
        """The precompile must reference the source by anchor-relative
        path (and set BuildRule.cwd = anchor_root) so the .pcm doesn't
        bake per-user absolute paths into its internal path table. This
        is what makes the downstream .o byte-identical across workspaces
        sharing cas-pcmdir."""
        cache_root = tmp_path / "cas-pcmdir"
        backend = _make_clang_module_backend(tmp_path, cas_pcmdir=cache_root, with_anchor=True)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        source = src_dir / "math.cppm"
        source.write_text("export module math;\n")
        pcm_path, _ = backend._clang_module_pcm_destination(str(source), "math")
        assert isinstance(backend._module_iface_pcm, dict)
        backend._module_iface_pcm["math"] = pcm_path

        pcm_rule, _obj_rule = backend._create_clang_module_interface_rules(str(source), "math")

        assert pcm_rule.cwd == str(tmp_path)
        # The source argument should appear as a relative path, not absolute.
        rel = os.path.relpath(str(source), str(tmp_path))
        assert rel in _cmd(pcm_rule)
        assert str(source) not in _cmd(pcm_rule)
