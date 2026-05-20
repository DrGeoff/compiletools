import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import compiletools.makefile_backend
import compiletools.testhelper as uth
from compiletools.apptools import tool_version
from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.makefile_backend import MakefileBackend


class TestMakefileBackendRegistered:
    def test_registered_as_make(self):
        cls = get_backend_class("make")
        assert cls is MakefileBackend

    def test_name(self):
        assert MakefileBackend.name() == "make"

    def test_build_filename(self):
        assert MakefileBackend.build_filename() == "Makefile"


class TestMakefileGenerate:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            cas_objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_generate_writes_makefile_syntax(self):
        """generate() should produce valid Makefile syntax from a BuildGraph."""
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

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".DELETE_ON_ERROR:" in content
        # Default policy is CAS-only (use_mtime=False): compile and link
        # rules both emit no normal prerequisites because their outputs
        # are content-addressable. See ``TestUseMtime`` below for the
        # exhaustive matrix.
        assert "obj/foo.o: | /tmp/obj" in content
        assert "g++ -c foo.cpp -o obj/foo.o" in content
        # Link line drops obj/foo.o as a normal prereq (lifted to order-only).
        assert "bin/foo:" in content
        assert ".PHONY: build" in content

    def test_generate_phony_no_recipe(self):
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".PHONY: all" in content
        assert "all: build" in content

    def test_generate_includes_makeflags(self):
        """Generated Makefile should disable builtin rules/variables."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "MAKEFLAGS += -rR" in content

    @patch("os.path.isfile", return_value=True)
    def test_generate_includes_shell_when_bash_exists(self, _mock_isfile):
        """Generated Makefile should set SHELL when /bin/bash exists."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "SHELL := /bin/bash" in content

    @patch("os.path.isfile", return_value=False)
    def test_generate_omits_shell_when_no_bash(self, _mock_isfile):
        """Generated Makefile should omit SHELL when /bin/bash is absent."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "SHELL" not in content


class TestMakefileRealclean:
    """The realclean recipe must be selective in obj_dir, since obj_dir
    can be a shared location used by peer sub-projects."""

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            cas_objdir="/tmp/cas-objdir",
            bindir="/tmp/proj/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _build_graph(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/cas-objdir/proj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "/tmp/cas-objdir/proj/foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="/tmp/proj/bin/foo",
                inputs=["/tmp/cas-objdir/proj/foo.o"],
                command=["g++", "-o", "/tmp/proj/bin/foo", "/tmp/cas-objdir/proj/foo.o"],
                rule_type="link",
            )
        )
        return graph

    def _generate(self, args, graph):
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue()

    def test_realclean_recipe_is_selective(self):
        args = self._make_args()
        content = self._generate(args, self._build_graph())

        # Find the realclean recipe block
        lines = content.splitlines()
        idx = next(i for i, line in enumerate(lines) if line.startswith("realclean:"))
        recipe = lines[idx + 1]

        # Must NOT do rm -rf on the shared obj_dir
        assert "rm -rf /tmp/cas-objdir" not in recipe
        # Must list this build's outputs explicitly
        assert "/tmp/cas-objdir/proj/foo.o" in recipe
        assert "/tmp/proj/bin/foo" in recipe
        # exe_dir is per-project so rm -rf on it is OK
        assert "rm -rf /tmp/proj/bin" in recipe
        # Should prune empty dirs in obj_dir afterwards
        assert "find /tmp/cas-objdir -type d -empty -delete" in recipe

    def test_realclean_with_no_outputs_only_rm_rf_exe_dir(self):
        """When the graph has no compile/link rules, realclean still removes
        exe_dir (per-project) but emits no rm -f / find for obj_dir."""
        args = self._make_args()
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="all",
                inputs=[],
                command=None,
                rule_type="phony",
            )
        )
        content = self._generate(args, graph)

        lines = content.splitlines()
        idx = next(i for i, line in enumerate(lines) if line.startswith("realclean:"))
        recipe = lines[idx + 1]

        assert "rm -rf /tmp/proj/bin" in recipe
        assert "rm -rf /tmp/cas-objdir" not in recipe
        assert "rm -f" not in recipe


class TestGetMakeVersion:
    def setup_method(self):
        tool_version.cache_clear()

    def teardown_method(self):
        tool_version.cache_clear()

    @patch("compiletools.apptools.subprocess.check_output")
    def test_parses_make_44(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 4.4.1\nCopyright ...\n"
        assert tool_version("make") == (4, 4)

    @patch("compiletools.apptools.subprocess.check_output")
    def test_parses_make_381(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 3.81\nCopyright ...\n"
        assert tool_version("make") == (3, 81)

    @patch("compiletools.apptools.subprocess.check_output")
    def test_parses_make_40(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 4.0\nCopyright ...\n"
        assert tool_version("make") == (4, 0)

    @patch("compiletools.apptools.subprocess.check_output")
    def test_returns_zero_on_error(self, mock_check_output):
        mock_check_output.side_effect = subprocess.CalledProcessError(1, "make")
        assert tool_version("make") == (0, 0)

    @patch("compiletools.apptools.subprocess.check_output")
    def test_returns_zero_on_file_not_found(self, mock_check_output):
        mock_check_output.side_effect = FileNotFoundError
        assert tool_version("make") == (0, 0)


class TestMakefileExecute:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            makefilename="Makefile",
            parallel=1,
            shuffle=False,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("compiletools.apptools.tool_version", return_value=(4, 4))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_output_sync_with_parallel(self, mock_check_call, _mock_ver):
        """--output-sync=target should be added when parallel > 1 and make >= 4.0."""
        args = self._make_args(parallel=4)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--output-sync=target" in cmd
        assert "-j" in cmd
        idx = cmd.index("-j")
        assert cmd[idx + 1] == "4"

    @patch("compiletools.apptools.tool_version", return_value=(4, 4))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_no_output_sync_with_single_job(self, mock_check_call, _mock_ver):
        """--output-sync should not be added when parallel = 1."""
        args = self._make_args(parallel=1)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--output-sync=target" not in cmd

    @patch("compiletools.apptools.tool_version", return_value=(3, 81))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_no_output_sync_on_old_make(self, mock_check_call, _mock_ver):
        """--output-sync should not be added on make < 4.0."""
        args = self._make_args(parallel=4)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--output-sync=target" not in cmd

    @patch("compiletools.apptools.tool_version", return_value=(4, 4))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_trace_on_verbose(self, mock_check_call, _mock_ver):
        """--trace should be added when verbose >= 4 and make >= 4.0."""
        args = self._make_args(verbose=4)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--trace" in cmd

    @patch("compiletools.apptools.tool_version", return_value=(3, 81))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_no_trace_on_old_make(self, mock_check_call, _mock_ver):
        """--trace should not be added on make < 4.0 even if verbose."""
        args = self._make_args(verbose=4)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--trace" not in cmd

    @patch("compiletools.apptools.tool_version", return_value=(4, 4))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_shuffle_when_enabled(self, mock_check_call, _mock_ver):
        """--shuffle should be added when enabled and make >= 4.4."""
        args = self._make_args(shuffle=True)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--shuffle" in cmd

    @patch("compiletools.apptools.tool_version", return_value=(4, 0))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_no_shuffle_on_old_make(self, mock_check_call, _mock_ver):
        """--shuffle should not be added on make < 4.4."""
        args = self._make_args(shuffle=True)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--shuffle" not in cmd

    @patch("compiletools.apptools.tool_version", return_value=(4, 4))
    @patch("compiletools.makefile_backend.subprocess.check_call")
    def test_no_shuffle_when_disabled(self, mock_check_call, _mock_ver):
        """--shuffle should not be added when not requested."""
        args = self._make_args(shuffle=False)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)
        backend._graph = None

        backend.execute("build")

        cmd = mock_check_call.call_args[0][0]
        assert "--shuffle" not in cmd


class TestMakefileTestRules:
    """Test that test execution rules are rendered correctly in the Makefile."""

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            cas_objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_test_recipe_with_testprefix_then_marker(self):
        """TESTPREFIX tokens precede the exe; marker touch is appended after."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/test_foo.result",
                inputs=["bin/test_foo"],
                command=["valgrind", "--leak-check=full", "bin/test_foo"],
                rule_type="test",
                success_marker="bin/test_foo.result",
            )
        )

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "valgrind --leak-check=full bin/test_foo && touch bin/test_foo.result" in content

    def test_test_recipe_quotes_exe_path_with_space(self):
        """A test exe path containing a space must be shell-quoted in the recipe.

        Without quoting, /bin/sh word-splits the path and invokes the wrong
        binary (or none at all), masking the test.
        """
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

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

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

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "&& touch 'bin/dir with space/test_foo.result'" in content

    def test_test_result_rule_appends_success_marker_touch(self):
        """A rule with success_marker renders recipe as 'cmd && touch marker'.

        Producers emit pure-argv command + success_marker; this backend
        appends the touch tail at render time so make's recipe runs through
        /bin/sh.
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
        graph.add_rule(
            BuildRule(
                output="runtests",
                inputs=["bin/test_foo.result"],
                command=None,
                rule_type="phony",
            )
        )

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "bin/test_foo.result: bin/test_foo" in content
        assert "bin/test_foo && touch bin/test_foo.result" in content
        assert "rm -f bin/test_foo.result" not in content
        assert ".PHONY: runtests" in content
        assert "runtests: bin/test_foo.result" in content

    def test_test_result_rule_in_makefile(self):
        """Test rules should produce .result file recipes in the Makefile."""
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
        graph.add_rule(
            BuildRule(
                output="runtests",
                inputs=["bin/test_foo.result"],
                command=None,
                rule_type="phony",
            )
        )

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "bin/test_foo.result: bin/test_foo" in content
        assert "bin/test_foo && touch bin/test_foo.result" in content
        assert "rm -f bin/test_foo.result" not in content
        assert ".PHONY: runtests" in content
        assert "runtests: bin/test_foo.result" in content

    def test_test_verbose_echo(self):
        """Test rules should include echo when verbose >= 1."""
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

        args = self._make_args(verbose=1)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "@echo ... bin/test_foo" in content

    def test_notparallel_when_serialise_tests(self):
        """.NOTPARALLEL should be emitted when --serialise-tests is set."""
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

        args = self._make_args(serialisetests=True)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".NOTPARALLEL: runtests" in content

    def test_no_notparallel_when_not_serialised(self):
        """.NOTPARALLEL should NOT be emitted when --serialise-tests is not set."""
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

        args = self._make_args(serialisetests=False)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".NOTPARALLEL" not in content


class TestMakefileHeaderDeterministic:
    """The Makefile header is the basis for the regen-skip optimization.

    Two ct-cake invocations with identical CLI args must produce
    byte-identical headers, otherwise _build_file_uptodate always
    returns False and Makefile generation runs every time.
    """

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            cas_objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_header_ignores_underscore_attrs(self):
        """Attrs prefixed with `_` (e.g. _parser, _context with object addresses)
        must not appear in the header signature."""
        args = self._make_args()
        # Attach the same kind of non-deterministic objects apptools attaches
        args._parser = object()
        args._context = object()

        backend = MakefileBackend(args=args, hunter=MagicMock())
        sig = backend._args_signature()
        assert "_parser" not in sig
        assert "_context" not in sig
        assert "0x" not in sig  # No memory addresses leaked

    def test_two_invocations_produce_identical_headers(self):
        """Critical regression: two backends built from independent args
        with identical CLI values must emit byte-identical headers."""
        args1 = self._make_args()
        args1._parser = object()
        args1._context = object()
        backend1 = MakefileBackend(args=args1, hunter=MagicMock())
        backend1._filesystem_type = None

        args2 = self._make_args()
        args2._parser = object()  # Distinct object => distinct repr address
        args2._context = object()
        backend2 = MakefileBackend(args=args2, hunter=MagicMock())
        backend2._filesystem_type = None

        graph = BuildGraph()
        graph.add_rule(BuildRule(output="all", inputs=[], command=None, rule_type="phony"))

        buf1 = io.StringIO()
        backend1._write_makefile(graph, buf1)
        buf2 = io.StringIO()
        backend2._write_makefile(graph, buf2)

        # Headers (first line) must match byte-for-byte
        header1 = buf1.getvalue().split("\n", 1)[0]
        header2 = buf2.getvalue().split("\n", 1)[0]
        assert header1 == header2, f"Headers differ:\n{header1}\n{header2}"


class TestWrapCompileCmdRobust:
    """Fix 5: -o placement should be located by index, not assumed at end."""

    def test_o_anywhere_in_command(self):
        """A token after `-o target` should not desync the wrap."""
        args = SimpleNamespace(
            file_locking=True,
            sleep_interval_lockdir=0.05,
            sleep_interval_cifs=0.01,
            sleep_interval_flock_fallback=0.01,
            lock_warn_interval=30,
            lock_cross_host_timeout=300,
        )
        backend = MakefileBackend(args=args, hunter=MagicMock())
        backend._filesystem_type = "nfs"
        # `-o target` is in the middle, with a trailing flag after
        cmd = ["g++", "-c", "foo.cpp", "-o", "foo.o", "-DEXTRA_AT_END"]
        result = backend._wrap_compile_cmd(cmd)
        # Wrap must reference the actual target
        assert "--target=foo.o" in result
        # The trailing flag must still be present in the wrapped command
        assert "-DEXTRA_AT_END" in result

    def test_missing_dash_o_raises(self):
        """A compile command without -o should raise loudly, not silently pass."""
        args = SimpleNamespace(file_locking=False)
        backend = MakefileBackend(args=args, hunter=MagicMock())
        backend._filesystem_type = None
        with pytest.raises(AssertionError, match="missing -o"):
            backend._wrap_compile_cmd(["g++", "-c", "foo.cpp"])


class TestMakeRuntestsIncremental:
    """Fix 2: `make runtests` must skip up-to-date tests."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_make_runtests_skips_uptodate(self, tmp_path, monkeypatch):
        """After a successful first run, the second `make runtests` must not
        re-execute the test executable. Regression for the recipe self-deleting
        its own .result output."""
        monkeypatch.chdir(tmp_path)
        source = tmp_path / "test_count.cpp"
        marker = tmp_path / "ran.count"
        source.write_text(f"""
// ct-testmarker
#include <fstream>
int main() {{
    std::ofstream f("{marker}", std::ios::app);
    f << "x";
    return 0;
}}
""")

        with uth.TempConfigContext(tempdir=str(tmp_path)) as cfg:
            with uth.ParserContext():
                compiletools.makefile_backend.main(["--config=" + cfg, "--no-file-locking", "--tests=" + str(source)])

            mfs = [f for f in os.listdir(".") if f.startswith("Makefile")]
            assert mfs, "Makefile should have been generated"

            # First run: builds + runs the test
            r1 = subprocess.run(["make", "-f", mfs[0], "runtests"], capture_output=True, text=True)
            assert r1.returncode == 0, f"first run: {r1.stdout}{r1.stderr}"
            assert marker.exists(), "test should have run at least once"
            count_after_first = len(marker.read_text())
            assert count_after_first >= 1

            # Second run: must NOT re-execute the test (mtime check should skip it)
            r2 = subprocess.run(["make", "-f", mfs[0], "runtests"], capture_output=True, text=True)
            assert r2.returncode == 0, f"second run: {r2.stdout}{r2.stderr}"
            count_after_second = len(marker.read_text())
            assert count_after_second == count_after_first, (
                f"test re-ran: count {count_after_first} -> {count_after_second}; output:\n{r2.stdout}{r2.stderr}"
            )


