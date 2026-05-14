"""End-to-end coverage of ``--test-xml-dir`` through ``ct-cake``.

Drives the full pipeline (parseargs -> Hunter -> backend.build_graph
-> compile -> link -> _run_tests) on the stub fixtures under
``examples-features/test_xml_output/``, then asserts ct-cake passed the right
framework-specific XML argv to each test executable.

The fixture binaries don't link against real GoogleTest / doctest --
their ``main()`` parses argv by hand and writes a stub JUnit-shaped
file when given the framework's XML flag. That keeps the test
self-contained: it verifies *ct-cake's* behaviour (passing the right
argv at the right moment) without requiring GoogleTest / doctest to
be installed on every CI runner.
"""

from __future__ import annotations

import os
import shutil

import compiletools.cake
import compiletools.testhelper as uth

# Skip the whole module if (a) the worktree's venv doesn't match this
# src tree (subprocess-driven helpers like ct-cas-publish would silently
# exercise the wrong compiletools install) or (b) ct-cas-publish itself
# isn't on PATH. ct-cake's link rule shells out to ct-cas-publish, so an
# unprovisioned venv breaks the build before _run_tests even starts; we'd
# then report "no XML produced" for the wrong reason.
pytestmark = uth.skipif_e2e_unavailable(
    lambda: shutil.which("ct-cas-publish") is not None,
    "ct-cas-publish not on PATH; run `uv pip install -e .` in this worktree",
)


def _setup_sample(tmpdir, source_relpath, extra_files=()):
    """Copy a sample source plus any extra header/aux files into tmpdir
    and return the absolute path of the copied source."""
    src_root = uth.example_path("test_xml_output")
    target = os.path.join(tmpdir, os.path.basename(source_relpath))
    shutil.copy2(os.path.join(src_root, source_relpath), target)
    for relpath in extra_files:
        dst = os.path.join(tmpdir, relpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src_root, relpath), dst)
    return target


def _make_config(tmpdir):
    """Mirror the test_cake.py setup: a temp config that points at
    whatever functional C++ compiler this host has, plus a ct.conf
    that anchors the variant name."""
    config_name = uth.create_temp_config(tmpdir)
    uth.create_temp_ct_conf(
        tempdir=tmpdir,
        defaultvariant=os.path.basename(config_name)[:-5],
    )
    return config_name


@uth.requires_functional_compiler
def test_gtest_fixture_emits_xml_under_test_xml_dir():
    """End-to-end: ct-cake compiles a stub gtest fixture, runs it with
    ``--test-xml-dir=DIR``, and the test process writes a JUnit XML
    file at ``DIR/<variant>/<exe>.xml`` because ct-cake passed
    ``--gtest_output=xml:<path>`` after exe_path."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(
            tmpdir,
            "test_stub_gtest.cpp",
            extra_files=("gtest/gtest.h",),
        )
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        uth.reset()
        compiletools.cake.main(
            [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + config_name,
                f"--tests={source}",
                f"--test-xml-dir={xml_dir}",
                "--no-auto",
                # File locking requires ct-lock-helper on PATH; disable so
                # the test runs in environments where the venv hasn't been
                # `uv pip install -e .`'d. The XML behaviour under test is
                # orthogonal to file locking.
                "--no-file-locking",
            ]
        )

        # The variant name is the config filename stem
        variant = os.path.basename(config_name)[:-5]
        expected_xml = os.path.join(xml_dir, variant, "test_stub_gtest.xml")
        assert os.path.exists(expected_xml), (
            f"missing {expected_xml}; ct-cake either skipped the test "
            f"or didn't pass --gtest_output. xml_dir contents: "
            f"{list(os.walk(xml_dir)) if os.path.isdir(xml_dir) else 'N/A'}"
        )
        with open(expected_xml) as f:
            contents = f.read()
        assert "<testsuites>" in contents


@uth.requires_functional_compiler
def test_doctest_fixture_emits_xml_with_doctest_argv():
    """Same as gtest, but for doctest's two-token argv shape
    (``--reporters=junit --out=PATH``)."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(
            tmpdir,
            "test_stub_doctest.cpp",
            extra_files=("doctest/doctest.h",),
        )
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        uth.reset()
        compiletools.cake.main(
            [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + config_name,
                f"--tests={source}",
                f"--test-xml-dir={xml_dir}",
                "--no-auto",
                # File locking requires ct-lock-helper on PATH; disable so
                # the test runs in environments where the venv hasn't been
                # `uv pip install -e .`'d. The XML behaviour under test is
                # orthogonal to file locking.
                "--no-file-locking",
            ]
        )

        variant = os.path.basename(config_name)[:-5]
        expected_xml = os.path.join(xml_dir, variant, "test_stub_doctest.xml")
        assert os.path.exists(expected_xml), f"missing {expected_xml}"
        with open(expected_xml) as f:
            contents = f.read()
        assert "<testsuites>" in contents


@uth.requires_functional_compiler
def test_unknown_framework_runs_without_xml_or_error():
    """A test source that includes nothing the detector recognises
    must run normally, produce no XML file, and not raise."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(tmpdir, "test_unknown_framework.cpp")
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        uth.reset()
        compiletools.cake.main(
            [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + config_name,
                f"--tests={source}",
                f"--test-xml-dir={xml_dir}",
                "--no-auto",
                # File locking requires ct-lock-helper on PATH; disable so
                # the test runs in environments where the venv hasn't been
                # `uv pip install -e .`'d. The XML behaviour under test is
                # orthogonal to file locking.
                "--no-file-locking",
            ]
        )

        variant = os.path.basename(config_name)[:-5]
        unexpected_xml = os.path.join(xml_dir, variant, "test_unknown_framework.xml")
        assert not os.path.exists(unexpected_xml), "unknown-framework test must NOT write XML"


@uth.requires_functional_compiler
def test_rerun_when_xml_deleted_between_runs():
    """Running ct-cake twice with --test-xml-dir set, deleting the XML
    file between runs, must re-emit the XML on the second run even
    though the .result marker is current."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(
            tmpdir,
            "test_stub_gtest.cpp",
            extra_files=("gtest/gtest.h",),
        )
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")
        variant = os.path.basename(config_name)[:-5]
        expected_xml = os.path.join(xml_dir, variant, "test_stub_gtest.xml")

        uth.reset()
        compiletools.cake.main(
            [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + config_name,
                f"--tests={source}",
                f"--test-xml-dir={xml_dir}",
                "--no-auto",
                # File locking requires ct-lock-helper on PATH; disable so
                # the test runs in environments where the venv hasn't been
                # `uv pip install -e .`'d. The XML behaviour under test is
                # orthogonal to file locking.
                "--no-file-locking",
            ]
        )
        assert os.path.exists(expected_xml)

        # Delete the XML file but leave the .result marker. A second
        # invocation must re-run the test (per the design's "Rerun-Skip
        # Integration" predicate) so the XML reappears.
        os.unlink(expected_xml)

        uth.reset()
        compiletools.cake.main(
            [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + config_name,
                f"--tests={source}",
                f"--test-xml-dir={xml_dir}",
                "--no-auto",
                # File locking requires ct-lock-helper on PATH; disable so
                # the test runs in environments where the venv hasn't been
                # `uv pip install -e .`'d. The XML behaviour under test is
                # orthogonal to file locking.
                "--no-file-locking",
            ]
        )
        assert os.path.exists(expected_xml), (
            "deleting the XML file between runs must trigger a re-run "
            "to regenerate it; .result alone is no longer enough"
        )
