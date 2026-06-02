import io
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import compiletools.testhelper as uth
from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.build_timer import BuildTimer
from compiletools.ninja_backend import NinjaBackend


def _default_ninja_args(**overrides):
    """Standard SimpleNamespace `args` for NinjaBackend tests. The body of
    this helper was duplicated 4-fold (3 verbatim with cosmetic field
    ordering, 1 near-duplicate with empty CFLAGS/CXXFLAGS) across
    TestNinjaGenerate, TestNinjaFileLocking, TestUseMtime, and
    TestModuleIfaceCompileRule before extraction."""
    defaults = dict(
        verbose=0,
        cas_objdir="/tmp/obj",
        bindir="/tmp/bin",
        git_root="",
        file_locking=False,
        filename=[],
        tests=[],
        static=[],
        dynamic=[],
        CC="gcc",
        CXX="g++",
        CFLAGS="-O2",
        CXXFLAGS="-O2",
        LD="g++",
        LDFLAGS="",
        serialisetests=False,
        build_only_changed=None,
        use_mtime=False,
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.1,
        sleep_interval_flock_fallback=0.1,
        lock_warn_interval=30,
        lock_cross_host_timeout=600,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestNinjaBackendRegistered:
    def test_registered_as_ninja(self):
        cls = get_backend_class("ninja")
        assert cls is NinjaBackend

    def test_name(self):
        assert NinjaBackend.name() == "ninja"

    def test_build_filename(self):
        assert NinjaBackend.build_filename() == "build.ninja"


class TestNinjaGenerate:
    def _generate(self, graph, args=None):
        """Run NinjaBackend.generate on *graph* with a mocked hunter; return content."""
        if args is None:
            args = _default_ninja_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue()

    def test_generate_writes_ninja_syntax(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp", "foo.h"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
                order_only_deps=["/tmp/obj"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(
            BuildRule(
                output="build",
                inputs=["bin/foo"],
                command=None,
                rule_type="phony",
            )
        )

        content = self._generate(graph)

        # Default policy is CAS-only (use_mtime=False): both compile and
        # link rules drop primary/implicit deps because their outputs
        # are content-addressable. See ``TestUseMtime`` below for the
        # exhaustive matrix.
        assert "build obj/foo.o: compile_cmd ||" in content
        # Link rule's primary input (obj/foo.o) is also lifted to order-only.
        assert "build bin/foo: link_cmd ||" in content
        # Ninja uses "build <alias>: phony <deps>" for phony targets
        assert "build build: phony bin/foo" in content
        # Order-only deps use || in Ninja
        assert "|| /tmp/obj" in content

    def test_test_recipe_quotes_exe_path_with_space(self):
        """A test exe path containing a space must be shell-quoted in cmd=."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/dir with space/test_foo.result",
                inputs=["bin/dir with space/test_foo"],
                command=["bin/dir with space/test_foo"],
                rule_type="test",
                success_marker="bin/dir with space/test_foo.result",
            )
        )

        content = self._generate(graph)

        assert "'bin/dir with space/test_foo'" in content

    def test_test_recipe_quotes_success_marker_with_space(self):
        """A success_marker path with a space must be shell-quoted in the touch tail."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/dir with space/test_foo.result",
                inputs=["bin/test_foo"],
                command=["bin/test_foo"],
                rule_type="test",
                success_marker="bin/dir with space/test_foo.result",
            )
        )

        content = self._generate(graph)

        assert "&& touch 'bin/dir with space/test_foo.result'" in content

    def test_test_rule_appends_success_marker_touch(self):
        """A rule with success_marker renders cmd=$cmd && touch $marker.

        Producers emit pure-argv command + success_marker; this backend
        appends the touch tail at render time so ninja's recipe runs
        through a shell.
        """
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/test_foo.result",
                inputs=["bin/test_foo"],
                command=["bin/test_foo"],
                rule_type="test",
                success_marker="bin/test_foo.result",
            )
        )

        content = self._generate(graph)

        assert "build bin/test_foo.result: test_cmd bin/test_foo" in content
        assert "cmd = bin/test_foo && touch bin/test_foo.result" in content

    def test_restat_suppressed_for_mkdir_rule(self):
        """restat=1 must NOT be emitted for mkdir rules — directory
        mtimes change every time a child is added/removed, so restat
        would let Ninja silently skip downstream rebuilds."""
        graph = BuildGraph()
        # mkdir rule with a command — this is the path that emits a
        # ninja rule definition.
        graph.add_rule(
            BuildRule(
                output="/tmp/objdir",
                inputs=[],
                command=["mkdir", "-p", "/tmp/objdir"],
                rule_type="mkdir",
            )
        )
        content = self._generate(graph)

        # Find the mkdir_cmd rule definition block and ensure no restat=1
        # appears inside it (between "rule mkdir_cmd" and the next blank
        # line / next "rule "/"build " stanza).
        lines = content.splitlines()
        in_mkdir_rule = False
        for line in lines:
            if line.startswith("rule mkdir_cmd"):
                in_mkdir_rule = True
                continue
            if in_mkdir_rule:
                if line.startswith("rule ") or line.startswith("build "):
                    break
                if not line.strip():
                    break
                assert "restat" not in line, f"restat appeared inside mkdir rule definition: {line!r}"

    def test_restat_present_for_compile_rule(self):
        """Sanity: compile rules SHOULD still emit restat=1."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )
        content = self._generate(graph)

        # Within the compile_cmd rule block, restat=1 must appear.
        lines = content.splitlines()
        in_compile_rule = False
        saw_restat = False
        for line in lines:
            if line.startswith("rule compile_cmd"):
                in_compile_rule = True
                continue
            if in_compile_rule:
                if line.startswith("rule ") or line.startswith("build "):
                    break
                if not line.strip():
                    break
                if "restat = 1" in line:
                    saw_restat = True
        assert saw_restat, "compile rule should still set restat=1"

    def test_ninja_rule_definitions(self):
        """Ninja requires rule definitions (rule compile_cmd / rule link_cmd)."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )

        content = self._generate(graph)

        # Should define a Ninja rule with command variable
        assert "rule compile_cmd" in content
        assert "command = $cmd" in content


