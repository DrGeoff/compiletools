"""Unit tests for the Bazel build backend (no compiler required)."""

import contextlib
import io
import os
import shutil
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from compiletools.bazel_backend import BazelBackend
from compiletools.build_backend import (
    compute_link_signature,
    extract_copts,
    extract_include_paths,
    extract_linkopts,
    get_backend_class,
)
from compiletools.build_graph import BuildGraph, BuildRule


def _make_bazelrc_backend(*, ldflags="", jvm_stack="256k", parallel: "int | None" = 1, graph=None):
    """Build a BazelBackend with MagicMock args wired for the bazelrc-content
    tests. Deduplicates `_backend_with` methods that previously lived on
    TestBazelLinkerDefault, TestBazelJvmStackSize, and TestBazelActiveProcessorCount."""
    args = MagicMock()
    args.LDFLAGS = ldflags
    args.bazel_jvm_stack_size = jvm_stack
    args.parallel = parallel
    args.CC = ""
    args.CXX = ""
    args.tests = []
    backend = BazelBackend(args=args, hunter=MagicMock())
    backend._graph = graph
    return backend


def _bazelrc_content(backend):
    """Render the bazelrc content with os.path.exists stubbed False (no
    system cacerts) — the standard precondition for the bazelrc-content
    tests above. Centralises the 2-line `with patch(...)` block."""
    with patch("os.path.exists", return_value=False):
        return backend._build_bazelrc_content()


@contextlib.contextmanager
def _patched_bazel_execute(backend):
    """Patch the standard bazel-execute dependencies (shutil.which to locate
    bazel, os.path.isdir, backend._run_bazel, _write_bazelrc, _publish_test_results)
    that nearly every TestBazelRunsTestsInBuildPhase test sets up. Yields
    (mock_run, mock_publish) for post-execute assertions."""
    with (
        patch("shutil.which", side_effect=lambda n: "/usr/bin/bazel" if n == "bazel" else None),
        patch("os.path.isdir", return_value=False),
        patch.object(backend, "_run_bazel") as mock_run,
        patch.object(backend, "_write_bazelrc"),
        patch.object(backend, "_publish_test_results") as mock_publish,
    ):
        yield mock_run, mock_publish


class TestBazelBackendRegistered:
    def test_registered_as_bazel(self):
        cls = get_backend_class("bazel")
        assert cls is BazelBackend

    def test_name(self):
        assert BazelBackend.name() == "bazel"

    def test_build_filename(self):
        assert BazelBackend.build_filename() == "BUILD.bazel"


