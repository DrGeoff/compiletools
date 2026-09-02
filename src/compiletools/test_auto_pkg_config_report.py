"""End-to-end tests for the ct-auto-pkg-config report tool (plan Stage 7).

The tool reuses the production magicflags pipeline with inference forced on
and reports the raw ``pkgconfig_infer`` verdicts through the
``MagicFlagsBase.last_inference_results`` hook. These tests pin the three
output styles (pretty / json / magic), the freeze round-trip (pasted
``//#PKG-CONFIG`` lines must parse to the identical flags under
``--no-auto-pkg-config``), the forcing of the feature over conf/env, and the
``--fail-on-ambiguous`` contract.

Fixtures: ``pkgconfig_env`` points PKG_CONFIG_PATH at the shared
examples-features/pkgs directory (autoinfer + fam fixtures); the autouse
conftest fixture keeps ``--auto-pkg-config`` off elsewhere, and the tool is
required to infer anyway (it prepends ``--auto-pkg-config`` to argv).
"""

import json
import os
import shutil

import pytest

import compiletools.examples_registry
import compiletools.magicflags
import compiletools.pkgconfig_infer
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.auto_pkg_config_report import main

TU_AUTOINFER = "#include <autoinfer/api.h>\nint main() { return 0; }\n"
TU_AMBIGUOUS = "#include <fam/x.h>\nint main() { return 0; }\n"


@pytest.fixture(autouse=True)
def _clear_auto_pkg_config_caches():
    """Isolation for the process-global caches the parse pipeline reads.

    Same contract as test_auto_pkg_config_magicflags: the PKG_CONFIG_PATH-keyed
    pkgconfig_infer memos and MagicFlagsBase's module-level caches/dedup set
    must not leak between tests.
    """
    compiletools.pkgconfig_infer.clear_cache()
    compiletools.magicflags.MagicFlagsBase.clear_cache()
    yield
    compiletools.pkgconfig_infer.clear_cache()
    compiletools.magicflags.MagicFlagsBase.clear_cache()


def _copy_fam_pc_dir(pkgconfig_env, pcdir):
    """Isolated PKG_CONFIG_PATH dir holding only the ambiguous family members.

    The shared pkgs directory also ships autoinfer-fam.pc, whose Name-based
    tie-break resolves fam/x.h; copying only -a/-b (plus the header tree they
    export) is what makes the verdict genuinely ambiguous.
    """
    pcdir.mkdir()
    for name in ("autoinfer-fam-a.pc", "autoinfer-fam-b.pc"):
        shutil.copy(os.path.join(pkgconfig_env, name), pcdir / name)
    shutil.copytree(os.path.join(pkgconfig_env, "autoinfer-fam-include"), pcdir / "autoinfer-fam-include")
    return str(pcdir)