class TestMakeRuntestsCasContract:
    """Regression: ``make runtests`` must use content-addressed success
    markers in CAS-only mode (default), not mtime-based skip checks.

    Pre-fix the rule was ``bin/<test>.result: bin/<test>`` -- mtime-based.
    Because the published bin/<test> is a hard-link of its cas-exedir
    entry, its inode mtime is the original CAS-creation time. A round-trip
    edit (header v1 -> v2 -> v1) hits the cached v1 cas entry on the
    third build; the v2 ``.result`` left over from the second run was
    newer than that ancient inode mtime, so make would happily fire the
    test recipe AGAIN even though the same v1 bytes had already passed.
    Under the new contract the marker lives at ``<cas_path>.result`` --
    sibling to the content-addressed exe -- so the v1 marker from step 1
    correctly suppresses the redundant re-run on step 3, and a genuine
    content change (v1 -> v2) still re-runs because the v2 cas-path has
    no marker yet.
    """

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_runtests_skips_round_trip_and_runs_on_real_change(self, tmp_path, monkeypatch):
        """Round-trip a header v1 -> v2 -> v1 with a stable ``--cas-exedir``
        and assert content-addressed semantics:

        * Step 1 (build v1, run): marker length advances from 0 to L1.
        * Step 2 (change to v2, build, run): marker length advances to L2 > L1
          because the v2 exe has different bytes and no cached marker.
        * Step 3 (revert to v1, build, run): marker length stays at L2
          because the v1 exe is a cas-exedir hit and ``<cas_v1>.result``
          from step 1 is still present -- the bytes have been tested.
        """
        monkeypatch.chdir(tmp_path)
        header = tmp_path / "lib.hpp"
        source = tmp_path / "test_count.cpp"
        marker = tmp_path / "ran.count"
        cas_exedir = tmp_path / "shared-cas-exedir"

        header.write_text("#pragma once\ninline int val() { return 1; }\n")
        source.write_text(f"""
// ct-testmarker
#include <fstream>
#include "lib.hpp"
int main() {{
    std::ofstream f("{marker}", std::ios::app);
    f << "x";
    // Encode val() in the marker but always exit 0 so make's
    // ``&& touch <result>`` is reached and the success marker is
    // actually written.
    f << val();
    return 0;
}}
""")

        def _build_and_runtests():
            with uth.TempConfigContext(tempdir=str(tmp_path)) as cfg:
                with uth.ParserContext():
                    compiletools.makefile_backend.main(
                        [
                            "--config=" + cfg,
                            "--no-file-locking",
                            "--cas-exedir=" + str(cas_exedir),
                            "--tests=" + str(source),
                        ]
                    )
                mfs = [f for f in os.listdir(".") if f.startswith("Makefile")]
                assert mfs, "Makefile should have been generated"
                r = subprocess.run(["make", "-f", mfs[0], "runtests"], capture_output=True, text=True)
                return r

        # Each successful test invocation appends exactly one fixed-length
        # record to the marker file: 1 byte ``x`` + 1 byte from ``f << val()``
        # = 2 bytes. The strict equality assertions below catch both
        # under-execution (skip-when-should-run) AND over-execution
        # (run-N-times-when-should-run-once) regressions.
        RECORD_BYTES = 2

        # --- Step 1: build v1, run, capture L1 -----------------------------
        r1 = _build_and_runtests()
        assert marker.exists(), f"first run never invoked the test: {r1.stdout}{r1.stderr}"
        L1 = len(marker.read_text())
        assert L1 == RECORD_BYTES, (
            f"first run wrote {L1} bytes; expected exactly {RECORD_BYTES} "
            f"(one invocation x {RECORD_BYTES} bytes/record). output:\n{r1.stdout}{r1.stderr}"
        )

        # --- Step 2: edit header to v2, rebuild, run, capture L2 -----------
        header.write_text("#pragma once\ninline int val() { return 2; }\n")
        # Force the source mtime to advance so make rebuilds even on FS with
        # whole-second mtime granularity.
        future = source.stat().st_mtime + 2
        os.utime(source, (future, future))
        os.utime(header, (future, future))
        r2 = _build_and_runtests()
        L2 = len(marker.read_text())
        assert L2 == 2 * RECORD_BYTES, (
            f"step-2 marker length {L2}; expected {2 * RECORD_BYTES} "
            f"(L1={L1} + one v2 invocation). A short marker means the test "
            f"didn't re-run on a genuine content change; a long marker means "
            f"the test ran multiple times. output:\n{r2.stdout}{r2.stderr}"
        )

        # --- Step 3: revert header to v1 (byte-identical to step 1) --------
        # The v1 cas-exedir entry from step 1 is reused (cache hit) and its
        # sibling ``.result`` marker is still present, so the test should
        # NOT be re-run. Marker file length must stay at L2.
        header.write_text("#pragma once\ninline int val() { return 1; }\n")
        future2 = max(header.stat().st_mtime, source.stat().st_mtime) + 2
        os.utime(source, (future2, future2))
        os.utime(header, (future2, future2))
        r3 = _build_and_runtests()
        L3 = len(marker.read_text())

        assert L3 == L2, (
            f"step-3 round-trip re-ran a previously-tested cas-exedir hit: "
            f"L2={L2} -> L3={L3} (expected L3 == L2). The v1 exe's bytes "
            f"were tested in step 1 and ``<cas_v1>.result`` should still be "
            f"present, so make should treat the test as up-to-date.\n"
            f"build+make output:\n{r3.stdout}{r3.stderr}"
        )


