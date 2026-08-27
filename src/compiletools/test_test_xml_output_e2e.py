"""End-to-end coverage of ``--test-xml-dir`` through ``ct-cake``.

Drives the full pipeline (parseargs -> Hunter -> backend.build_graph
-> compile -> link -> in-build test rules) on the stub fixtures under
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
# unprovisioned venv breaks the build before the test rules even run; we'd
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


def _run_cake(source, config_name, xml_dir, extra_argv=()):
    """Drive ct-cake on a single test source with ``--test-xml-dir``.

    ``--no-file-locking`` is set because ct-cake's link rule shells out to
    ct-lock-helper, which won't be on PATH in environments where the venv
    hasn't been ``uv pip install -e .``'d. The XML-emission behaviour under
    test is orthogonal to file locking.
    """
    uth.reset()
    compiletools.cake.main(
        [
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--config=" + config_name,
            f"--tests={source}",
            f"--test-xml-dir={xml_dir}",
            "--no-auto",
            "--no-file-locking",
            *extra_argv,
        ]
    )


def _variant_for(config_name):
    """Return the variant name encoded in the temp-config filename stem."""
    return os.path.basename(config_name)[:-5]


@uth.requires_functional_compiler
def test_gtest_fixture_emits_xml_under_test_xml_dir():
    """End-to-end: ct-cake compiles a stub gtest fixture, runs it with
    ``--test-xml-dir=DIR``, and the test process writes a JUnit XML
    file at ``DIR/<variant>/<exe>.xml`` because ct-cake passed
    ``--gtest_output=xml:<path>`` after exe_path."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(tmpdir, "test_stub_gtest.cpp", extra_files=("gtest/gtest.h",))
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir)

        expected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_stub_gtest.xml")
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
        source = _setup_sample(tmpdir, "test_stub_doctest.cpp", extra_files=("doctest/doctest.h",))
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir)

        expected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_stub_doctest.xml")
        assert os.path.exists(expected_xml), f"missing {expected_xml}"
        with open(expected_xml) as f:
            contents = f.read()
        assert "<testsuites>" in contents


@uth.requires_functional_compiler
def test_catch2_fixture_emits_xml_with_catch2_argv():
    """Same as gtest/doctest, but for Catch2's four-token argv shape
    (``--reporter junit --out PATH``)."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(tmpdir, "test_stub_catch2.cpp")
        # The registry keeps the stub header under a vendor-root extra
        # level (catch2/catch2/catch_all.hpp); stage it so the stub's
        # #include "catch2/catch_all.hpp" resolves from the source dir.
        os.makedirs(os.path.join(tmpdir, "catch2"), exist_ok=True)
        shutil.copy2(
            os.path.join(uth.example_path("test_xml_output"), "catch2", "catch2", "catch_all.hpp"),
            os.path.join(tmpdir, "catch2", "catch_all.hpp"),
        )
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir)

        expected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_stub_catch2.xml")
        assert os.path.exists(expected_xml), f"missing {expected_xml}"
        with open(expected_xml) as f:
            assert "<testsuites>" in f.read()


@uth.requires_functional_compiler
def test_unknown_framework_runs_without_xml_or_error():
    """A test source that includes nothing the detector recognises
    must run normally, produce no XML file, and not raise."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(tmpdir, "test_unknown_framework.cpp")
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir)

        unexpected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_unknown_framework.xml")
        assert not os.path.exists(unexpected_xml), "unknown-framework test must NOT write XML"


