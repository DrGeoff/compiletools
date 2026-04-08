import io
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.makefile_backend import MakefileBackend, _get_make_version


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
            objdir="/tmp/obj",
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
        assert "obj/foo.o: foo.cpp foo.h" in content
        assert "| /tmp/obj" in content
        assert "g++ -c foo.cpp -o obj/foo.o" in content
        assert "bin/foo: obj/foo.o" in content
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


class TestGetMakeVersion:
    def setup_method(self):
        _get_make_version.cache_clear()

    def teardown_method(self):
        _get_make_version.cache_clear()

    @patch("compiletools.makefile_backend.subprocess.check_output")
    def test_parses_make_44(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 4.4.1\nCopyright ...\n"
        assert _get_make_version() == (4, 4)

    @patch("compiletools.makefile_backend.subprocess.check_output")
    def test_parses_make_381(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 3.81\nCopyright ...\n"
        assert _get_make_version() == (3, 81)

    @patch("compiletools.makefile_backend.subprocess.check_output")
    def test_parses_make_40(self, mock_check_output):
        mock_check_output.return_value = "GNU Make 4.0\nCopyright ...\n"
        assert _get_make_version() == (4, 0)

    @patch("compiletools.makefile_backend.subprocess.check_output")
    def test_returns_zero_on_error(self, mock_check_output):
        mock_check_output.side_effect = subprocess.CalledProcessError(1, "make")
        assert _get_make_version() == (0, 0)

    @patch("compiletools.makefile_backend.subprocess.check_output")
    def test_returns_zero_on_file_not_found(self, mock_check_output):
        mock_check_output.side_effect = FileNotFoundError
        assert _get_make_version() == (0, 0)


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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 4))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 4))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(3, 81))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 4))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(3, 81))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 4))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 0))
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

    @patch("compiletools.makefile_backend._get_make_version", return_value=(4, 4))
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
            objdir="/tmp/obj",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_test_result_rule_in_makefile(self):
        """Test rules should produce .result file recipes in the Makefile."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="bin/test_foo.result",
            inputs=["bin/test_foo"],
            command=["rm", "-f", "bin/test_foo.result", "&&", "bin/test_foo", "&&", "touch", "bin/test_foo.result"],
            rule_type="test",
        ))
        graph.add_rule(BuildRule(
            output="runtests",
            inputs=["bin/test_foo.result"],
            command=None,
            rule_type="phony",
        ))

        args = self._make_args()
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert "bin/test_foo.result: bin/test_foo" in content
        assert "rm -f bin/test_foo.result && bin/test_foo && touch bin/test_foo.result" in content
        assert ".PHONY: runtests" in content
        assert "runtests: bin/test_foo.result" in content

    def test_test_verbose_echo(self):
        """Test rules should include echo when verbose >= 1."""
        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="bin/test_foo.result",
            inputs=["bin/test_foo"],
            command=["rm", "-f", "bin/test_foo.result", "&&", "bin/test_foo", "&&", "touch", "bin/test_foo.result"],
            rule_type="test",
        ))

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
        graph.add_rule(BuildRule(
            output="bin/test_foo.result",
            inputs=["bin/test_foo"],
            command=["rm", "-f", "bin/test_foo.result", "&&", "bin/test_foo", "&&", "touch", "bin/test_foo.result"],
            rule_type="test",
        ))

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
        graph.add_rule(BuildRule(
            output="bin/test_foo.result",
            inputs=["bin/test_foo"],
            command=["rm", "-f", "bin/test_foo.result", "&&", "bin/test_foo", "&&", "touch", "bin/test_foo.result"],
            rule_type="test",
        ))

        args = self._make_args(serialisetests=False)
        hunter = MagicMock()
        backend = MakefileBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        assert ".NOTPARALLEL" not in content