class TestAllOutputsCurrentHeaderEdit:
    """Fix 3: editing a header must trigger a rebuild even though
    `_all_outputs_current` short-circuits before make runs.

    With content-addressable object naming the bug does not reproduce:
    a header content change updates the dep_hash in the rebuilt graph,
    which changes the object output path, so `os.path.exists(rule.output)`
    in `_all_outputs_current` returns False on the freshly-built graph and
    make is invoked. This test pins that behavior."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_header_edit_triggers_rebuild(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        header = tmp_path / "h.hpp"
        source = tmp_path / "main.cpp"
        header.write_text("#pragma once\nint val() { return 1; }\n")
        source.write_text('// ct-exemarker\n#include "h.hpp"\nint main() { return val() == 1 ? 0 : 1; }\n')

        def _build():
            with uth.TempConfigContext(tempdir=str(tmp_path)) as cfg:
                with uth.ParserContext():
                    compiletools.makefile_backend.main(["--config=" + cfg, "--no-file-locking", str(source)])
                mfs = [f for f in os.listdir(".") if f.startswith("Makefile")]
                assert mfs
                r = subprocess.run(["make", "-f", mfs[0]], capture_output=True, text=True)
                assert r.returncode == 0, f"build failed: {r.stdout}{r.stderr}"

        _build()

        # Find the .o file from first build
        objs_first = sorted(
            os.path.join(dp, fn) for dp, _, files in os.walk(tmp_path) for fn in files if fn.endswith(".o")
        )
        assert objs_first, "no .o file produced by first build"

        # Edit the header to change its content
        header.write_text("#pragma once\nint val() { return 2; }\n")

        _build()

        objs_second = sorted(
            os.path.join(dp, fn) for dp, _, files in os.walk(tmp_path) for fn in files if fn.endswith(".o")
        )
        # New .o (with new dep_hash) must exist after the second build
        new_objs = set(objs_second) - set(objs_first)
        assert new_objs, f"header edit did not produce a fresh .o (objs: {objs_second})"


class TestTimingWrapBSDDate:
    """Fix 1: BSD ``date`` doesn't support ``%N`` and prints e.g.
    ``1745247600N`` literally, which Python's ``int()`` would parse as
    ``1745247600`` (dropping the suffix and corrupting timing data).

    The wrapping shell snippet must:
      1. Prefer bash 5+'s ``$EPOCHREALTIME`` (works on macOS/BSD).
      2. Fall back to ``date +%s%N`` only if the result is purely numeric.
      3. Emit ``0`` if both fail (so the JSONL line stays well-formed).
    """

    def _make_args(self):
        return SimpleNamespace(
            verbose=0,
            file_locking=False,
            makefilename="Makefile",
        )

    def _backend_with_timer(self):
        backend = MakefileBackend(args=self._make_args(), hunter=MagicMock())
        backend._filesystem_type = None
        return backend

    def test_wrap_prefers_epochrealtime(self):
        backend = self._backend_with_timer()
        wrapped = backend._wrap_with_timing("g++ -c foo.cpp", "foo.o")
        # Must reference $EPOCHREALTIME for portability
        assert "EPOCHREALTIME" in wrapped

    def test_wrap_validates_date_output(self):
        backend = self._backend_with_timer()
        wrapped = backend._wrap_with_timing("g++ -c foo.cpp", "foo.o")
        # Must include a numeric-validation guard so that BSD date's
        # literal "1745247600N" output is rejected (parsed as 0 instead).
        assert "[!0-9]" in wrapped, "wrapped recipe lacks a numeric validation guard for date output"

    def test_bsd_date_simulation_yields_well_formed_json(self, tmp_path):
        """Simulate the wrapping shell with a fake ``date`` that returns the
        BSD-style ``1745247600N`` and no $EPOCHREALTIME, and confirm the
        resulting JSONL line is parseable JSON with numeric ns fields."""
        if not os.path.exists("/bin/bash"):
            pytest.skip("requires /bin/bash")

        backend = self._backend_with_timer()
        wrapped = backend._wrap_with_timing("true", "foo.o")
        # The Makefile recipe form has $$ for $; convert back to single $
        # because we'll feed it directly to bash.
        shell_recipe = wrapped.replace("$$", "$")
        # Strip the leading "@" Make convention (silent recipe)
        if shell_recipe.startswith("@"):
            shell_recipe = shell_recipe[1:]

        # Write a fake `date` that mimics BSD: prints "<seconds>N" for +%s%N.
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_date = fake_bin / "date"
        fake_date.write_text("#!/bin/sh\necho 1745247600N\n")
        fake_date.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        # Force fallback path: unset EPOCHREALTIME explicitly
        env.pop("EPOCHREALTIME", None)

        # Invoke a clean bash without inheriting the parent's EPOCHREALTIME.
        # Use `--norc --noprofile` to avoid user dotfiles.
        result = subprocess.run(
            ["/bin/bash", "--norc", "--noprofile", "-c", "unset EPOCHREALTIME; " + shell_recipe],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"recipe failed: {result.stderr}"

        # The JSONL line was redirected to backend._timing_log_path.
        log_path = backend._timing_log_path
        if not os.path.exists(log_path):
            pytest.skip(f"log path {log_path} not present after recipe run; cwd-dependent")
        try:
            with open(log_path) as f:
                line = f.readline().strip()
            # The line MUST be valid JSON — substring-only checks miss
            # malformed payloads (e.g. an unquoted target value), which
            # ``BuildTimer.record_rules_from_make_timing`` would silently
            # drop on ``JSONDecodeError``, leaving ``.ct-timing.json``
            # with phase rows but no per-rule entries.

            entry = json.loads(line)
            # Critical guarantee: when only BSD `date` is available and
            # $EPOCHREALTIME is unset, our wrapper validates the date
            # output and substitutes 0 (well-formed) rather than
            # ``1745247600`` (a truncated garbage int).
            assert entry["start_ns"] == 0, f"BSD date corruption leaked into log line: {line!r}"
            assert entry["end_ns"] == 0, f"BSD date corruption leaked into log line: {line!r}"
            assert entry["target"] == "foo.o", f"target field was mangled: {line!r}"
        finally:
            try:
                os.remove(log_path)
            except FileNotFoundError:
                pass


class TestTimingWrapEmitsValidJSON:
    """Regression: ``_wrap_with_timing`` must emit a JSONL line that
    actually parses as JSON.  The previous implementation embedded the
    target as a bare token (``"target":/abs/path``), producing invalid
    JSON that ``BuildTimer.record_rules_from_make_timing`` silently
    dropped — leaving ``--timing`` runs with phase rows but no per-rule
    entries.
    """

    def _make_args(self):
        return SimpleNamespace(
            verbose=0,
            file_locking=False,
            makefilename="Makefile",
        )

    def _backend_with_timer(self):
        backend = MakefileBackend(args=self._make_args(), hunter=MagicMock())
        backend._filesystem_type = None
        return backend

    @pytest.mark.parametrize(
        "target",
        [
            "foo.o",
            "/abs/path/to/widget_factory_4b501471279f.o",
            "bin/blank/test_factory",
            'path with "quote".o',
            "path with space.o",
            "path-with'apostrophe.o",
        ],
    )
    def test_wrapped_recipe_emits_valid_json(self, tmp_path, target):
        if not os.path.exists("/bin/bash"):
            pytest.skip("requires /bin/bash")

        backend = self._backend_with_timer()
        wrapped = backend._wrap_with_timing("true", target)
        # Convert Make's $$ back to a single $ so we can run via bash.
        shell_recipe = wrapped.replace("$$", "$").lstrip("@+")
        result = subprocess.run(
            ["/bin/bash", "--norc", "--noprofile", "-c", shell_recipe],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"recipe failed: {result.stderr}"

        log_path = backend._timing_log_path
        if not os.path.exists(log_path):
            pytest.skip(f"log path {log_path} not present after recipe run; cwd-dependent")
        try:
            with open(log_path) as f:
                line = f.readline().strip()
            entry = json.loads(line)
            assert entry["target"] == target, f"target round-tripped wrong: {entry['target']!r}"
            assert isinstance(entry["start_ns"], int)
            assert isinstance(entry["end_ns"], int)
        finally:
            try:
                os.remove(log_path)
            except FileNotFoundError:
                pass


class TestTimingLogPidNamespace:
    """Fix 4: parallel ``ct-cake --timing`` invocations against the same
    Makefile race on ``.ct-make-timing.jsonl``.  The path must be
    namespaced by PID + monotonic_ns so two invocations write to
    distinct files."""

    def test_log_path_includes_pid_and_ns(self):
        args = SimpleNamespace(makefilename="Makefile", file_locking=False)
        backend = MakefileBackend(args=args, hunter=MagicMock())
        path = backend._timing_log_path
        pid = str(os.getpid())
        assert pid in path, f"PID not in log path: {path}"
        # Filename should match the .ct-make-timing.<suffix>.jsonl pattern
        assert os.path.basename(path).startswith(".ct-make-timing.")
        assert path.endswith(".jsonl")

    def test_two_backends_get_distinct_paths(self):
        """Critical: two MakefileBackend instances (e.g. two concurrent
        ct-cake --timing invocations) must compute distinct log paths."""
        args1 = SimpleNamespace(makefilename="Makefile", file_locking=False)
        args2 = SimpleNamespace(makefilename="Makefile", file_locking=False)
        b1 = MakefileBackend(args=args1, hunter=MagicMock())
        b2 = MakefileBackend(args=args2, hunter=MagicMock())
        # Force monotonic_ns to advance between calls

        p1 = b1._timing_log_path
        time.sleep(0)  # let monotonic_ns tick (it's strictly monotonic anyway)
        p2 = b2._timing_log_path
        assert p1 != p2, f"Concurrent backends got same log path: {p1}"

    def test_log_path_is_stable_per_backend(self):
        """A single backend must reuse the same log path across calls so
        cleanup actually removes the right file."""
        args = SimpleNamespace(makefilename="Makefile", file_locking=False)
        backend = MakefileBackend(args=args, hunter=MagicMock())
        p1 = backend._timing_log_path
        p2 = backend._timing_log_path
        assert p1 == p2


class TestMakefileConcurrency:
    """Fix 9: concurrent `make -j` against an object CAS must produce a
    correct build (no half-written .o, no torn link). Recent commit 348c18e1
    wraps link rules with ct-lock-helper for this case."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
    def test_concurrent_make_against_same_objdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Many small sources maximise compile/link interleaving so the
        # write-while-read race surfaces reliably on a cold objdir.
        sources = []
        for i in range(8):
            src = tmp_path / f"prog_{i}.cpp"
            src.write_text(f"// ct-exemarker\nint main() {{ return {i}; }}\n")
            sources.append(str(src))

        with uth.TempConfigContext(tempdir=str(tmp_path)) as cfg:
            with uth.ParserContext():
                compiletools.makefile_backend.main(["--config=" + cfg, "--file-locking=true"] + sources)

            mfs = [f for f in os.listdir(".") if f.startswith("Makefile")]
            assert mfs

            # No warm-up: race two concurrent makes against a COLD objdir
            # so the test exercises the genuine compile/link race that the
            # atomic-compile-temp-rename fix is designed to prevent.
            # Repeat several iterations to keep the test deterministic.
            objdirs = [d for d in os.listdir(".") if d.startswith("bld")]
            for iteration in range(3):
                # Wipe build outputs between iterations
                for d in objdirs:
                    if os.path.isdir(d):

                        shutil.rmtree(d, ignore_errors=True)

                results: list[subprocess.CompletedProcess] = []
                errs: list[BaseException] = []

                def _run(_results=results, _errs=errs):
                    try:
                        r = subprocess.run(
                            ["make", "-f", mfs[0], "-j", "16"],
                            capture_output=True,
                            text=True,
                        )
                        _results.append(r)
                    except BaseException as e:
                        _errs.append(e)

                threads = [threading.Thread(target=_run) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=120)
                assert not errs, f"iter {iteration}: concurrent make raised: {errs}"
                assert len(results) == 2

                for r in results:
                    assert r.returncode == 0, (
                        f"iter {iteration}: concurrent make failed: stdout={r.stdout} stderr={r.stderr}"
                    )
                    combined = r.stdout + r.stderr
                    assert "undefined reference" not in combined, (
                        f"iter {iteration}: linker saw partial .o (undefined reference): {combined}"
                    )
                    assert "undefined symbol" not in combined, (
                        f"iter {iteration}: linker saw partial .o (undefined symbol): {combined}"
                    )

                # All executables must exist and run cleanly after this iteration.
                exes = []
                for src in sources:
                    stem = os.path.splitext(os.path.basename(src))[0]
                    for dp, _, files in os.walk(tmp_path):
                        for fn in files:
                            if fn == stem and os.access(os.path.join(dp, fn), os.X_OK):
                                exes.append(os.path.join(dp, fn))
                                break
                assert len(exes) == len(sources), f"iter {iteration}: missing executables: {exes}"
                for exe in exes:
                    rc = subprocess.run([exe], capture_output=True).returncode
                    assert rc in range(256), f"iter {iteration}: {exe} did not run cleanly"


