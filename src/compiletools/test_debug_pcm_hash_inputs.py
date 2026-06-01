"""Tests for ``compiletools.debug_pcm_hash_inputs``.

The diagnostic must agree with the production path that computes BMI
cmd_hashes. Two complementary guarantees are tested here:

1. **Idempotence under no-change replay.** Two back-to-back invocations
   on the same fixture produce byte-identical JSON. This is the property
   the diagnostic is supposed to expose for the user's report flow: if
   their two real ct-cake invocations diverge, the diagnostic's two
   runs SHOULDN'T (because the bug, if any, is in the build pipeline
   feeding the hash inputs, not in the hash function itself).
2. **Drift guard against the production path.** The cmd_hash the
   diagnostic prints matches the value
   ``BuildBackend._compute_pcm_command_hash`` would compute for the
   same source under the same args. If they diverge, the diagnostic
   is lying about what the cache layer will do, and the user's
   bug-report diff would point at the wrong input.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

import compiletools.testhelper as uth
from compiletools import examples_registry as er


def _run(workdir, *extra_argv):
    """Invoke ct-debug-pcm-hash-inputs in a subprocess; return parsed JSON.

    Subprocess (not direct in-process call) so the diagnostic's full
    parseargs + headerdeps + magicflags + Hunter pipeline runs from a
    cold state on each call -- matching the "two ct-cake invocations"
    scenario the user is trying to reproduce.
    """
    cmd = [
        sys.executable,
        "-m",
        "compiletools.debug_pcm_hash_inputs",
        *extra_argv,
        "rounding.cppm",
    ]
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"ct-debug-pcm-hash-inputs failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return json.loads(result.stdout)


def _fixture_workdir(tmp_path, form: str):
    src = os.path.join(er.example_path("export_template_cmd_hash"), form)
    dst = tmp_path / form
    shutil.copytree(src, dst)
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    return dst


def test_output_has_expected_schema(tmp_path):
    """The diagnostic emits the six fields ``_pcm_command_hash`` consumes,
    plus the final cmd_hash and the constituent source/dep hashes."""
    workdir = _fixture_workdir(tmp_path, "form1")
    out = _run(workdir)
    expected = {
        "anchor_root",
        "cmd_hash",
        "compiler_identity",
        "cxx_command",
        "cxxflags_tokens",
        "dep_hash",
        "deplist",
        "magic_cpp_flags",
        "magic_cxx_flags",
        "source",
        "source_hash",
        "stage",
        "transitive_content_hash",
    }
    assert set(out.keys()) == expected
    assert out["stage"] in {"gcc_module_interface", "clang_module_interface"}
    # cmd_hash is 16 hex chars (the truncation _pcm_command_hash applies).
    assert isinstance(out["cmd_hash"], str) and len(out["cmd_hash"]) == 16
    assert all(c in "0123456789abcdef" for c in out["cmd_hash"])


@pytest.mark.parametrize("form", ["form1", "form2"])
def test_two_invocations_produce_identical_output(tmp_path, form):
    """Idempotence: the diagnostic on byte-identical source + identical
    args must return byte-identical JSON across two subprocess calls.

    This is the property the user diffs against when investigating a
    cmd_hash drift report; if THIS test ever fails, the diagnostic
    itself has acquired a non-determinism (test scope) — separate from
    the BMI-drift question.
    """
    workdir = _fixture_workdir(tmp_path, form)
    out1 = _run(workdir)
    out2 = _run(workdir)
    assert out1 == out2, (
        "Diagnostic is non-deterministic across two cold subprocess runs.\n"
        f"out1: {json.dumps(out1, indent=2, sort_keys=True)}\n"
        f"out2: {json.dumps(out2, indent=2, sort_keys=True)}"
    )


def test_form1_and_form2_have_different_cmd_hash(tmp_path):
    """The two forms have different source bytes (rounding.cppm differs)
    so they MUST hash to different cmd_hashes. Confirms the diagnostic
    actually distinguishes real input changes, not just shows the same
    hash for everything."""
    w1 = _fixture_workdir(tmp_path, "form1")
    w2 = _fixture_workdir(tmp_path, "form2")
    h1 = _run(w1)["cmd_hash"]
    h2 = _run(w2)["cmd_hash"]
    assert h1 != h2, f"Both forms hashed to {h1!r}; source-byte differences should produce different cmd_hashes."


def test_flag_change_changes_cmd_hash(tmp_path):
    """Adding a hash-relevant CXX flag (``-O3``) must change cmd_hash.
    Sanity-check that the diagnostic flows CXXFLAGS through the same
    pipeline the production hash uses."""
    workdir = _fixture_workdir(tmp_path, "form1")
    baseline = _run(workdir)["cmd_hash"]
    with_o3 = _run(workdir, "--append-CXXFLAGS=-O3")["cmd_hash"]
    assert baseline != with_o3, "Adding -O3 to CXXFLAGS should change cmd_hash."


def _gcc_modules_cxx() -> str | None:
    """Return the absolute path of a g++ on PATH that accepts
    ``-std=c++20 -fmodules-ts``, or None to skip."""
    from compiletools.test_cxx_modules import _detected_gcc_supports_modules, _which

    if not _detected_gcc_supports_modules():
        return None
    return _which("g++")


def test_diagnostic_matches_what_ct_cake_writes_to_disk(tmp_path, monkeypatch):
    """End-to-end drift guard: the cmd_hash the diagnostic prints
    equals the ``<cmd_hash>`` subdir name ct-cake actually creates
    under ``cas-pcmdir/<variant>/`` when it precompiles the same
    module.

    This is the strongest form of the contract: the diagnostic is
    only useful if its hash matches what the build system writes.
    Anything weaker (an in-process replica of the hash inputs) just
    re-tests Python's purity.
    """
    cxx = _gcc_modules_cxx()
    if cxx is None:
        pytest.skip("no g++ on PATH that supports -std=c++20 -fmodules-ts")

    workdir = _fixture_workdir(tmp_path, "form1")

    diag_env = os.environ.copy()
    diag_env.update({"CXX": cxx, "CPP": cxx, "LD": cxx})
    diag_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "compiletools.debug_pcm_hash_inputs",
            "rounding.cppm",
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=diag_env,
        timeout=60,
    )
    assert diag_proc.returncode == 0, f"diagnostic failed:\nstdout: {diag_proc.stdout}\nstderr: {diag_proc.stderr}"
    diag = json.loads(diag_proc.stdout)

    # Run ct-cake with the same compiler env, isolated cas dirs under
    # workdir, and let it precompile rounding.cppm. After the build,
    # cas-pcmdir/<variant>/<cmd_hash>/rounding.gcm exists; that
    # <cmd_hash> directory name is what the diagnostic should be
    # producing.
    monkeypatch.chdir(workdir)
    with uth.CompilerEnvContext(cxx):
        r = subprocess.run(
            ["ct-cake"],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=240,
        )
    assert r.returncode == 0, f"ct-cake failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"

    # The .gcm filename is derived from the module NAME (here
    # "myproj.util.rounding"), not the source filename ("rounding.cppm").
    gcm_files = list((workdir / "cas-pcmdir").rglob("myproj.util.rounding.gcm"))
    assert gcm_files, "ct-cake did not write myproj.util.rounding.gcm to cas-pcmdir; tree:\n" + "\n".join(
        str(p) for p in (workdir / "cas-pcmdir").rglob("*")
    )
    # Layout: <cas-pcmdir>/<variant>/<cmd_hash>/<module-name>.gcm
    on_disk_cmd_hash = os.path.basename(os.path.dirname(str(gcm_files[0])))
    assert diag["cmd_hash"] == on_disk_cmd_hash, (
        f"Diagnostic cmd_hash {diag['cmd_hash']!r} disagrees with the "
        f"<cmd_hash> directory ct-cake created on disk "
        f"({on_disk_cmd_hash!r}). _gather_inputs has drifted from "
        "_compute_pcm_command_hash; one side needs updating."
    )