def _compile_graph():
    """Return a BuildGraph with a single compile rule for locking tests."""
    graph = BuildGraph()
    graph.add_rule(
        BuildRule(
            output="obj/foo.o",
            inputs=["foo.cpp", "foo.h"],
            command=["g++", "-O2", "-c", "foo.cpp", "-o", "obj/foo.o"],
            rule_type="compile",
            order_only_deps=["/tmp/obj"],
        )
    )
    return graph


class TestNinjaFileLocking:
    def test_compile_not_wrapped_when_locking_disabled(self):
        """Compile commands pass through unchanged when file_locking=False."""
        args = _default_ninja_args(file_locking=False)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(_compile_graph(), output=buf)
        content = buf.getvalue()

        assert "ct-lock-helper" not in content
        assert "cmd = g++ -O2 -c foo.cpp -o obj/foo.o" in content

    def test_compile_wrapped_with_lockdir_strategy(self):
        """Compile commands are wrapped with ct-lock-helper on NFS."""
        args = _default_ninja_args(file_locking=True, sleep_interval_lockdir=0.05)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"),
        ):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "ct-lock-helper" in content
        assert "--strategy=lockdir" in content
        assert "--target=obj/foo.o" in content
        assert "CT_LOCK_SLEEP_INTERVAL=0.05" in content
        # The compile command should appear after the -- separator
        assert "-- g++ -O2 -c foo.cpp" in content

    def test_compile_wrapped_with_native_flock(self):
        """Compile commands use native flock binary on local filesystems."""
        args = _default_ninja_args(file_locking=True, sleep_interval_flock_fallback=0.03)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"),
            patch("compiletools.backend_locking._native_flock_available", return_value=True),
        ):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "flock " in content
        assert "ct-lock-helper" not in content

    def test_compile_wrapped_with_flock_fallback(self):
        """Falls back to ct-lock-helper when native flock is unavailable."""
        args = _default_ninja_args(file_locking=True, sleep_interval_flock_fallback=0.03)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"),
            patch("compiletools.backend_locking._native_flock_available", return_value=False),
        ):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "--strategy=flock" in content
        assert "CT_LOCK_SLEEP_INTERVAL_FLOCK=0.03" in content

    def test_compile_wrapped_with_cifs_strategy(self):
        """Compile commands use cifs strategy on CIFS filesystems."""
        args = _default_ninja_args(file_locking=True, sleep_interval_cifs=0.02)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="cifs"),
        ):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "--strategy=cifs" in content
        assert "CT_LOCK_SLEEP_INTERVAL_CIFS=0.02" in content

    def test_link_commands_wrapped_when_locking_enabled(self):
        """Link commands ARE wrapped with ct-lock-helper link when locking is on."""
        graph = _compile_graph()
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o"],
                rule_type="link",
            )
        )

        args = _default_ninja_args(file_locking=True, sleep_interval_lockdir=0.05)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"),
        ):
            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

        # Link command SHOULD have ct-lock-helper link
        for line in content.splitlines():
            if "cmd = " in line and "bin/foo" in line:
                assert "ct-lock-helper link" in line, f"Link not wrapped: {line}"
                assert "--strategy=lockdir" in line
                assert "--target=bin/foo" in line
                break
        else:
            pytest.fail("link command not found in output")

    def test_link_commands_not_wrapped_when_locking_disabled(self):
        """Link commands pass through unchanged when file_locking=False."""
        graph = _compile_graph()
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o"],
                rule_type="link",
            )
        )

        args = _default_ninja_args(file_locking=False)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        for line in content.splitlines():
            if "cmd = " in line and "bin/foo" in line:
                assert "ct-lock-helper" not in line
                break

    def test_static_library_wrapped_when_locking_enabled(self):
        """Static library (ar) rules wrapped with ct-lock-helper link."""
        graph = _compile_graph()
        graph.add_rule(
            BuildRule(
                output="lib/libfoo.a",
                inputs=["obj/foo.o"],
                command=["ar", "rcs", "-o", "lib/libfoo.a", "obj/foo.o"],
                rule_type="static_library",
            )
        )

        args = _default_ninja_args(file_locking=True, sleep_interval_lockdir=0.05)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=True),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"),
        ):
            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

        for line in content.splitlines():
            if "cmd = " in line and "libfoo.a" in line:
                assert "ct-lock-helper link" in line, f"ar rule not wrapped: {line}"
                break
        else:
            pytest.fail("static_library command not found in output")

    def test_ct_lock_helper_missing_exits(self):
        """generate() exits with error when ct-lock-helper is missing."""
        args = _default_ninja_args(file_locking=True)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with (
            patch("compiletools.build_backend.check_lock_helper_available", return_value=False),
            pytest.raises(RuntimeError),
        ):
            backend.generate(_compile_graph())