class TestMakeRunsTestsInBuildPhase:
    """MakefileBackend.execute("build") runs test ``.result`` rules
    natively (via the ``all`` phony), so make's ``-j`` scheduler fires each
    test the moment its exe links — no separate post-build ``runtests`` sweep.
    """

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_make_runs_tests_in_build(self, tmp_path, monkeypatch):
        """After execute("build") — NOT execute("runtests") — the test's
        ``.result`` success marker exists, proving the test ran during the
        build phase."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_pass.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 0; }\n')

        backend, graph = uth.build_real_backend(MakefileBackend, tmp_path, [], tests=[test_src])
        test_rule = next(r for r in graph.rules if r.rule_type == "test")
        assert test_rule.success_marker is not None
        try:
            os.remove(test_rule.success_marker)
        except FileNotFoundError:
            pass

        makefile = tmp_path / "Makefile"
        with open(makefile, "w") as f:
            backend.generate(graph, output=f)

        backend.execute("build")

        assert os.path.exists(test_rule.success_marker), (
            "no .result marker after execute('build') — test did not run during the build phase"
        )

    @uth.requires_functional_compiler
    def test_make_test_failure_halts_build(self, tmp_path, monkeypatch):
        """A failing test must make execute("build") raise CalledProcessError,
        and the failing test's ``.result`` marker must NOT be created (the
        ``&& touch`` tail only runs on rc==0)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "unit_test.hpp").write_text("#pragma once\n")
        test_src = tmp_path / "test_fail.cpp"
        test_src.write_text('#include "unit_test.hpp"\nint main() { return 1; }\n')

        backend, graph = uth.build_real_backend(MakefileBackend, tmp_path, [], tests=[test_src])
        makefile = tmp_path / "Makefile"
        with open(makefile, "w") as f:
            backend.generate(graph, output=f)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        results = uth.find_result_markers(tmp_path)
        assert not results, f"failing test left a .result marker (touch ran despite rc!=0): {results}"

    @uth.requires_functional_compiler
    def test_make_framework_test_failure_preserves_xml(self, tmp_path, monkeypatch):
        """A failing framework-detected test writes its JUnit XML report and
        *then* exits non-zero. Because the test rule's ``output`` is the XML
        path, ``.DELETE_ON_ERROR`` would delete that just-written report --
        contradicting the spec contract that a failed test still leaves its
        report behind. The XML target must be ``.PRECIOUS`` so it survives.

        Asserts:
          - execute("build") raises CalledProcessError (test failure halts),
          - the failing test's ``.result`` marker is NOT created,
          - the JUnit XML file DOES still exist after the failed build.
        """
        monkeypatch.chdir(tmp_path)
        test_src = uth.write_failing_gtest_fixture(tmp_path)

        xml_dir = tmp_path / "junit"
        backend, graph = uth.build_real_backend(
            MakefileBackend, tmp_path, [], tests=[test_src], extra_argv=["--test-xml-dir=" + str(xml_dir)]
        )

        # The framework test rule's output must be the XML path (not the
        # .result marker) for the .PRECIOUS protection to be exercised.
        test_rules = [r for r in graph.rules if r.rule_type == "test"]
        assert len(test_rules) == 1
        xml_rule = test_rules[0]
        assert xml_rule.output != xml_rule.success_marker, (
            "framework was not detected -- test rule output is still the .result marker"
        )
        assert xml_rule.output.endswith(".xml")
        xml_path = xml_rule.output

        makefile = tmp_path / "Makefile"
        with open(makefile, "w") as f:
            backend.generate(graph, output=f)
        # The makefile must mark the XML target .PRECIOUS.
        assert f".PRECIOUS: {xml_path}" in makefile.read_text() or any(
            xml_path in line for line in makefile.read_text().splitlines() if line.startswith(".PRECIOUS:")
        ), "framework-test XML output is not .PRECIOUS in the generated makefile"

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        results = uth.find_result_markers(tmp_path)
        assert not results, f"failing test left a .result marker (touch ran despite rc!=0): {results}"
        assert os.path.exists(xml_path), (
            f"JUnit XML at {xml_path} was deleted by .DELETE_ON_ERROR -- "
            f".PRECIOUS did not protect it. tmp_path contents: "
            f"{[os.path.join(dp, fn) for dp, _, files in os.walk(tmp_path) for fn in files]}"
        )
        with open(xml_path) as xf:
            assert "<testsuites>" in xf.read()

    @uth.requires_functional_compiler
    def test_make_framework_test_failure_reruns_when_only_failed_xml_exists(self, tmp_path, monkeypatch):
        """A preserved failed JUnit XML file is not a passing test marker.

        The framework XML target is intentionally ``.PRECIOUS`` so the report
        survives a failing test. A later build must still re-run that test when
        the XML exists but the ``.result`` success marker does not.
        """
        monkeypatch.chdir(tmp_path)
        test_src = uth.write_failing_gtest_fixture(tmp_path)

        xml_dir = tmp_path / "junit"
        backend, graph = uth.build_real_backend(
            MakefileBackend,
            tmp_path,
            [],
            tests=[test_src],
            extra_argv=["--test-xml-dir=" + str(xml_dir)],
        )

        test_rule = next(r for r in graph.rules if r.rule_type == "test")
        assert test_rule.output != test_rule.success_marker
        assert test_rule.success_marker is not None

        makefile = tmp_path / "Makefile"
        with open(makefile, "w") as f:
            backend.generate(graph, output=f)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")

        assert os.path.exists(test_rule.output)
        assert not os.path.exists(test_rule.success_marker)

        with pytest.raises(subprocess.CalledProcessError):
            backend.execute("build")