class TestBazelGenerate:
    def _make_backend(self):
        args = MagicMock()
        hunter = MagicMock()
        return BazelBackend(args=args, hunter=hunter)

    def _generate(self, graph):
        """Build a BazelBackend, run generate on *graph*, return content."""
        backend = self._make_backend()
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        return buf.getvalue()

    def test_single_source_cc_binary(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/hello.o",
                inputs=["hello.cpp"],
                command=["g++", "-O2", "-c", "hello.cpp", "-o", "obj/hello.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/hello",
                inputs=["obj/hello.o"],
                command=["g++", "-o", "bin/hello", "obj/hello.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert "cc_binary(" in content
        assert 'name = "hello"' in content
        assert '"hello.cpp"' in content

    def test_multiple_sources(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp"],
                command=["g++", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="obj/util.o",
                inputs=["util.cpp", "util.h"],
                command=["g++", "-c", "util.cpp", "-o", "obj/util.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/app",
                inputs=["obj/main.o", "obj/util.o"],
                command=["g++", "-o", "bin/app", "obj/main.o", "obj/util.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert 'name = "app"' in content
        assert '"main.cpp"' in content
        assert '"util.cpp"' in content
        assert '"util.h"' in content

    def test_magic_flags_in_copts(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-O2", "-std=c++17", "-DFOO=1", "-c", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/foo",
                inputs=["obj/foo.o"],
                command=["g++", "-o", "bin/foo", "obj/foo.o", "-lm"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert '"-O2"' in content
        assert '"-std=c++17"' in content
        assert '"-DFOO=1"' in content
        assert '"-lm"' in content

    def test_include_paths_emitted_as_includes(self):
        """//#INCLUDE= annotations produce includes=[...] in the cc_binary stanza."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp"],
                command=["g++", "-Isubdir", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/main",
                inputs=["obj/main.o"],
                command=["g++", "-o", "bin/main", "obj/main.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert "includes = [" in content
        assert '"subdir"' in content
        # The -I path must NOT appear in copts (strip_includes=True removes it).
        assert '"-Isubdir"' not in content

    def test_no_includes_when_no_dash_I(self):
        """When no -I flags are present, includes= is omitted."""
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp"],
                command=["g++", "-O2", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/main",
                inputs=["obj/main.o"],
                command=["g++", "-o", "bin/main", "obj/main.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert "includes = [" not in content

    def test_phony_rules_not_emitted(self):
        graph = BuildGraph()
        graph.add_rule(BuildRule(output="build", inputs=["bin/foo"], command=None, rule_type="phony"))
        graph.add_rule(BuildRule(output="all", inputs=["build"], command=None, rule_type="phony"))

        content = self._generate(graph)

        assert "cc_binary" not in content
        assert "phony" not in content

    def test_empty_graph(self):
        graph = BuildGraph()

        content = self._generate(graph)

        assert "BUILD.bazel generated by compiletools" in content
        assert "cc_binary" not in content

    def test_headers_in_srcs(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp", "config.h", "types.h"],
                command=["g++", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/main",
                inputs=["obj/main.o"],
                command=["g++", "-o", "bin/main", "obj/main.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        assert '"config.h"' in content
        assert '"types.h"' in content

    def test_known_limitation_cc_binary_does_not_use_cc_library_deps(self):
        """Breadcrumb: when a graph contains both a static_library and a
        link rule that consumes its objects, the emitted cc_binary
        re-lists the library's source files in its own srcs rather than
        using ``deps = [":mylib"]``. This is correct (Bazel still
        compiles each .cpp once per binary) but suboptimal — at scale
        bazel duplicates compile work across binaries that share libs.

        When this assertion stops holding, the cc_library deps gap has
        been fixed; remove this test, validate the corresponding
        CLAUDE.md notes, and update the bazel_backend docstring.
        """
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/lib_only.o",
                inputs=["lib_only.cpp"],
                command=["g++", "-c", "lib_only.cpp", "-o", "obj/lib_only.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/libmylib.a",
                inputs=["obj/lib_only.o"],
                command=["ar", "rcs", "bin/libmylib.a", "obj/lib_only.o"],
                rule_type="static_library",
            )
        )
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp"],
                command=["g++", "-c", "main.cpp", "-o", "obj/main.o"],
                rule_type="compile",
            )
        )
        # Binary's link rule consumes BOTH the lib's object and its own.
        graph.add_rule(
            BuildRule(
                output="bin/app",
                inputs=["obj/main.o", "obj/lib_only.o"],
                command=["g++", "-o", "bin/app", "obj/main.o", "obj/lib_only.o"],
                rule_type="link",
            )
        )

        content = self._generate(graph)

        # Library and binary both exist.
        assert "cc_library(" in content
        assert "cc_binary(" in content
        # The library source appears in both targets (the gap).
        assert content.count('"lib_only.cpp"') == 2, (
            f"expected lib source duplicated across cc_library and cc_binary; if the "
            f"deps gap is fixed and cc_binary now uses deps=[':libmylib_a'], remove "
            f"this regression marker. content:\n{content}"
        )
        # No deps wiring yet.
        assert "deps =" not in content


class TestCoptsExtraction:
    def test_basic_flags(self):
        cmd = ["g++", "-O2", "-std=c++17", "-c", "foo.cpp", "-o", "obj/foo.o"]
        assert extract_copts(cmd, strip_includes=True) == ["-O2", "-std=c++17"]

    def test_define_flags(self):
        cmd = ["g++", "-DFOO=1", "-DBAR", "-c", "foo.cpp", "-o", "obj/foo.o"]
        assert extract_copts(cmd, strip_includes=True) == ["-DFOO=1", "-DBAR"]

    def test_empty_command(self):
        assert extract_copts([]) == []

    def test_complex_flags(self):
        # -I flags are filtered out since Bazel manages includes itself
        cmd = ["g++", "-Wall", "-Wextra", "-I/usr/include", "-c", "x.cpp", "-o", "x.o"]
        assert extract_copts(cmd, strip_includes=True) == ["-Wall", "-Wextra"]


class TestIncludePathsExtraction:
    def test_attached_dash_I(self):
        cmd = ["g++", "-c", "-Isubdir", "-I", "subdir2", "-isystem/usr/include", "main.cpp", "-o", "main.o"]
        paths = extract_include_paths(cmd)
        assert paths == ["subdir", "subdir2", "/usr/include"]

    def test_iquote(self):
        cmd = ["g++", "-c", "-iquote", "include", "-iquote/abs/path", "x.cpp", "-o", "x.o"]
        paths = extract_include_paths(cmd)
        assert paths == ["include", "/abs/path"]

    def test_empty(self):
        assert extract_include_paths([]) == []
        assert extract_include_paths(["g++", "-c", "x.cpp", "-o", "x.o"]) == []

    def test_isystem_equals_form(self):
        cmd = ["g++", "-c", "-isystem=/usr/include/boost", "x.cpp", "-o", "x.o"]
        paths = extract_include_paths(cmd)
        assert paths == ["/usr/include/boost"]

    def test_iquote_equals_form(self):
        cmd = ["g++", "-c", "-iquote=/abs/path", "y.cpp", "-o", "y.o"]
        paths = extract_include_paths(cmd)
        assert paths == ["/abs/path"]

    def test_dash_I_equals_form(self):
        # Triad consistency: -I=foo strips the leading '=' just like
        # -isystem=foo and -iquote=foo. Regression: previously returned
        # the literal "=foo" path, contradicting the docstring.
        cmd = ["g++", "-c", "-I=foo", "x.cpp", "-o", "x.o"]
        paths = extract_include_paths(cmd)
        assert paths == ["foo"]


class TestLinkoptsExtraction:
    def test_library_flags(self):
        cmd = ["g++", "-o", "bin/foo", "obj/foo.o", "-lm", "-lpthread"]
        objs = {"obj/foo.o"}
        assert extract_linkopts(cmd, objs) == ["-lm", "-lpthread"]

    def test_no_extra_flags(self):
        cmd = ["g++", "-o", "bin/foo", "obj/foo.o"]
        objs = {"obj/foo.o"}
        assert extract_linkopts(cmd, objs) == []

    def test_empty_command(self):
        assert extract_linkopts([], set()) == []

    def test_multiple_objects_stripped(self):
        cmd = ["g++", "-o", "bin/app", "obj/a.o", "obj/b.o", "-lz"]
        objs = {"obj/a.o", "obj/b.o"}
        assert extract_linkopts(cmd, objs) == ["-lz"]


class TestStarlarkStringEscape:
    """``BazelBackend._starlark_str`` must produce a parser-safe quoted literal
    for any input — paths and flag tokens may contain double quotes, backslashes,
    or control characters, and an unescaped one would break BUILD.bazel."""

    def test_plain_path_unchanged(self):
        assert BazelBackend._starlark_str("foo/bar.cpp") == '"foo/bar.cpp"'

    def test_double_quote_escaped(self):
        assert BazelBackend._starlark_str('weird"name.cpp') == '"weird\\"name.cpp"'

    def test_backslash_escaped(self):
        # Path with a literal backslash (rare on POSIX but legal in filenames).
        assert BazelBackend._starlark_str("a\\b.cpp") == '"a\\\\b.cpp"'

    def test_newline_escaped(self):
        assert BazelBackend._starlark_str("line1\nline2") == '"line1\\nline2"'

    def test_control_char_raises(self):
        # Bazel's Java Starlark parser does not accept \xNN escapes, so we
        # refuse to emit them up front rather than producing bytes Bazel
        # would reject as a syntax error.
        with pytest.raises(ValueError, match="control char"):
            BazelBackend._starlark_str("a\x07b")
        with pytest.raises(ValueError, match="control char"):
            BazelBackend._starlark_str("\x7f")  # DEL

    def test_starlark_copt_preserves_double_quotes(self):
        """``_starlark_copt`` uses double-escaping so Bazel's shell tokenizer
        preserves ``"`` chars in the compiler argv.

        Bazel applies Bourne-shell tokenization to ``copts`` values after
        Starlark parses them.  A plain ``\\"`` (``_starlark_str`` form) in the
        BUILD.bazel file survives Starlark parsing as a literal ``"`` but is
        then stripped by the shell tokenizer.  ``_starlark_copt`` emits
        ``\\\\\\\"`` (Starlark: ``\\\\`` + ``\\"``) so the shell tokenizer sees
        the two-char sequence ``\\"`` and passes a literal ``"`` to gcc.
        """
        # Plain flags are unchanged vs _starlark_str.
        assert BazelBackend._starlark_copt("-fPIC") == '"-fPIC"'
        assert BazelBackend._starlark_copt("-Wall") == '"-Wall"'

        # -DCT_PROJECT_VERSION="1.2.3" must be double-escaped so it reaches
        # the compiler as -DCT_PROJECT_VERSION="1.2.3" (with literal ").
        result = BazelBackend._starlark_copt('-DCT_PROJECT_VERSION="1.2.3"')
        # BUILD.bazel text: "-DCT_PROJECT_VERSION=\\\"1.2.3\\\""
        assert result == '"-DCT_PROJECT_VERSION=\\\\\\"1.2.3\\\\\\""'
        # Verify it differs from the broken single-escape form.
        assert result != '"-DCT_PROJECT_VERSION=\\"1.2.3\\""'

        # Same for a string-valued project name define.
        result_name = BazelBackend._starlark_copt('-DCT_PROJECT_NAME="demo_app"')
        assert result_name == '"-DCT_PROJECT_NAME=\\\\\\"demo_app\\\\\\""'

    def test_emit_target_quotes_pathological_src(self):
        """End-to-end: a src containing ``"`` must round-trip through
        ``_emit_target`` without producing an unbalanced quote in the output."""

        buf = StringIO()
        BazelBackend._emit_target(
            buf,
            "cc_binary",
            "weird_target",
            ['weird"src.cpp', "normal.cpp"],
            ['-DFOO="x"'],
            ["-lpthread"],
        )
        out = buf.getvalue()
        # The pathological src uses single-escape (_starlark_str): \" for each ".
        assert '"weird\\"src.cpp"' in out
        # The pathological copt uses double-escape (_starlark_copt): \\\" for each "
        # so that Bazel's Bourne-shell tokenizer preserves the " in the compiler argv.
        assert '"-DFOO=\\\\\\"x\\\\\\""' in out
        # The old single-escape form must NOT appear in the copt (it would let
        # Bazel's shell tokenizer strip the " before reaching the compiler).
        assert '"-DFOO=\\"x\\""' not in out
        # No bare ``"weird"src.cpp"`` (would indicate an unescaped quote in srcs).
        assert '"weird"src.cpp"' not in out


class TestBazelRunsTestsInBuildPhase:
    """The build phase runs tests natively: when the graph has RuleType.TEST
    rules, ``//:all`` is driven with ``bazel test`` (which builds non-test
    targets AND runs every cc_test), and ``_publish_test_results`` stamps
    ``.result`` markers / publishes JUnit XML afterwards.
    """

    @staticmethod
    def _backend_with_test_graph(*, testprefix="", test_xml_dir=None):
        args = MagicMock()
        args.parallel = None
        args.TESTPREFIX = testprefix
        args.test_xml_dir = test_xml_dir
        backend = BazelBackend(args=args, hunter=MagicMock())
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/test_foo",
                inputs=["obj/test_foo.o"],
                command=["g++", "-o", "bin/test_foo", "obj/test_foo.o"],
                rule_type="link",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/test_foo.result",
                inputs=["bin/test_foo"],
                command=["bin/test_foo"],
                rule_type="test",
                success_marker="bin/test_foo.result",
            )
        )
        backend._graph = graph
        return backend

    def test_execute_build_uses_bazel_test_when_graph_has_tests(self):
        backend = self._backend_with_test_graph()
        with _patched_bazel_execute(backend) as (mock_run, mock_publish):
            backend.execute("build")

        cmd = mock_run.call_args[0][0]
        assert cmd[1] == "test", cmd
        assert cmd[-1] == "//:all", cmd
        mock_publish.assert_called_once()

    def test_execute_build_uses_bazel_build_when_no_tests(self):
        args = MagicMock()
        args.parallel = None
        backend = BazelBackend(args=args, hunter=MagicMock())
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/app",
                inputs=["obj/app.o"],
                command=["g++", "-o", "bin/app", "obj/app.o"],
                rule_type="link",
            )
        )
        backend._graph = graph
        with _patched_bazel_execute(backend) as (mock_run, mock_publish):
            backend.execute("build")

        assert mock_run.call_args[0][0][1] == "build"
        mock_publish.assert_not_called()

    def test_run_under_plumbs_testprefix(self):
        backend = self._backend_with_test_graph(testprefix="valgrind --error-exitcode=1")
        with _patched_bazel_execute(backend) as (mock_run, _mock_publish):
            backend.execute("build")

        cmd = mock_run.call_args[0][0]
        assert "--run_under=valgrind --error-exitcode=1" in cmd, cmd

    def test_serialise_tests_adds_local_test_jobs_flag(self):
        """--serialise-tests passes ``--local_test_jobs=1`` to bazel so only
        one test runs at a time while compilation still parallelises freely."""
        backend = self._backend_with_test_graph()
        backend.args.serialisetests = True
        with _patched_bazel_execute(backend) as (mock_run, _mock_publish):
            backend.execute("build")

        cmd = mock_run.call_args[0][0]
        assert "--local_test_jobs=1" in cmd, cmd

    def test_no_local_test_jobs_flag_when_not_serialised(self):
        """Without --serialise-tests, ``--local_test_jobs`` must not appear."""
        backend = self._backend_with_test_graph()
        backend.args.serialisetests = False
        with _patched_bazel_execute(backend) as (mock_run, _mock_publish):
            backend.execute("build")

        cmd = mock_run.call_args[0][0]
        assert "--local_test_jobs=1" not in cmd, cmd

    def test_publish_test_results_touches_marker_and_copies_xml(self, tmp_path, monkeypatch):
        """_publish_test_results touches each test's .result marker and copies
        bazel's bazel-testlogs/<target>/test.xml to the per-test XML path."""
        monkeypatch.chdir(tmp_path)
        testlog_dir = tmp_path / "bazel-testlogs" / "test_foo"
        testlog_dir.mkdir(parents=True)
        (testlog_dir / "test.xml").write_text('<testsuites><testsuite failures="0" errors="0"/></testsuites>')
        (tmp_path / "bin").mkdir()
        exe_path = str(tmp_path / "bin" / "test_foo")

        args = MagicMock()
        args.test_xml_dir = str(tmp_path / "xmlout")
        args.variant = "gcc.debug"
        args.use_mtime = False
        backend = BazelBackend(args=args, hunter=MagicMock())
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=exe_path + ".result",
                inputs=[exe_path],
                command=[exe_path],
                rule_type="test",
                success_marker=exe_path + ".result",
            )
        )
        backend._graph = graph

        backend._publish_test_results()

        assert os.path.exists(exe_path + ".result"), "test .result marker not touched"
        assert os.path.exists(os.path.join(str(tmp_path / "xmlout"), "gcc.debug", "test_foo.xml")), (
            "bazel test.xml not published to the per-test XML path"
        )

    def test_publish_test_results_raises_on_xml_failures(self, tmp_path, monkeypatch):
        """A test.xml reporting failures makes _publish_test_results raise even
        though bazel test exited 0 (defensive cross-check)."""
        monkeypatch.chdir(tmp_path)
        testlog_dir = tmp_path / "bazel-testlogs" / "test_foo"
        testlog_dir.mkdir(parents=True)
        (testlog_dir / "test.xml").write_text('<testsuites><testsuite failures="1" errors="0"/></testsuites>')
        (tmp_path / "bin").mkdir()
        exe_path = str(tmp_path / "bin" / "test_foo")

        args = MagicMock()
        args.test_xml_dir = None
        args.use_mtime = False
        backend = BazelBackend(args=args, hunter=MagicMock())
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output=exe_path + ".result",
                inputs=[exe_path],
                command=[exe_path],
                rule_type="test",
                success_marker=exe_path + ".result",
            )
        )
        backend._graph = graph

        with pytest.raises(RuntimeError, match="reported failures"):
            backend._publish_test_results()
        assert not os.path.exists(exe_path + ".result"), "marker stamped despite reported failure"


class TestBazelCopyExecutables:
    def test_copy_built_executables_to_namer_paths(self, tmp_path):
        """After bazel build, executables should be copied to namer paths."""
        # Set up a fake bazel-bin directory
        bazel_bin = tmp_path / "bazel-bin"
        bazel_bin.mkdir()
        exe = bazel_bin / "helloworld_cpp"
        exe.write_text("#!/bin/sh\necho hello")
        exe.chmod(0o755)

        args = MagicMock()
        args.filename = ["/src/helloworld_cpp.cpp"]
        args.tests = []
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)

        dest_path = str(tmp_path / "obj" / "helloworld_cpp")
        backend.namer = MagicMock()
        backend.namer.executable_pathname = MagicMock(return_value=dest_path)

        with patch("compiletools.wrappedos.realpath", side_effect=lambda x: x):
            backend._copy_built_executables(str(bazel_bin))


        assert os.path.exists(dest_path)

    def test_copy_built_executables_from_subdirectory(self, tmp_path):
        """Executables in subdirectories of bazel-bin should be found."""
        bazel_bin = tmp_path / "bazel-bin"
        (bazel_bin / "subdir" / "pkg").mkdir(parents=True)
        exe = bazel_bin / "subdir" / "pkg" / "myapp"
        exe.write_text("#!/bin/sh\necho hello")
        exe.chmod(0o755)

        args = MagicMock()
        args.filename = ["/src/subdir/myapp.cpp"]
        args.tests = []
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)

        dest_path = str(tmp_path / "obj" / "myapp")
        backend.namer = MagicMock()
        backend.namer.executable_pathname = MagicMock(return_value=dest_path)

        with patch("compiletools.wrappedos.realpath", side_effect=lambda x: x):
            backend._copy_built_executables(str(bazel_bin))


        assert os.path.exists(dest_path)

    def test_copy_built_executables_mangled_name_in_subdirectory(self, tmp_path):
        """Mangled names in subdirectories should be found and copied."""
        bazel_bin = tmp_path / "bazel-bin"
        (bazel_bin / "src" / "tests").mkdir(parents=True)
        exe = bazel_bin / "src" / "tests" / "my_test_app"
        exe.write_text("#!/bin/sh\necho test")
        exe.chmod(0o755)

        args = MagicMock()
        args.filename = ["/src/my-test-app.cpp"]
        args.tests = []
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)

        dest_path = str(tmp_path / "obj" / "my-test-app")
        backend.namer = MagicMock()
        backend.namer.executable_pathname = MagicMock(return_value=dest_path)

        with patch("compiletools.wrappedos.realpath", side_effect=lambda x: x):
            backend._copy_built_executables(str(bazel_bin))


        assert os.path.exists(dest_path)


class TestBazelPublishOutputs:
    """Regression: ``_publish_bazel_outputs`` must land test executables at
    ``namer.executable_pathname(source)`` (the path ``_run_tests`` computes),
    not at topbindir.

    The original bug placed exes at ``topbindir/<name>`` while
    ``_run_tests`` looked at ``bin/<variant>/<name>``, so the subprocess
    call raised FileNotFoundError on every bazel build with tests.
    """

    @pytest.fixture(autouse=True)
    def _chdir_to_tmp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

    def test_publish_lands_test_exes_at_namer_paths(self, tmp_path):
        bazel_bin = tmp_path / "bazel-bin"
        bazel_bin.mkdir()
        test_exe = bazel_bin / "test_combinator"
        test_exe.write_text("#!/bin/sh\nexit 0\n")
        test_exe.chmod(0o555)  # r-x, matching real bazel output

        args = MagicMock()
        args.filename = []
        args.tests = ["/src/test_combinator.cpp"]
        backend = BazelBackend(args=args, hunter=MagicMock())

        expected_path = str(tmp_path / "bin" / "gcc.debug" / "test_combinator")
        backend.namer = MagicMock()
        backend.namer.executable_pathname = MagicMock(return_value=expected_path)
        backend.namer.topbindir = MagicMock(return_value=str(tmp_path / "bin") + "/")
        backend._graph = None

        with patch("compiletools.wrappedos.realpath", side_effect=lambda x: x):
            backend._publish_bazel_outputs()

        # The path _run_tests would query (via namer.executable_pathname) must
        # exist. If _publish_bazel_outputs reverts to copying to topbindir, this
        # test fails because the test exe ends up at bin/test_combinator instead.
        assert os.path.exists(expected_path), (
            f"test exe not at namer.executable_pathname path {expected_path!r}; "
            f"_run_tests would raise FileNotFoundError"
        )

    def test_publish_idempotent_on_rerun_with_readonly_dest(self, tmp_path):
        """Second invocation must not fail with EACCES on the r-x output."""
        bazel_bin = tmp_path / "bazel-bin"
        bazel_bin.mkdir()
        exe = bazel_bin / "myapp"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o555)

        args = MagicMock()
        args.filename = ["/src/myapp.cpp"]
        args.tests = []
        backend = BazelBackend(args=args, hunter=MagicMock())

        expected_path = str(tmp_path / "bin" / "gcc.debug" / "myapp")
        backend.namer = MagicMock()
        backend.namer.executable_pathname = MagicMock(return_value=expected_path)
        backend.namer.topbindir = MagicMock(return_value=str(tmp_path / "bin") + "/")
        backend._graph = None

        with patch("compiletools.wrappedos.realpath", side_effect=lambda x: x):
            backend._publish_bazel_outputs()
            backend._publish_bazel_outputs()  # rerun must not raise

        assert os.path.exists(expected_path)