class TestUseMtime:
    """``args.use_mtime`` controls whether compile rules emit input deps.

    Default (False): compile rules emit ``build <out>: rule || <order_only>``
    — no primary, no implicit. The CAS object name encodes everything that
    would justify a rebuild, and ninja skips a rule whose output exists and
    has no inputs.

    Opt-in (True): preserves classical input-driven rebuild detection.
    """

    def _generate(self, args, graph):
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue()

    def _compile_graph(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/obj/aa/foo_aabbccdd.o",
                inputs=["/work/foo.cpp", "/work/foo.h", "/work/bar.h"],
                command=["g++", "-c", "/work/foo.cpp", "-o", "/tmp/obj/aa/foo_aabbccdd.o"],
                rule_type="compile",
                order_only_deps=["/tmp/obj/aa"],
            )
        )
        return graph

    def _compile_build_line(self, content: str) -> str:
        for line in content.splitlines():
            if line.startswith("build /tmp/obj/aa/foo_aabbccdd.o:"):
                return line
        raise AssertionError(f"no compile build line found in:\n{content}")

    def test_compile_rule_drops_inputs_when_no_use_mtime(self):
        args = _default_ninja_args(use_mtime=False)
        content = self._generate(args, self._compile_graph())
        line = self._compile_build_line(content)
        assert "/work/foo.cpp" not in line
        assert "/work/foo.h" not in line
        assert "/work/bar.h" not in line
        # Order-only deps still appear after ``||``
        assert "|| /tmp/obj/aa" in line

    def test_compile_rule_keeps_inputs_when_use_mtime(self):
        args = _default_ninja_args(use_mtime=True)
        content = self._generate(args, self._compile_graph())
        line = self._compile_build_line(content)
        # Primary input present
        assert "compile_cmd /work/foo.cpp" in line
        # Implicit deps (after ``|``) present
        assert "/work/foo.h" in line
        assert "/work/bar.h" in line
        assert "|| /tmp/obj/aa" in line

    def test_pch_dependency_lifts_to_order_only_when_no_use_mtime(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/obj/aa/foo_aabbccdd.o",
                inputs=["/work/foo.cpp", "/tmp/pch/aa/std_xxx.gch"],
                command=["g++", "-c", "/work/foo.cpp", "-o", "/tmp/obj/aa/foo_aabbccdd.o"],
                rule_type="compile",
                order_only_deps=["/tmp/obj/aa"],
            )
        )
        args = _default_ninja_args(use_mtime=False)
        content = self._generate(args, graph)
        line = self._compile_build_line(content)
        # PCH must still appear in the build line so ninja orders it,
        # but only on the order-only side of ``||``.
        assert "/tmp/pch/aa/std_xxx.gch" in line
        # Strip the part between ``:`` and ``||`` and check PCH not there.
        before_ordering, _, _ = line.partition("||")
        assert "/tmp/pch/aa/std_xxx.gch" not in before_ordering
        assert "/work/foo.cpp" not in before_ordering