class TestAutoPkgConfigReport(tb.BaseCompileToolsTestCase):
    """ct-auto-pkg-config run in-process over a tmp translation unit."""

    def _write_tu(self, text, name="main.cpp"):
        return str(uth.write_sources({name: text})[name])

    @staticmethod
    def _projection(result):
        return {str(k): [str(v) for v in values] for k, values in result.items()}

    # ------------------------------------------------------------- pretty

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_pretty_reports_resolved_header(self, capsys):
        """One line per header: <file>: <header> -> <pkg> (via <resolver>)."""
        tu = self._write_tu(TU_AUTOINFER)
        assert main([tu]) == 0
        out = capsys.readouterr().out
        assert f"{os.path.realpath(tu)}: autoinfer/api.h -> autoinfer (via pc-index)" in out

    @uth.requires_functional_compiler
    def test_pretty_reports_unresolved_header_with_reason(self, capsys, monkeypatch):
        """An include nothing on this host exports prints unresolved: <reason>."""
        monkeypatch.setenv("PKG_CONFIG_PATH", os.getcwd())
        monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
        tu = self._write_tu("#include <nosuchpkg/nonexistent.h>\nint main() { return 0; }\n")
        assert main([tu]) == 0
        out = capsys.readouterr().out
        assert f"{os.path.realpath(tu)}: nosuchpkg/nonexistent.h unresolved: " in out

    # --------------------------------------------------------------- json

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_json_is_schema_tagged_and_carries_the_verdict(self, capsys):
        """--style=json: json.loads-able payload with schema, the raw
        InferenceResult fields, and the header_path of the located header."""
        tu = self._write_tu(TU_AUTOINFER)
        assert main(["--style=json", tu]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == 1
        results = payload["files"][os.path.realpath(tu)]
        verdicts = {entry["header"]: entry for entry in results}
        entry = verdicts["autoinfer/api.h"]
        assert entry["package"] == "autoinfer"
        assert entry["resolver"] == "pc-index"
        assert entry["header_path"] is not None and entry["header_path"].endswith("autoinfer-include/autoinfer/api.h")
        assert entry["candidates"] == ["autoinfer"]
        assert entry["reason"] == ""

    # ------------------------------------------------- ambiguous + rc 1

    @uth.requires_functional_compiler
    def test_ambiguous_verdict_reported_and_fail_on_ambiguous_exits_1(
        self, pkgconfig_env, tmp_path, monkeypatch, capsys
    ):
        """fam/x.h against a pc dir holding only the -a/-b family members: the
        ambiguity is reported (json carries the reason, pretty prints it) and
        --fail-on-ambiguous turns it into exit code 1."""
        monkeypatch.setenv("PKG_CONFIG_PATH", _copy_fam_pc_dir(pkgconfig_env, tmp_path / "pc"))
        monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
        tu = self._write_tu(TU_AMBIGUOUS)

        assert main(["--style=json", tu]) == 0
        payload = json.loads(capsys.readouterr().out)
        results = payload["files"][os.path.realpath(tu)]
        assert len(results) == 1
        assert results[0]["package"] is None
        assert results[0]["reason"].startswith("ambiguous")
        assert set(results[0]["candidates"]) == {"autoinfer-fam-a", "autoinfer-fam-b"}

        assert main([tu]) == 0  # diagnostic exit code without the flag
        assert "unresolved:" in capsys.readouterr().out

        assert main(["--fail-on-ambiguous", tu]) == 1
        assert "unresolved:" in capsys.readouterr().out

    # -------------------------------------------------------------- magic

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_magic_emits_one_annotation_plus_comment_line(self, capsys):
        """--style=magic: a // <file> comment line then exactly one
        //#PKG-CONFIG=autoinfer for the single-inference TU; nothing else."""
        tu = self._write_tu(TU_AUTOINFER)
        assert main(["--style=magic", tu]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines == [f"// {os.path.realpath(tu)}", "//#PKG-CONFIG=autoinfer"]

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_magic_on_files_without_inferences_prints_nothing(self, monkeypatch, capsys):
        """A TU whose includes resolve to nothing produces no magic output."""
        monkeypatch.setenv("PKG_CONFIG_PATH", os.getcwd())
        monkeypatch.setenv("CT_DISTRO_BACKEND", "none")
        tu = self._write_tu("#include <nosuchpkg/nonexistent.h>\nint main() { return 0; }\n")
        assert main(["--style=magic", tu]) == 0
        assert capsys.readouterr().out == ""

    # ------------------------------------------------- the shipped example

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_magic_on_the_shipped_example_emits_exactly_autoinfer(self, capsys):
        """Plan exit criterion: --style magic over the registered
        auto_pkg_config example emits exactly //#PKG-CONFIG=autoinfer."""
        tu = compiletools.examples_registry.example_file("auto_pkg_config/main.cpp")
        assert main(["--style=magic", tu]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert [line for line in lines if line.startswith("//#PKG-CONFIG=")] == ["//#PKG-CONFIG=autoinfer"]

    # ------------------------------------------------------ freeze round-trip

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_magic_output_pasted_in_freezes_the_flags(self, capsys):
        """The freeze workflow: the magic-style lines pasted into a copy of
        the source make a --no-auto-pkg-config parse produce the identical
        flag slots as parsing the original with --auto-pkg-config."""
        tu = self._write_tu(TU_AUTOINFER)
        assert main(["--style=magic", tu]) == 0
        pkg_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("//#PKG-CONFIG=")]
        assert pkg_lines, "precondition: the report inferred at least one package"

        frozen = self._write_tu("".join(line + "\n" for line in pkg_lines) + TU_AUTOINFER, name="frozen.cpp")
        frozen_flags = self._projection(tb.create_magic_parser(["--no-auto-pkg-config"]).parse(frozen))
        original_flags = self._projection(tb.create_magic_parser(["--auto-pkg-config"]).parse(tu))
        assert frozen_flags == original_flags, "frozen TU must parse to the identical flags"

    # ------------------------------------------------------------ forcing

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_report_forces_inference_on_despite_env(self, monkeypatch, capsys):
        """CT_AUTO_PKG_CONFIG=0 (also the autouse test-suite default) must not
        be able to turn the report into a silent no-op: main prepends
        --auto-pkg-config to argv, and CLI beats env."""
        monkeypatch.setenv("CT_AUTO_PKG_CONFIG", "0")
        tu = self._write_tu(TU_AUTOINFER)
        assert main([tu]) == 0
        assert "-> autoinfer (via pc-index)" in capsys.readouterr().out