class TestBazelClean:
    def test_clean_runs_bazel_clean_command(self, tmp_path):
        """clean() should invoke 'bazel clean' then call super().clean()."""
        args = MagicMock()
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)
        backend.namer = MagicMock()
        backend.namer.executable_dir.return_value = str(tmp_path / "exe")
        backend.namer.object_dir.return_value = str(tmp_path / "obj")

        with (
            patch("subprocess.check_call") as mock_check_call,
            patch("shutil.which", side_effect=lambda name: "/usr/bin/bazel" if name == "bazel" else None),
        ):
            backend.clean()
            mock_check_call.assert_called_once_with(["/usr/bin/bazel", "clean"], text=True)


class TestBazelExecute:
    def _make_backend(self):
        args = MagicMock()
        args.LDFLAGS = ""  # explicit: prod always provides a str
        hunter = MagicMock()
        return BazelBackend(args=args, hunter=hunter)

    @patch("os.path.exists", return_value=True)
    def test_bazelrc_includes_tls_workaround_when_cacerts_exist(self, mock_exists):
        backend = self._make_backend()
        content = backend._build_bazelrc_content()
        assert "trustStore=" in content, f"Expected trustStore in {content!r}"
        assert "trustStorePassword=" in content, f"Expected trustStorePassword in {content!r}"

    def test_bazelrc_omits_tls_workaround_when_no_cacerts(self):
        content = _bazelrc_content(self._make_backend())
        assert "trustStore" not in content, f"Unexpected trustStore in {content!r}"

    def test_bazelrc_includes_spawn_strategy_and_action_env(self):
        content = _bazelrc_content(self._make_backend())
        assert "build --spawn_strategy=local" in content
        assert "build --action_env=PATH" in content

    def test_bazelrc_propagates_configured_compiler(self):
        """args.CC / args.CXX must be passed to bazel so its autoconfig
        toolchain doesn't fall back to /bin/gcc (which on RHEL 8 is gcc 8
        and rejects -std=c++20)."""
        backend = _make_bazelrc_backend(parallel=4)
        backend.args.CC = "/opt/gcc-15/bin/gcc"
        backend.args.CXX = "/opt/gcc-15/bin/g++"

        content = _bazelrc_content(backend)

        assert "build --repo_env=CC=/opt/gcc-15/bin/gcc" in content, content
        assert "build --repo_env=CXX=/opt/gcc-15/bin/g++" in content, content
        assert "build --action_env=CC=/opt/gcc-15/bin/gcc" in content, content
        assert "build --action_env=CXX=/opt/gcc-15/bin/g++" in content, content

    def test_bazelrc_forwards_ld_library_path_to_test_env(self):
        """bazel test runs each cc_test in a hermetic sandbox that strips
        client env. Toolchains installed outside /lib64 ship their own
        libstdc++.so.6 with newer GLIBCXX_ versions; without forwarding
        LD_LIBRARY_PATH the loader falls back to /lib64 and the binary
        fails with `version 'GLIBCXX_3.4.NN' not found`."""
        content = _bazelrc_content(self._make_backend())
        assert "build --test_env=LD_LIBRARY_PATH" in content, content

    def test_execute_passes_minimal_cli_to_run_bazel(self):
        """All static-per-build flags live in .bazelrc; the CLI carries
        only the bazel binary, ``build``, optional ``--jobs=N``, and
        the target."""
        args = MagicMock()
        args.parallel = 4
        backend = BazelBackend(args=args, hunter=MagicMock())
        with (
            patch("shutil.which", side_effect=lambda n: "/usr/bin/bazel" if n == "bazel" else None),
            patch("os.path.isdir", return_value=False),
            patch.object(backend, "_run_bazel") as mock_run,
            patch.object(backend, "_write_bazelrc") as mock_write,
        ):
            backend.execute("build")

        cmd = mock_run.call_args[0][0]
        # Pin: every static-per-build flag belongs in .bazelrc, not the CLI.
        # A failure here means a flag has crept back onto the CLI and a
        # peer's manual `bazel build //:all` will diverge from the wrapper.
        assert cmd == ["/usr/bin/bazel", "build", "--jobs=4", "//:all"], cmd
        mock_write.assert_called_once()