class TestModuleIfaceCompileRule:
    """Named-module interface compile rules must NOT receive -MMD -MF.

    GCC's module-mapper protocol makes the compile look like a multi-input
    action to ninja.  With deps=gcc, ninja reports "inputs may not also have
    inputs" and stops.  The separate ``compile_module_iface_cmd`` rule
    (no depfile / no deps) is used for these rules instead.
    """

    def _generate_with_module_iface(self, module_iface_obj, module_iface_pcm=None):
        """Build a graph with one module-iface compile rule and generate ninja."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/obj/aa/math_abc123.o",
                inputs=["/work/math.cppm"],
                command=[
                    "g++",
                    "-c",
                    "/work/math.cppm",
                    "-fmodule-mapper=mapper.txt",
                    "-o",
                    "/tmp/obj/aa/math_abc123.o",
                ],
                rule_type="compile",
                order_only_deps=["/tmp/obj/aa"],
            )
        )
        graph.add_rule(
            BuildRule(
                output="/tmp/obj/aa/main_def456.o",
                inputs=["/work/main.cpp"],
                command=["g++", "-c", "/work/main.cpp", "-o", "/tmp/obj/aa/main_def456.o"],
                rule_type="compile",
                order_only_deps=["/tmp/obj/aa"],
            )
        )

        args = _default_ninja_args(CFLAGS="", CXXFLAGS="")
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)
        # Inject module-interface tracking so the backend knows which output is
        # a module-interface artefact.
        backend._module_iface_obj = module_iface_obj
        if module_iface_pcm is not None:
            backend._module_iface_pcm = module_iface_pcm

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue()

    def test_module_iface_rule_omits_mmd_mf(self):
        """compile rule for a module-interface output must not contain -MMD."""
        content = self._generate_with_module_iface(
            module_iface_obj={"math": "/tmp/obj/aa/math_abc123.o"},
        )
        # Find the cmd line for the module-interface output.
        lines = content.splitlines()
        module_cmd = None
        for i, line in enumerate(lines):
            if "math_abc123.o" in line and line.startswith("build "):
                # Next cmd = line belongs to this rule.
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip().startswith("cmd = "):
                        module_cmd = lines[j]
                        break
                break
        assert module_cmd is not None, "module-interface cmd not found"
        assert "-MMD" not in module_cmd, f"unexpected -MMD in module-iface cmd: {module_cmd}"
        assert "-MF" not in module_cmd, f"unexpected -MF in module-iface cmd: {module_cmd}"

    def test_module_iface_uses_separate_ninja_rule(self):
        """Module-interface rules use compile_module_iface_cmd (no depfile)."""
        content = self._generate_with_module_iface(
            module_iface_obj={"math": "/tmp/obj/aa/math_abc123.o"},
        )
        # The separate rule definition must appear.
        assert "rule compile_module_iface_cmd" in content
        # The build line for the module-interface output uses that rule.
        assert (
            "build /tmp/obj/aa/math_abc123.o: compile_module_iface_cmd" in content
            or "math_abc123.o: compile_module_iface_cmd" in content
        )
        # The compile_module_iface_cmd rule definition must NOT have depfile.
        in_module_rule = False
        for line in content.splitlines():
            if line == "rule compile_module_iface_cmd":
                in_module_rule = True
                continue
            if in_module_rule:
                if line == "" or (line.startswith("rule ") and "compile_module_iface_cmd" not in line):
                    break
                assert "depfile" not in line, f"depfile found in module-iface rule: {line}"
                assert "deps = gcc" not in line, f"deps=gcc found in module-iface rule: {line}"
        assert in_module_rule, "scan never entered compile_module_iface_cmd block — rule header not found"

    def test_normal_compile_rule_still_has_mmd_mf(self):
        """Ordinary (non-module-interface) compile rules still get -MMD -MF."""
        content = self._generate_with_module_iface(
            module_iface_obj={"math": "/tmp/obj/aa/math_abc123.o"},
        )
        lines = content.splitlines()
        main_cmd = None
        for i, line in enumerate(lines):
            if "main_def456.o" in line and line.startswith("build "):
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip().startswith("cmd = "):
                        main_cmd = lines[j]
                        break
                break
        assert main_cmd is not None, "normal compile cmd not found"
        assert "-MMD" in main_cmd, f"-MMD missing from normal compile cmd: {main_cmd}"
        assert "-MF" in main_cmd, f"-MF missing from normal compile cmd: {main_cmd}"

    def test_no_module_iface_rule_when_no_module_iface_outputs(self):
        """compile_module_iface_cmd is NOT emitted when no module-interface rules."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/obj/aa/foo_aabbcc.o",
                inputs=["/work/foo.cpp"],
                command=["g++", "-c", "/work/foo.cpp", "-o", "/tmp/obj/aa/foo_aabbcc.o"],
                rule_type="compile",
            )
        )
        args = _default_ninja_args(CFLAGS="", CXXFLAGS="")
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "compile_module_iface_cmd" not in content