@uth.requires_functional_compiler
def test_gtest_reachable_only_via_isystem_is_detected():
    """The downstream "inert in this repo" shape: gtest lives behind
    ``-isystem`` (a system package), so DirectHeaderDeps -- which only
    walks ``-I``/CPATH dirs -- never resolves ``gtest/gtest.h`` and the
    resolved transitive header set carries no framework signal. Detection
    must fire from the source's raw ``#include`` token instead, and the
    XML must be emitted."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        source = _setup_sample(tmpdir, "test_stub_gtest.cpp")
        sysinc = os.path.join(tmpdir, "sysinc")
        os.makedirs(os.path.join(sysinc, "gtest"))
        shutil.copy2(
            os.path.join(uth.example_path("test_xml_output"), "gtest", "gtest.h"),
            os.path.join(sysinc, "gtest", "gtest.h"),
        )
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir, extra_argv=[f"--append-CXXFLAGS=-isystem {sysinc}"])

        expected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_stub_gtest.xml")
        assert os.path.exists(expected_xml), (
            f"missing {expected_xml}; detection ignored the raw #include token "
            f"and the -isystem-only gtest went undetected"
        )


# --- content-addressed XML: the CAS ``<cas_exe>.xml`` is the source of
# --- truth; the test-xml-dir path is a materialisation target. The stub
# --- appends a line to $CT_STUB_RUN_LOG per execution, giving these
# --- tests a run-count observable that separates "re-ran the test" from
# --- "re-materialised the report".


def _run_count(run_log):
    if not os.path.exists(run_log):
        return 0
    with open(run_log) as f:
        return sum(1 for _ in f)


@uth.requires_functional_compiler
def test_deleted_workspace_xml_rematerialized_without_rerun(monkeypatch):
    """Deleting the test-xml-dir report between runs must bring it back
    on the next build — via a copy from the content-addressed
    ``<cas_exe>.xml``, NOT by re-executing the test."""
    with uth.TempDirContext():
        tmpdir = os.getcwd()
        run_log = os.path.join(tmpdir, "stub-run.log")
        monkeypatch.setenv("CT_STUB_RUN_LOG", run_log)
        source = _setup_sample(tmpdir, "test_stub_gtest.cpp", extra_files=("gtest/gtest.h",))
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")
        expected_xml = os.path.join(xml_dir, _variant_for(config_name), "test_stub_gtest.xml")

        _run_cake(source, config_name, xml_dir)
        assert os.path.exists(expected_xml)
        assert _run_count(run_log) == 1

        os.unlink(expected_xml)

        _run_cake(source, config_name, xml_dir)
        assert os.path.exists(expected_xml), "deleted report was not re-materialised"
        assert _run_count(run_log) == 1, (
            "re-materialising a deleted report re-executed the test; the cas-side XML should have satisfied the rule"
        )


@uth.requires_functional_compiler
def test_missing_cas_xml_forces_rerun(monkeypatch):
    """The content-addressed XML is the rule's staleness key: evicting it
    (e.g. a cache trim) must re-run the test even though the .result
    marker AND the workspace report still exist."""
    import glob

    with uth.TempDirContext():
        tmpdir = os.getcwd()
        run_log = os.path.join(tmpdir, "stub-run.log")
        monkeypatch.setenv("CT_STUB_RUN_LOG", run_log)
        source = _setup_sample(tmpdir, "test_stub_gtest.cpp", extra_files=("gtest/gtest.h",))
        config_name = _make_config(tmpdir)
        xml_dir = os.path.join(tmpdir, "junit")

        _run_cake(source, config_name, xml_dir)
        assert _run_count(run_log) == 1

        cas_xmls = glob.glob(os.path.join(tmpdir, "cas-exedir", "**", "*.xml"), recursive=True)
        assert cas_xmls, "no content-addressed XML found under cas-exedir"
        for path in cas_xmls:
            os.unlink(path)

        _run_cake(source, config_name, xml_dir)
        assert _run_count(run_log) == 2, "missing cas-side XML did not force a re-run"
        assert glob.glob(os.path.join(tmpdir, "cas-exedir", "**", "*.xml"), recursive=True), (
            "re-run did not re-deposit the cas-side XML"
        )


@uth.requires_functional_compiler
def test_fresh_workspace_with_warm_cas_skips_test_run(monkeypatch):
    """The headline CI contract: workspace B, byte-identical to workspace
    A and sharing its cas dirs, must materialise the JUnit report from
    the CAS without re-running the test — before content-addressing,
    every fresh workspace re-ran every test against a 100% CAS hit."""
    with uth.TempDirContext():
        root = os.getcwd()
        shared = os.path.join(root, "shared-cas")
        run_log = os.path.join(root, "stub-run.log")
        monkeypatch.setenv("CT_STUB_RUN_LOG", run_log)
        extra = [
            f"--cas-exedir={os.path.join(shared, 'exe')}",
            f"--cas-objdir={os.path.join(shared, 'obj')}",
        ]
        src_root = uth.example_path("test_xml_output")
        for name in ("wsA", "wsB"):
            ws = os.path.join(root, name)
            os.makedirs(os.path.join(ws, "gtest"))
            shutil.copy2(os.path.join(src_root, "test_stub_gtest.cpp"), os.path.join(ws, "test_stub_gtest.cpp"))
            shutil.copy2(os.path.join(src_root, "gtest", "gtest.h"), os.path.join(ws, "gtest", "gtest.h"))
            # Both workspaces must resolve the SAME variant name (the
            # variant is folded into every cas cell path), so the config
            # filename is fixed rather than mkstemp-random.
            config_name = uth.create_temp_config(ws, filename=os.path.join(ws, "sharedvariant.conf"))
            uth.create_temp_ct_conf(tempdir=ws, defaultvariant="sharedvariant")
            xml_dir = os.path.join(ws, "junit")

            # Scoped chdir: the restore must happen while the tmp tree
            # still exists (TempDirContext deletes it before the
            # function-level monkeypatch teardown would run).
            with monkeypatch.context() as ws_ctx:
                ws_ctx.chdir(ws)
                _run_cake(os.path.join(ws, "test_stub_gtest.cpp"), config_name, xml_dir, extra_argv=extra)

            ws_xml = os.path.join(xml_dir, "sharedvariant", "test_stub_gtest.xml")
            assert os.path.exists(ws_xml), f"{name}: report not materialised"
            with open(ws_xml) as f:
                assert "<testsuites>" in f.read()

        assert _run_count(run_log) == 1, (
            "fresh workspace with a warm shared cas-exedir re-ran the test instead of materialising the cas-side XML"
        )