class TestBazelWriteBazelrc:
    """End-to-end: ``_execute_build`` actually persists ``.bazelrc`` next to
    BUILD.bazel so a manual ``bazel build //:all`` from the workspace picks
    up the same flags as the wrapper-driven build."""

    def test_write_bazelrc_persists_content_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = MagicMock()
        args.parallel = 2
        args.bazel_jvm_stack_size = "256k"
        args.LDFLAGS = ""
        args.CC = ""
        args.CXX = ""
        args.tests = []
        backend = BazelBackend(args=args, hunter=MagicMock())
        backend._graph = None

        with (
            patch("shutil.which", side_effect=lambda n: "/usr/bin/bazel" if n == "bazel" else None),
            patch.object(backend, "_run_bazel"),
            patch.object(backend, "_publish_bazel_outputs"),
        ):
            backend.execute("build")

        bazelrc = tmp_path / ".bazelrc"
        assert bazelrc.exists(), "expected .bazelrc to be written by _execute_build"
        content = bazelrc.read_text()
        assert "build --spawn_strategy=local" in content
        assert "startup --host_jvm_args=-Xss256k" in content
        assert "startup --host_jvm_args=-XX:ActiveProcessorCount=2" in content

    def test_write_bazelrc_skips_when_content_unchanged(self, tmp_path):
        """Concurrent peer runs with the same args must not race on the
        atomic-rename. Eliminate the race by comparing first."""
        args = MagicMock()
        args.parallel = 2
        args.bazel_jvm_stack_size = "256k"
        args.LDFLAGS = ""
        args.CC = ""
        args.CXX = ""
        args.tests = []
        backend = BazelBackend(args=args, hunter=MagicMock())
        backend._graph = None

        path = tmp_path / ".bazelrc"
        with patch("os.path.exists", return_value=False):
            content = backend._build_bazelrc_content()
        path.write_text(content)
        original_mtime_ns = path.stat().st_mtime_ns

        # Second invocation with identical content must not rewrite (mtime
        # preserved). atomic_output_file would bump mtime on rewrite.
        with patch("os.path.exists", return_value=False):
            backend._write_bazelrc(str(tmp_path))
        assert path.stat().st_mtime_ns == original_mtime_ns, "expected skip-on-unchanged"

    def test_write_bazelrc_replaces_when_content_differs(self, tmp_path):
        args = MagicMock()
        args.parallel = 2
        args.bazel_jvm_stack_size = "256k"
        args.LDFLAGS = ""
        args.CC = ""
        args.CXX = ""
        args.tests = []
        backend = BazelBackend(args=args, hunter=MagicMock())
        backend._graph = None

        path = tmp_path / ".bazelrc"
        path.write_text("# stale content from a peer\n")
        with patch("os.path.exists", return_value=False):
            backend._write_bazelrc(str(tmp_path))
        assert path.read_text() != "# stale content from a peer\n"
        assert "build --spawn_strategy=local" in path.read_text()