class TestMakefileTestEchoTarget:
    """The TEST-rule echo line must reference the test exe, not
    ``command[-1]`` — which is now an XML flag when --test-xml-dir is set
    (``_test_command_for`` appends framework XML argv after the exe)."""

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=1,
            cas_objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_makefile_test_echo_target_is_exe(self):
        """With a framework XML flag appended after the exe in command, the
        verbose echo line still names the exe path, not the XML flag."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/test_foo.result",
                inputs=["bin/test_foo"],
                command=["bin/test_foo", "--gtest_output=xml:/tmp/xml/test_foo.xml"],
                rule_type="test",
                success_marker="bin/test_foo.result",
            )
        )

        args = self._make_args()
        backend = MakefileBackend(args=args, hunter=MagicMock())
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Echo line names the exe ...
        assert "@echo ... bin/test_foo ;" in content
        # ... and never the XML flag.
        assert "echo ... --gtest_output" not in content

    def test_echo_target_is_exe_in_cas_order_only_mode(self):
        """In CAS-only mode the exe lives in ``order_only_deps`` (inputs is
        empty); the echo target must still resolve to the exe."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="cas/ab/test_foo_abc.result",
                inputs=[],
                command=["cas/ab/test_foo_abc", "--gtest_output=xml:/tmp/x.xml"],
                rule_type="test",
                order_only_deps=["bin/test_foo"],
                success_marker="cas/ab/test_foo_abc.result",
            )
        )

        args = self._make_args()
        backend = MakefileBackend(args=args, hunter=MagicMock())
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "@echo ... bin/test_foo ;" in content
        assert "echo ... --gtest_output" not in content


