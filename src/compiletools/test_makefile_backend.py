import io
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import compiletools.makefile
import compiletools.testhelper as uth
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


class TestMakefileRealclean:
    """The realclean recipe must be selective in obj_dir, since obj_dir
    can be a shared location used by peer sub-projects."""

    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/shared-obj",
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
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _build_graph(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="/tmp/shared-obj/proj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "foo.cpp", "-o", "/tmp/shared-obj/proj/foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="/tmp/proj/bin/foo",
                inputs=["/tmp/shared-obj/proj/foo.o"],
                command=["g++", "-o", "/tmp/proj/bin/foo", "/tmp/shared-obj/proj/foo.o"],
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
        assert "rm -rf /tmp/shared-obj" not in recipe
        # Must list this build's outputs explicitly
        assert "/tmp/shared-obj/proj/foo.o" in recipe
        assert "/tmp/proj/bin/foo" in recipe
        # exe_dir is per-project so rm -rf on it is OK
        assert "rm -rf /tmp/proj/bin" in recipe
        # Should prune empty dirs in obj_dir afterwards
        assert "find /tmp/shared-obj -type d -empty -delete" in recipe

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
        assert "rm -rf /tmp/shared-obj" not in recipe
        assert "rm -f" not in recipe


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
        graph.add_rule(
            BuildRule(
                output="bin/test_foo.result",
                inputs=["bin/test_foo"],
                command=["bin/test_foo", "&&", "touch", "bin/test_foo.result"],
                rule_type="test",
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
                command=["bin/test_foo", "&&", "touch", "bin/test_foo.result"],
                rule_type="test",
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
                command=["bin/test_foo", "&&", "touch", "bin/test_foo.result"],
                rule_type="test",
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
                command=["bin/test_foo", "&&", "touch", "bin/test_foo.result"],
                rule_type="test",
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
                compiletools.makefile.main(["--config=" + cfg, "--no-file-locking", "--tests=" + str(source)])

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
                    compiletools.makefile.main(["--config=" + cfg, "--no-file-locking", str(source)])
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
            # Critical guarantee: when only BSD `date` is available and
            # $EPOCHREALTIME is unset, our wrapper validates the date
            # output and substitutes 0 (well-formed) rather than
            # ``1745247600`` (a truncated garbage int).  Look for the
            # ``"start_ns":0`` and ``"end_ns":0`` substrings — which
            # confirms the validation rejected the bogus BSD output.
            assert '"start_ns":0' in line, f"BSD date corruption leaked into log line: {line!r}"
            assert '"end_ns":0' in line, f"BSD date corruption leaked into log line: {line!r}"
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
        import time as _t

        p1 = b1._timing_log_path
        _t.sleep(0)  # let monotonic_ns tick (it's strictly monotonic anyway)
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
    """Fix 9: concurrent `make -j` against a shared objdir must produce a
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
                compiletools.makefile.main(["--config=" + cfg, "--file-locking=true"] + sources)

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
                        import shutil as _sh

                        _sh.rmtree(d, ignore_errors=True)

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