class TestBazelLinkerDefault:
    """`--linkopt=-fuse-ld=gold` is added by default but skipped when the
    user has set their own -fuse-ld=... in LDFLAGS or in any per-rule
    link command (covers magic-flag-injected linker choices)."""

    def test_gold_added_when_no_user_setting(self):
        content = _bazelrc_content(_make_bazelrc_backend())
        assert "build --linkopt=-fuse-ld=gold" in content, content

    def test_gold_skipped_when_ldflags_sets_fuse_ld(self):
        content = _bazelrc_content(_make_bazelrc_backend(ldflags="-Wall -fuse-ld=mold"))
        assert "-fuse-ld=" not in content, content

    def test_user_set_fuse_ld_via_link_rule_command(self):
        # Test the predicate directly to avoid running the full execute path.
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/app",
                inputs=["obj/main.o"],
                command=["g++", "-Wl,-fuse-ld=lld", "-o", "bin/app", "obj/main.o"],
                rule_type="link",
            )
        )
        backend = _make_bazelrc_backend(graph=graph)
        assert backend._user_set_fuse_ld() is True

    def test_user_set_fuse_ld_via_shared_library_command(self):
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="bin/libfoo.so",
                inputs=["obj/foo.o"],
                command=["g++", "-shared", "-fuse-ld=mold", "-o", "bin/libfoo.so", "obj/foo.o"],
                rule_type="shared_library",
            )
        )
        backend = _make_bazelrc_backend(graph=graph)
        assert backend._user_set_fuse_ld() is True

    def test_user_set_fuse_ld_ignores_non_link_rules(self):
        # A compile rule containing -fuse-ld= shouldn't trigger (defensive: it can't
        # legitimately appear there, but the predicate filters by rule_type).
        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/foo.o",
                inputs=["foo.cpp"],
                command=["g++", "-c", "-fuse-ld=lld", "foo.cpp", "-o", "obj/foo.o"],
                rule_type="compile",
            )
        )
        backend = _make_bazelrc_backend(graph=graph)
        assert backend._user_set_fuse_ld() is False

    def test_user_set_fuse_ld_ignores_quoted_substring_in_define(self):
        # LDFLAGS that quote the substring inside an unrelated flag (e.g. a
        # -DSOMETHING value) must NOT false-positive — the prior substring
        # search would have. Tokenisation via shlex makes the predicate
        # syntactic rather than textual.
        backend = _make_bazelrc_backend(ldflags='-DPROBE_FLAG="-fuse-ld=lld" -lm')
        assert backend._user_set_fuse_ld() is False

    def test_user_set_fuse_ld_via_wl_passthrough(self):
        # -Wl,-fuse-ld=mold (possibly with comma-separated peers) must trigger.
        backend = _make_bazelrc_backend(ldflags="-Wl,-fuse-ld=mold,--no-as-needed")
        assert backend._user_set_fuse_ld() is True


