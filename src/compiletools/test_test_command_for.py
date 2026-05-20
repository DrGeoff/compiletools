"""Contract tests for BuildBackend._test_command_for.

Verifies that the extracted argv-builder produces the testprefix parts
plus the framework-specific JUnit-XML argv declared in
``compiletools.test_framework.KNOWN_FRAMEWORKS`` when ``--test-xml-dir``
is set, and just ``[*testprefix_parts, exe_path]`` when it is not.

The stub backend used here comes from ``testhelper.make_stub_backend_class``,
which subclasses ``BuildBackend`` -- so ``_test_command_for`` is genuinely
inherited from the ABC, not stubbed.
"""

import os

from compiletools.testhelper import (
    make_backend_args,
    make_mock_hunter,
    make_mock_namer,
    make_stub_backend_class,
)


def _make_backend(tmpdir, *, source, headers, **arg_overrides):
    """Wire a stub BuildBackend with one test source and a fixed header set."""
    args = make_backend_args(tmpdir, tests=[source], **arg_overrides)
    hunter = make_mock_hunter(sources=[source], headers=headers)
    backend = make_stub_backend_class()(args=args, hunter=hunter)
    backend.namer = make_mock_namer(args)
    return backend, args


def _exe_path(backend, source):
    return backend.namer.executable_pathname(source)


def _xml_cmd(tmpdir, source, headers, **arg_overrides):
    """Build a backend with ``--test-xml-dir`` set and return
    ``(cmd, exe, expected_xml, framework)`` for the test source.

    Bundles the canonical pattern: every xml-related test sets
    ``xml_dir = <tmpdir>/xml`` and ``variant="gcc.debug"``, then expects
    the XML to land at ``<xml_dir>/<variant>/<basename(exe)>.xml``.
    """
    xml_dir = os.path.join(tmpdir, "xml")
    backend, _ = _make_backend(
        tmpdir,
        source=source,
        headers=headers,
        test_xml_dir=xml_dir,
        variant="gcc.debug",
        **arg_overrides,
    )
    exe = _exe_path(backend, source)
    cmd, framework = backend._test_command_for(source, exe)
    expected_xml = os.path.join(xml_dir, "gcc.debug", os.path.basename(exe) + ".xml")
    return cmd, exe, expected_xml, framework


def test_gtest_xml_argv_when_xml_dir_set(tmp_path):
    cmd, exe, expected_xml, _ = _xml_cmd(
        str(tmp_path), "/src/test_foo.cpp", ["/usr/include/gtest/gtest.h"]
    )
    assert cmd == [exe, f"--gtest_output=xml:{expected_xml}"]


def test_gtest_no_xml_argv_when_xml_dir_unset(tmp_path):
    tmpdir = str(tmp_path)
    source = "/src/test_foo.cpp"
    backend, _ = _make_backend(
        tmpdir,
        source=source,
        headers=["/usr/include/gtest/gtest.h"],
    )
    exe = _exe_path(backend, source)
    assert backend._test_command_for(source, exe) == ([exe], None)


def test_doctest_xml_argv_when_xml_dir_set(tmp_path):
    cmd, exe, expected_xml, _ = _xml_cmd(
        str(tmp_path), "/src/test_bar.cpp", ["/usr/include/doctest/doctest.h"]
    )
    assert cmd == [exe, "--reporters=junit", f"--out={expected_xml}"]


def test_catch2_xml_argv_when_xml_dir_set(tmp_path):
    cmd, exe, expected_xml, _ = _xml_cmd(
        str(tmp_path), "/src/test_baz.cpp", ["/usr/include/catch2/catch_all.hpp"]
    )
    assert cmd == [exe, "--reporter", "junit", "--out", expected_xml]


def test_testprefix_parts_prepended(tmp_path):
    cmd, exe, expected_xml, _ = _xml_cmd(
        str(tmp_path),
        "/src/test_foo.cpp",
        ["/usr/include/gtest/gtest.h"],
        TESTPREFIX="valgrind --error-exitcode=1",
    )
    assert cmd == ["valgrind", "--error-exitcode=1", exe, f"--gtest_output=xml:{expected_xml}"]


def test_no_framework_no_xml_argv_and_warns_at_verbose1(tmp_path, capsys):
    cmd, exe, _, _ = _xml_cmd(
        str(tmp_path),
        "/src/test_plain.cpp",
        ["/usr/include/something_else.h"],
        verbose=1,
    )
    assert cmd == [exe]
    assert "no known unit-test framework detected" in capsys.readouterr().err


def test_no_framework_no_warning_when_xml_dir_unset(tmp_path, capsys):
    source = "/src/test_plain.cpp"
    backend, _ = _make_backend(
        str(tmp_path),
        source=source,
        headers=["/usr/include/something_else.h"],
        verbose=1,
    )
    exe = _exe_path(backend, source)
    assert backend._test_command_for(source, exe) == ([exe], None)
    assert "no known unit-test framework detected" not in capsys.readouterr().err


def test_no_framework_no_warning_when_verbose0(tmp_path, capsys):
    cmd, exe, _, _ = _xml_cmd(
        str(tmp_path),
        "/src/test_plain.cpp",
        ["/usr/include/something_else.h"],
        verbose=0,
    )
    assert cmd == [exe]
    assert "no known unit-test framework detected" not in capsys.readouterr().err


def test_touch_result_marker_empty_path_is_noop(tmp_path):
    tmpdir = str(tmp_path)
    source = "/src/test_foo.cpp"
    backend, _ = _make_backend(
        tmpdir,
        source=source,
        headers=["/usr/include/gtest/gtest.h"],
    )
    # Empty path: no-op, no error raised.
    backend._touch_result_marker("")


def test_touch_result_marker_creates_file(tmp_path):
    tmpdir = str(tmp_path)
    source = "/src/test_foo.cpp"
    backend, _ = _make_backend(
        tmpdir,
        source=source,
        headers=["/usr/include/gtest/gtest.h"],
    )
    result_path = os.path.join(tmpdir, "test_foo.result")
    assert not os.path.exists(result_path)
    backend._touch_result_marker(result_path)
    assert os.path.exists(result_path)


def test_returns_detected_framework(tmp_path):
    """_test_command_for returns the detected TestFramework alongside the argv
    so _build_graph can pick the test rule's output (XML vs .result)."""
    cmd, exe, _, framework = _xml_cmd(
        str(tmp_path), "/src/test_foo.cpp", ["/usr/include/gtest/gtest.h"]
    )
    assert framework is not None
    assert cmd[0] == exe


def test_returns_none_framework_when_unknown(tmp_path):
    """An unknown header set yields ``(argv, None)``."""
    cmd, exe, _, framework = _xml_cmd(
        str(tmp_path), "/src/test_plain.cpp", ["/usr/include/something_else.h"]
    )
    assert framework is None
    assert cmd == [exe]
