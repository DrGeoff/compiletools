import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from compiletools.build_backend import get_backend_class
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.ninja_backend import NinjaBackend


class TestNinjaBackendRegistered:
    def test_registered_as_ninja(self):
        cls = get_backend_class("ninja")
        assert cls is NinjaBackend

    def test_name(self):
        assert NinjaBackend.name() == "ninja"

    def test_build_filename(self):
        assert NinjaBackend.build_filename() == "build.ninja"


class TestNinjaGenerate:
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
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
            sleep_interval_lockdir=None,
            sleep_interval_cifs=0.1,
            sleep_interval_flock_fallback=0.1,
            lock_warn_interval=30,
            lock_cross_host_timeout=600,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

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

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

        # Ninja uses "build <output>: <rule> <inputs>" syntax
        assert "build obj/foo.o: compile_cmd foo.cpp" in content
        assert "build bin/foo: link_cmd obj/foo.o" in content
        # Ninja uses "build <alias>: phony <deps>" for phony targets
        assert "build build: phony bin/foo" in content
        # Order-only deps use || in Ninja
        assert "|| /tmp/obj" in content

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

        args = self._make_args()
        hunter = MagicMock()
        hunter.huntsource = MagicMock()
        hunter.getsources = MagicMock(return_value=[])
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()

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
    def _make_args(self, **overrides):
        defaults = dict(
            verbose=0,
            objdir="/tmp/obj",
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
            sleep_interval_lockdir=None,
            sleep_interval_cifs=0.1,
            sleep_interval_flock_fallback=0.1,
            lock_warn_interval=30,
            lock_cross_host_timeout=600,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_compile_not_wrapped_when_locking_disabled(self):
        """Compile commands pass through unchanged when file_locking=False."""
        args = self._make_args(file_locking=False)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(_compile_graph(), output=buf)
        content = buf.getvalue()

        assert "ct-lock-helper" not in content
        assert "cmd = g++ -O2 -c foo.cpp -o obj/foo.o" in content

    def test_compile_wrapped_with_lockdir_strategy(self):
        """Compile commands are wrapped with ct-lock-helper on NFS."""
        args = self._make_args(file_locking=True, sleep_interval_lockdir=0.05)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with patch("compiletools.ninja_backend.check_lock_helper_available", return_value=True), \
             patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "ct-lock-helper" in content
        assert "--strategy=lockdir" in content
        assert "--target=obj/foo.o" in content
        assert "CT_LOCK_SLEEP_INTERVAL=0.05" in content
        # The compile command should appear after the -- separator
        assert "-- g++ -O2 -c foo.cpp" in content

    def test_compile_wrapped_with_flock_strategy(self):
        """Compile commands use flock strategy on local filesystems."""
        args = self._make_args(file_locking=True, sleep_interval_flock_fallback=0.03)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with patch("compiletools.ninja_backend.check_lock_helper_available", return_value=True), \
             patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "--strategy=flock" in content
        assert "CT_LOCK_SLEEP_INTERVAL_FLOCK=0.03" in content

    def test_compile_wrapped_with_cifs_strategy(self):
        """Compile commands use cifs strategy on CIFS filesystems."""
        args = self._make_args(file_locking=True, sleep_interval_cifs=0.02)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with patch("compiletools.ninja_backend.check_lock_helper_available", return_value=True), \
             patch("compiletools.filesystem_utils.get_filesystem_type", return_value="cifs"):
            buf = io.StringIO()
            backend.generate(_compile_graph(), output=buf)
            content = buf.getvalue()

        assert "--strategy=cifs" in content
        assert "CT_LOCK_SLEEP_INTERVAL_CIFS=0.02" in content

    def test_link_commands_not_wrapped(self):
        """Link commands are never wrapped with ct-lock-helper."""
        graph = _compile_graph()
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o"],
                rule_type="link",
            )
        )

        args = self._make_args(file_locking=True, sleep_interval_lockdir=0.05)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with patch("compiletools.ninja_backend.check_lock_helper_available", return_value=True), \
             patch("compiletools.filesystem_utils.get_filesystem_type", return_value="nfs"):
            buf = io.StringIO()
            backend.generate(graph, output=buf)
            content = buf.getvalue()

        # Link command should NOT have ct-lock-helper
        for line in content.splitlines():
            if "cmd = g++ -o bin/foo" in line:
                assert "ct-lock-helper" not in line
                break
        else:
            pytest.fail("link command not found in output")

    def test_ct_lock_helper_missing_exits(self):
        """generate() exits with error when ct-lock-helper is missing."""
        args = self._make_args(file_locking=True)
        hunter = MagicMock()
        backend = NinjaBackend(args=args, hunter=hunter)

        with patch("compiletools.ninja_backend.check_lock_helper_available", return_value=False), \
             pytest.raises(SystemExit):
            backend.generate(_compile_graph())