class TestBazelJvmStackSize:
    """`--bazel-jvm-stack-size` drives the JVM -Xss host_jvm_args; empty disables."""

    def test_default_size_appears_in_bazelrc(self):
        content = _bazelrc_content(_make_bazelrc_backend(jvm_stack="256k"))
        assert "startup --host_jvm_args=-Xss256k" in content, content

    def test_custom_size_appears_in_bazelrc(self):
        content = _bazelrc_content(_make_bazelrc_backend(jvm_stack="512k"))
        assert "startup --host_jvm_args=-Xss512k" in content, content
        assert "-Xss256k" not in content, content

    def test_empty_skips_xss_flag(self):
        content = _bazelrc_content(_make_bazelrc_backend(jvm_stack=""))
        assert "-Xss" not in content, content


class TestBazelActiveProcessorCount:
    """`-XX:ActiveProcessorCount` matches args.parallel so bazel's JVM
    respects the canonical core-limit knob and doesn't pre-spawn nproc
    threads at server startup on many-core hosts."""

    def test_active_processor_count_matches_parallel(self):
        content = _bazelrc_content(_make_bazelrc_backend(parallel=8))
        assert "startup --host_jvm_args=-XX:ActiveProcessorCount=8" in content, content

    def test_no_flag_when_parallel_unset(self):
        content = _bazelrc_content(_make_bazelrc_backend(parallel=None))
        assert "ActiveProcessorCount" not in content, content


