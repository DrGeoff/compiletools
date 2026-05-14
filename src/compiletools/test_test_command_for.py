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
    TempDirContextNoChange,
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


def test_gtest_xml_argv_when_xml_dir_set():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_foo.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/gtest/gtest.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
        )
        exe = _exe_path(backend, source)
        cmd, _ = backend._test_command_for(source, exe)
        expected_xml = os.path.join(xml_dir, "gcc.debug", os.path.basename(exe) + ".xml")
        assert cmd == [exe, f"--gtest_output=xml:{expected_xml}"]


def test_gtest_no_xml_argv_when_xml_dir_unset():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_foo.cpp"
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/gtest/gtest.h"],
        )
        exe = _exe_path(backend, source)
        assert backend._test_command_for(source, exe) == ([exe], None)


def test_doctest_xml_argv_when_xml_dir_set():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_bar.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/doctest/doctest.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
        )
        exe = _exe_path(backend, source)
        cmd, _ = backend._test_command_for(source, exe)
        expected_xml = os.path.join(xml_dir, "gcc.debug", os.path.basename(exe) + ".xml")
        assert cmd == [exe, "--reporters=junit", f"--out={expected_xml}"]


def test_catch2_xml_argv_when_xml_dir_set():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_baz.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/catch2/catch_all.hpp"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
        )
        exe = _exe_path(backend, source)
        cmd, _ = backend._test_command_for(source, exe)
        expected_xml = os.path.join(xml_dir, "gcc.debug", os.path.basename(exe) + ".xml")
        assert cmd == [exe, "--reporter", "junit", "--out", expected_xml]


def test_testprefix_parts_prepended():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_foo.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/gtest/gtest.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
            TESTPREFIX="valgrind --error-exitcode=1",
        )
        exe = _exe_path(backend, source)
        cmd, _ = backend._test_command_for(source, exe)
        expected_xml = os.path.join(xml_dir, "gcc.debug", os.path.basename(exe) + ".xml")
        assert cmd == [
            "valgrind",
            "--error-exitcode=1",
            exe,
            f"--gtest_output=xml:{expected_xml}",
        ]


def test_no_framework_no_xml_argv_and_warns_at_verbose1(capsys):
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_plain.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/something_else.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
            verbose=1,
        )
        exe = _exe_path(backend, source)
        cmd, _ = backend._test_command_for(source, exe)
        assert cmd == [exe]
        captured = capsys.readouterr()
        assert "no known unit-test framework detected" in captured.err


def test_no_framework_no_warning_when_xml_dir_unset(capsys):
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_plain.cpp"
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/something_else.h"],
            verbose=1,
        )
        exe = _exe_path(backend, source)
        assert backend._test_command_for(source, exe) == ([exe], None)
        captured = capsys.readouterr()
        assert "no known unit-test framework detected" not in captured.err


def test_no_framework_no_warning_when_verbose0(capsys):
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_plain.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/something_else.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
            verbose=0,
        )
        exe = _exe_path(backend, source)
        assert backend._test_command_for(source, exe) == ([exe], None)
        captured = capsys.readouterr()
        assert "no known unit-test framework detected" not in captured.err


def test_touch_result_marker_empty_path_is_noop():
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_foo.cpp"
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/gtest/gtest.h"],
        )
        # Empty path: no-op, no error raised.
        backend._touch_result_marker("")


def test_touch_result_marker_creates_file():
    with TempDirContextNoChange() as tmpdir:
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


def test_returns_detected_framework():
    """_test_command_for returns the detected TestFramework alongside the argv
    so _build_graph can pick the test rule's output (XML vs .result)."""
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_foo.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/gtest/gtest.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
        )
        exe = _exe_path(backend, source)
        cmd, framework = backend._test_command_for(source, exe)
        assert framework is not None
        assert cmd[0] == exe


def test_returns_none_framework_when_unknown():
    """An unknown header set yields ``(argv, None)``."""
    with TempDirContextNoChange() as tmpdir:
        source = "/src/test_plain.cpp"
        xml_dir = os.path.join(tmpdir, "xml")
        backend, _ = _make_backend(
            tmpdir,
            source=source,
            headers=["/usr/include/something_else.h"],
            test_xml_dir=xml_dir,
            variant="gcc.debug",
        )
        exe = _exe_path(backend, source)
        cmd, framework = backend._test_command_for(source, exe)
        assert framework is None
        assert cmd == [exe]