class TestUseMtime:
    """``args.use_mtime`` controls whether compile rules emit prerequisites.

    Default (False): the cached object's CAS path encodes file_h+dep_h+macro_h,
    so existence is sufficient. Make sees no normal prerequisites and runs the
    recipe iff the target is missing — defeating fresh-checkout mtime
    invalidation that re-runs byte-identical compiles.

    Opt-in (True): preserves classical mtime-driven prerequisite emission.
    """

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            cas_objdir="/tmp/obj",
            bindir="/tmp/bin",
            git_root="",
            file_locking=False,
            makefilename="Makefile",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _generate(self, args, graph):
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = MakefileBackend(args=args, hunter=hunter)
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

    def _compile_line(self, content: str) -> str:
        for line in content.splitlines():
            if line.startswith("/tmp/obj/aa/foo_aabbccdd.o:"):
                return line
        raise AssertionError(f"no compile rule line found in:\n{content}")

    def test_compile_rule_drops_prereqs_when_no_use_mtime(self):
        args = self._make_args(use_mtime=False)
        content = self._generate(args, self._compile_graph())
        line = self._compile_line(content)
        # ``<target>: | <order_only>`` — nothing between ``:`` and ``|``
        assert "/tmp/obj/aa/foo_aabbccdd.o: | /tmp/obj/aa" in line, line
        # Sources/headers MUST NOT appear as normal prereqs.
        assert "/work/foo.cpp" not in line
        assert "/work/foo.h" not in line
        assert "/work/bar.h" not in line

    def test_compile_rule_keeps_prereqs_when_use_mtime(self):
        args = self._make_args(use_mtime=True)
        content = self._generate(args, self._compile_graph())
        line = self._compile_line(content)
        assert "/work/foo.cpp" in line
        assert "/work/foo.h" in line
        assert "/work/bar.h" in line
        assert "| /tmp/obj/aa" in line

    def test_pch_dependency_lifts_to_order_only_when_no_use_mtime(self):
        """A PCH .gch file in inputs[] would still trigger a prereq-mtime
        recompile under classical make. Move it to order-only when the
        CAS-only policy is in effect, preserving build ordering without
        triggering rebuilds on PCH mtime."""
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
        args = self._make_args(use_mtime=False)
        content = self._generate(args, graph)
        line = self._compile_line(content)
        # PCH must still be referenced (so it's built before this rule),
        # but only on the order-only side of ``|``.
        assert "/tmp/pch/aa/std_xxx.gch" in line
        # Specifically: the PCH appears AFTER the ``|`` separator.
        target_part, _, ordering = line.partition("|")
        assert "/tmp/pch/aa/std_xxx.gch" in ordering
        assert "/tmp/pch/aa/std_xxx.gch" not in target_part
        # And the source/header path is dropped entirely.
        assert "/work/foo.cpp" not in target_part

    def test_link_rule_unchanged_when_no_use_mtime(self):
        """Only compile rules drop prereqs. Link rules legitimately depend
        on object-file mtime (relink when an .o changes)."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/bin/foo",
                inputs=["/tmp/obj/aa/foo_aabbccdd.o", "/tmp/obj/bb/bar_eeffgghh.o"],
                command=["g++", "-o", "/tmp/bin/foo", "/tmp/obj/aa/foo_aabbccdd.o", "/tmp/obj/bb/bar_eeffgghh.o"],
                rule_type="link",
            )
        )
        args = self._make_args(use_mtime=False)
        content = self._generate(args, graph)
        for line in content.splitlines():
            if line.startswith("/tmp/bin/foo:"):
                assert "/tmp/obj/aa/foo_aabbccdd.o" in line
                assert "/tmp/obj/bb/bar_eeffgghh.o" in line
                return
        raise AssertionError(f"no link rule line found in:\n{content}")