class TestBazelRunBazelDiagnostic:
    """`_run_bazel` augments CalledProcessError with a remediation hint
    when bazel stderr matches the rules_cc / missing-lld pattern."""

    def _make_backend(self):
        args = MagicMock()
        return BazelBackend(args=args, hunter=MagicMock())

    def _fake_proc(self, stderr_text: str, returncode: int):
        proc = MagicMock()
        proc.stderr = io.StringIO(stderr_text)
        proc.wait.return_value = returncode
        return proc

    def test_success_returns_quietly(self):
        backend = self._make_backend()
        with patch("subprocess.Popen", return_value=self._fake_proc("INFO: Build completed\n", 0)):
            backend._run_bazel(["bazel", "build", "//:all"])  # no exception

    def test_failure_without_marker_raises_plain_called_process_error(self):
        backend = self._make_backend()

        with patch("subprocess.Popen", return_value=self._fake_proc("undefined reference to `foo'\n", 1)):
            with pytest.raises(subprocess.CalledProcessError) as excinfo:
                backend._run_bazel(["bazel", "build", "//:all"])
        assert "Bazel's link step failed" not in (excinfo.value.stderr or "")

    def test_lld_failure_augments_stderr_with_clang_hint(self):
        backend = self._make_backend()

        stderr = "collect2: fatal error: cannot find 'ld'\ncompilation terminated.\n"
        with patch("subprocess.Popen", return_value=self._fake_proc(stderr, 1)):
            with pytest.raises(subprocess.CalledProcessError) as excinfo:
                backend._run_bazel(["bazel", "build", "//:all"])
        assert "clang" in (excinfo.value.stderr or "").lower()
        assert "fuse-ld=lld" in (excinfo.value.stderr or "")

    def test_toolchain_failure_augments_stderr(self):
        backend = self._make_backend()

        stderr = "ERROR: Could not find a C++ toolchain for the requested platform\n"
        with patch("subprocess.Popen", return_value=self._fake_proc(stderr, 1)):
            with pytest.raises(subprocess.CalledProcessError) as excinfo:
                backend._run_bazel(["bazel", "build", "//:all"])
        assert "clang" in (excinfo.value.stderr or "").lower()


class TestBazelGenerateNoFilesystemMutation:
    """generate() to a StringIO must NOT create files in ext/.

    Filesystem mutation (copying out-of-workspace sources into ext/)
    happens in _execute_build → _prepare_external_sources, not in
    generate(). Tests writing to a buffer should leave the cwd clean.
    """

    def test_generate_to_stringio_does_not_create_ext(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Source lives outside the (cwd) workspace, simulating an
        # out-of-tree dependency that would normally trigger the ext/
        # copy code path.
        outside = tmp_path.parent / "outside_src"
        outside.mkdir(exist_ok=True)
        src = outside / "external.cpp"
        src.write_text("int main() { return 0; }\n")

        graph = BuildGraph()
        graph.add_rule(
            BuildRule(
                output="obj/external.o",
                inputs=[str(src)],
                command=["g++", "-c", str(src), "-o", "obj/external.o"],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/external",
                inputs=["obj/external.o"],
                command=["g++", "-o", "bin/external", "obj/external.o"],
                rule_type="link",
            )
        )

        args = MagicMock()
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)

        buf = io.StringIO()
        backend.generate(graph, output=buf)

        # No ext/ directory must have been created in cwd.
        assert not (tmp_path / "ext").exists(), "generate() to StringIO must not create ext/ on disk"
        # And the buffer should contain the bazel rules.
        assert "cc_binary" in buf.getvalue()


class TestBazelPrepareExternalSources:
    """_prepare_external_sources copies out-of-workspace sources into ext/."""

    def test_copies_external_sources(self, tmp_path):
        outside = tmp_path.parent / "external_dir_for_bazel_test"
        outside.mkdir(exist_ok=True)
        src = outside / "ext_source.cpp"
        src.write_text("int main() { return 0; }\n")

        try:
            base_dir = tmp_path / "workspace"
            base_dir.mkdir()

            graph = BuildGraph()
            graph.add_rule(
                BuildRule(
                    output="obj/ext_source.o",
                    inputs=[str(src)],
                    command=["g++", "-c", str(src), "-o", "obj/ext_source.o"],
                    rule_type="compile",
                )
            )
            graph.add_rule(
                BuildRule(
                    output="bin/ext_source",
                    inputs=["obj/ext_source.o"],
                    command=["g++", "-o", "bin/ext_source", "obj/ext_source.o"],
                    rule_type="link",
                )
            )

            args = MagicMock()
            hunter = MagicMock()
            backend = BazelBackend(args=args, hunter=hunter)
            backend._prepare_external_sources(graph, str(base_dir))

            assert (base_dir / "ext" / "ext_source.cpp").exists()
        finally:

            if outside.exists():
                shutil.rmtree(outside)