_ninja_unavailable = shutil.which("ninja") is None


@pytest.mark.skipif(_ninja_unavailable, reason="ninja not on PATH")
class TestNinjaRunsTestsInBuildPhase:
    """NinjaBackend.execute("build") runs test rules natively (via the
    ``all`` phony), so ninja's scheduler fires each test the moment its exe
    links — no separate post-build ``runtests`` sweep.
    """

    @pytest.fixture(autouse=True)
    def _reset_parser_state(self):
        uth.reset()
        yield
        uth.reset()

    @pytest.fixture(autouse=True)
    def _chdir_to_tmp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

    @uth.requires_functional_compiler
    def test_ninja_runs_tests_in_build(self, tmp_path):
        """After execute("build") — NOT execute("runtests") — the test's
        ``.result`` success marker exists, proving the test ran during the
        build phase."""
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_pass.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 0; }\n')

        backend, graph = uth.build_real_backend(NinjaBackend, tmp_path, [], tests=[test_src])
        with open(tmp_path / "build.ninja", "w") as f:
            backend.generate(graph, output=f)

        backend.execute("build")

        assert uth.find_result_markers(tmp_path), (
            "no .result marker after execute('build') — test did not run during the build phase"
        )

    @uth.requires_functional_compiler
    def test_ninja_test_failure_halts_build(self, tmp_path):
        """A failing no-framework test must make execute("build") raise
        CalledProcessError, and the failing test's ``.result`` marker must NOT
        be created (the ``&& touch`` tail only runs on rc==0)."""
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_fail.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 1; }\n')

        backend, graph = uth.build_real_backend(NinjaBackend, tmp_path, [], tests=[test_src])
        with open(tmp_path / "build.ninja", "w") as f:
            backend.generate(graph, output=f)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        results = uth.find_result_markers(tmp_path)
        assert not results, f"failing test left a .result marker (touch ran despite rc!=0): {results}"

    @uth.requires_functional_compiler
    def test_ninja_framework_test_failure_preserves_xml(self, tmp_path):
        """A failing framework-detected test writes its JUnit XML report and
        *then* exits non-zero. Ninja — unlike make — does not delete a failed
        rule's output, so no ``.PRECIOUS`` equivalent is needed: the XML must
        survive the failed build.

        Asserts:
          - the test rule's ``output`` is the XML path (framework detected),
          - execute("build") raises CalledProcessError (test failure halts),
          - the failing test's ``.result`` marker is NOT created,
          - the JUnit XML file DOES still exist after the failed build.
        """
        test_src = uth.write_failing_gtest_fixture(tmp_path)

        xml_dir = tmp_path / "junit"
        backend, graph = uth.build_real_backend(
            NinjaBackend, tmp_path, [], tests=[test_src], extra_argv=["--test-xml-dir=" + str(xml_dir)]
        )

        # The framework test rule's output must be the XML path (not the
        # .result marker) for the no-delete-on-failure behaviour to matter.
        test_rules = [r for r in graph.rules if r.rule_type == "test"]
        assert len(test_rules) == 1
        xml_rule = test_rules[0]
        assert xml_rule.output != xml_rule.success_marker, (
            "framework was not detected -- test rule output is still the .result marker"
        )
        assert xml_rule.output.endswith(".xml")
        xml_path = xml_rule.output

        with open(tmp_path / "build.ninja", "w") as f:
            backend.generate(graph, output=f)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        results = uth.find_result_markers(tmp_path)
        assert not results, f"failing test left a .result marker (touch ran despite rc!=0): {results}"
        assert os.path.exists(xml_path), (
            f"JUnit XML at {xml_path} was deleted by ninja on rule failure -- "
            f"ninja unexpectedly deletes failed-rule outputs. tmp_path contents: "
            f"{[os.path.join(dp, fn) for dp, _, files in os.walk(tmp_path) for fn in files]}"
        )
        with open(xml_path) as xf:
            assert "<testsuites>" in xf.read()

    @uth.requires_functional_compiler
    def test_ninja_framework_test_failure_reruns_when_only_failed_xml_exists(self, tmp_path):
        """A preserved failed JUnit XML file must not satisfy ninja's up-to-date check.

        The framework XML output survives a failing test (ninja does not
        delete failed-rule outputs). A later ``ninja runtests`` must still
        re-run that test when the XML exists but the ``.result`` success
        marker does not — which means the rule must declare both files as
        outputs so ninja's existence check considers the missing stamp.
        """
        test_src = uth.write_failing_gtest_fixture(tmp_path)

        xml_dir = tmp_path / "junit"
        backend, graph = uth.build_real_backend(
            NinjaBackend,
            tmp_path,
            [],
            tests=[test_src],
            extra_argv=["--test-xml-dir=" + str(xml_dir)],
        )

        test_rule = next(r for r in graph.rules if r.rule_type == "test")
        assert test_rule.output != test_rule.success_marker
        assert test_rule.success_marker is not None

        with open(tmp_path / "build.ninja", "w") as f:
            backend.generate(graph, output=f)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        assert os.path.exists(test_rule.output)
        assert not os.path.exists(test_rule.success_marker)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")


