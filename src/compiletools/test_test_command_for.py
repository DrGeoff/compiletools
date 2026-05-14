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
        cmd = backend._test_command_for(exe)
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
        assert backend._test_command_for(exe) == [exe]


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
        cmd = backend._test_command_for(exe)
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
        cmd = backend._test_command_for(exe)
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
        cmd = backend._test_command_for(exe)
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
        cmd = backend._test_command_for(exe)
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
        assert backend._test_command_for(exe) == [exe]
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
        assert backend._test_command_for(exe) == [exe]
        captured = capsys.readouterr()
        assert "no known unit-test framework detected" not in captured.err


def test_framework_detection_cached_on_test_frameworks():
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
        backend._test_command_for(exe)
        assert exe in backend._test_frameworks
        assert backend._test_frameworks[exe] is not None
        # Second call must not re-run header_dependencies (cache hit).
        call_count = backend.hunter.header_dependencies.call_count
        backend._test_command_for(exe)
        assert backend.hunter.header_dependencies.call_count == call_count