class TestBazelAllOutputsCurrent:
    """Bazel backends place outputs in bazel-bin/, not at namer paths,
    so the base-class _all_outputs_current pre-check would mis-fire.
    The override must return False unconditionally so the build (and
    post-build copy from bazel-bin/) always runs."""

    def test_always_returns_false_even_when_outputs_exist(self, tmp_path):

        args = MagicMock()
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)

        obj_path = str(tmp_path / "foo.o")
        exe_path = str(tmp_path / "main")
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
        with open(exe_path + ".ct-sig", "w") as f:
            f.write(compute_link_signature(link_rule))

        assert backend._all_outputs_current(graph) is False

    def test_always_returns_false_with_empty_graph(self):
        args = MagicMock()
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)
        assert backend._all_outputs_current(BuildGraph()) is False


class TestBazelNamedModuleHandling:
    """Verify that .cppm module interface sources are excluded from srcs=[],
    their prebuilt .gcm artefacts land in additional_compiler_inputs, and
    the bazel module mapper contains an entry for each named module."""

    def _make_backend(self, *, module_iface_obj=None, module_iface_gcm=None):
        args = MagicMock()
        args.LDFLAGS = ""  # explicit: prod always provides a str
        hunter = MagicMock()
        backend = BazelBackend(args=args, hunter=hunter)
        # Populate the named-module state that _write_build / _bazel_module_inputs_and_copts
        # consult at generation time. The backend normally gets these from build_graph().
        backend._module_iface_obj = module_iface_obj or {}
        backend._module_iface_gcm = module_iface_gcm or {}
        backend._module_iface_pcm = {}
        backend._module_compiler_kind = "gcc" if module_iface_gcm else None
        backend._module_pcm_cache_root = "/cas/pcm" if module_iface_gcm else None
        backend._gcc_header_unit_resolved = {}
        backend._header_unit_artefact = {}
        return backend

    def _build_simple_module_graph(self):
        """Return a graph with math.cppm (module iface) + main.cpp (importer).

        math.cppm  -> obj/math.o  (compile rule, gcc writes math.gcm as side-effect)
        main.cpp   -> obj/main.o  (compile rule, imports "math" via -fmodule-mapper=)
        link rule  -> bin/main    (link)
        """
        graph = BuildGraph()
        # Module interface compile rule
        graph.add_rule(
            BuildRule(
                output="obj/math.o",
                inputs=["math.cppm"],
                command=[
                    "g++",
                    "-fmodules-ts",
                    "-fmodule-mapper=.module-mapper.txt",
                    "-c",
                    "-x",
                    "c++",
                    "math.cppm",
                    "-o",
                    "obj/math.o",
                ],
                rule_type="compile",
            )
        )
        # Importer compile rule — lists math.o as ordering input (gcc)
        graph.add_rule(
            BuildRule(
                output="obj/main.o",
                inputs=["main.cpp", "obj/math.o"],
                command=[
                    "g++",
                    "-fmodules-ts",
                    "-fmodule-mapper=.module-mapper.txt",
                    "-c",
                    "main.cpp",
                    "-o",
                    "obj/main.o",
                ],
                rule_type="compile",
            )
        )
        graph.add_rule(
            BuildRule(
                output="bin/main",
                inputs=["obj/main.o", "obj/math.o"],
                command=["g++", "-o", "bin/main", "obj/main.o", "obj/math.o"],
                rule_type="link",
            )
        )
        return graph

    def test_cppm_excluded_from_srcs(self):
        """math.cppm must NOT appear in cc_binary srcs=[]; rules_cc rejects it."""
        graph = self._build_simple_module_graph()
        backend = self._make_backend(
            module_iface_obj={"math": "obj/math.o"},
            module_iface_gcm={"math": "cas-pcm/math.gcm"},
        )
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "math.cppm" not in content, (
            "math.cppm must not appear in BUILD.bazel: bazel's rules_cc ALLOWED_SRC_FILES does not include .cppm"
        )

    def test_gcm_in_additional_compiler_inputs(self):
        """The prebuilt .gcm artefact must appear in additional_compiler_inputs."""
        graph = self._build_simple_module_graph()
        backend = self._make_backend(
            module_iface_obj={"math": "obj/math.o"},
            module_iface_gcm={"math": "cas-pcm/math.gcm"},
        )
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "cas-pcm/math.gcm" in content, (
            "math.gcm must appear in additional_compiler_inputs so bazel's "
            "input validation sees the prebuilt BMI artefact"
        )

    def test_main_cpp_still_in_srcs(self):
        """main.cpp (the importer, not a module iface) must remain in srcs."""
        graph = self._build_simple_module_graph()
        backend = self._make_backend(
            module_iface_obj={"math": "obj/math.o"},
            module_iface_gcm={"math": "cas-pcm/math.gcm"},
        )
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "main.cpp" in content, "main.cpp (importer) must still appear in srcs"

    def test_prebuilt_obj_in_srcs_for_linking(self):
        """The prebuilt .o from math.cppm must appear in srcs so it gets linked."""
        graph = self._build_simple_module_graph()
        backend = self._make_backend(
            module_iface_obj={"math": "obj/math.o"},
            module_iface_gcm={"math": "cas-pcm/math.gcm"},
        )
        buf = io.StringIO()
        backend.generate(graph, output=buf)
        content = buf.getvalue()
        assert "obj/math.o" in content, (
            "obj/math.o (prebuilt interface object) must appear in srcs=[...] so "
            "its definitions are linked into the final cc_binary"
        )

    def test_mno_modules_not_injected_when_no_module_iface_gcm(self):
        """-Mno-modules must NOT appear in .bazelrc when _module_iface_gcm is empty.

        Clang builds and header-unit-only gcc builds have no named-module
        interface artefacts (.gcm files), so the gcc-specific workaround flag
        -Mno-modules should not appear in the generated bazelrc.
        """
        # Backend with empty _module_iface_gcm (no named-module GCC artefacts).
        backend = self._make_backend(
            module_iface_obj={},
            module_iface_gcm={},
        )
        content = backend._build_bazelrc_content()
        assert "-Mno-modules" not in content, (
            "-Mno-modules must not be injected into .bazelrc when there are no "
            "named-module GCC artefacts (_module_iface_gcm is empty)"
        )