class TestNinjaLogClassifiesTestRules:
    """record_rules_from_ninja_log consults the BuildGraph so a test
    rule's .ninja_log entry is classified ``category="test"`` instead of
    being guessed from the output's file extension (which would mis-bucket a
    framework test's ``.xml`` output or a no-framework test's ``.result``).
    """

    def test_ninja_log_classifies_test_rules(self, tmp_path):
        # Synthetic graph: one compile rule and two test rules — a
        # no-framework test (output == .result marker) and a framework test
        # (output == .xml path). Both must classify as "test".
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["src/foo.cpp"],
                command=["g++", "-c", "src/foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/test_plain.result",
                inputs=["bin/test_plain"],
                command=["bin/test_plain"],
                rule_type="test",
                success_marker="bin/test_plain.result",
            )
        )
        graph.add_rule(
            BuildRule(
                output="junit/test_gtest.xml",
                inputs=["bin/test_gtest"],
                command=["bin/test_gtest", "--gtest_output=xml:junit/test_gtest.xml"],
                rule_type="test",
                success_marker="bin/test_gtest.result",
            )
        )

        log = tmp_path / ".ninja_log"
        log.write_text(
            "# ninja log v5\n"
            "0\t1500\t0\tobj/foo.o\thash1\n"
            "1500\t1600\t0\tbin/test_plain.result\thash2\n"
            "1600\t1800\t0\tjunit/test_gtest.xml\thash3\n"
        )

        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(str(log), graph=graph)

        phase = timer._root.children[0]
        by_target = {r.target: r for r in phase.children}
        assert by_target["bin/test_plain.result"].category == "test", (
            "no-framework test output classified by extension instead of graph rule_type"
        )
        assert by_target["junit/test_gtest.xml"].category == "test", (
            "framework test .xml output classified by extension instead of graph rule_type"
        )
        # Sanity: the compile rule is still classified correctly.
        assert by_target["obj/foo.o"].category == "compile"
