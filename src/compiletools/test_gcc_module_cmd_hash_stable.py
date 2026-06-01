"""Stability regression for the 2026-05-28 cmd_hash-drift bug report.

The report (`docs/.../2026-05-28-compiletools-bug3-export-template-cmd-hash-drift.md`)
claimed that a template-only named-module interface unit written with
per-declaration `export template <T>` (form 1) landed its gcc BMI at a
fresh `<cas-pcmdir>/<variant>/<cmd_hash>/` subdir on some back-to-back
ct-cake invocations, while the `export namespace { template <T> ... }`
rewrite (form 2) hashed stably across the same set of runs. The report
also claimed a downstream consumer-`.o` cascade in cas-objdir.

This test runs both forms through three back-to-back ct-cake subprocess
invocations and asserts:

* cas-pcmdir/<variant>/ contains exactly ONE <cmd_hash>/ subdir for the
  template module after all three runs. More than one means BMI cmd_hash
  drifted between invocations.
* cas-objdir contains the same set of .o filenames after each
  invocation (no growth). Growth means the consumer-`.o` cascade the
  report described.

Subprocesses run with varying `PYTHONHASHSEED` to catch any
dict/set-iteration-order non-determinism that could leak into the hash
inputs (the same hardening `test_noop_rebuild.py` applies for the
non-module case).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from compiletools import examples_registry as er

_CMD_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def _gcc_modules_cxx() -> str | None:
    from compiletools.test_cxx_modules import _detected_gcc_supports_modules, _which

    if not _detected_gcc_supports_modules():
        return None
    return _which("g++")


requires_gcc_modules = pytest.mark.skipif(
    _gcc_modules_cxx() is None,
    reason="No g++ on PATH that supports C++20 modules (-std=c++20 -fmodules-ts)",
)


def _fixture_workdir(tmp_path, form: str, name_suffix: str):
    src = os.path.join(er.example_path("export_template_cmd_hash"), form)
    dst = tmp_path / f"{form}-{name_suffix}"
    shutil.copytree(src, dst)
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    return dst


def _run_ct_cake(workdir, cxx: str, seed: int, *extra_argv: str) -> subprocess.CompletedProcess:
    """Invoke ct-cake in a subprocess. Varying PYTHONHASHSEED across
    calls catches set/dict-iteration non-determinism in the hash-input
    pipeline (the same hardening test_noop_rebuild.py uses)."""
    env = os.environ.copy()
    env.update({"CXX": cxx, "CPP": cxx, "LD": cxx, "PYTHONHASHSEED": str(seed)})
    cmd = ["ct-cake", *extra_argv]
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, env=env, timeout=240)


def _cmd_hash_subdirs(pcmdir_root) -> list[str]:
    """Return sorted list of <cmd_hash>/ subdirs under cas-pcmdir/<variant>/."""
    if not pcmdir_root.exists():
        return []
    out: list[str] = []
    for variant_dir in sorted(pcmdir_root.iterdir()):
        if not variant_dir.is_dir():
            continue
        for child in sorted(variant_dir.iterdir()):
            if child.is_dir() and _CMD_HASH_RE.match(child.name):
                out.append(child.name)
    return out


def _o_filenames(objdir_root) -> set[str]:
    """Return the set of *.o basenames under cas-objdir/.

    Basename only (no path) — the cas-objdir layout shards by
    file_hash[:2] so the same .o under different shards is still
    one entry; we want to detect "more .o files appeared", which
    full-path counting would inflate. The .o basename already
    encodes its triple-hash key, so distinct keys produce distinct
    basenames.
    """
    if not objdir_root.exists():
        return set()
    return {p.name for p in objdir_root.rglob("*.o")}


@requires_gcc_modules
@pytest.mark.parametrize("form", ["form1", "form2"])
@pytest.mark.parametrize(
    ("backend", "use_mtime_argv"),
    [
        pytest.param(None, [], id="default-cas-only"),
        pytest.param("make", ["--backend=make", "--use-mtime=True"], id="make-use-mtime"),
    ],
)
def test_gcc_module_cmd_hash_stable_across_invocations(tmp_path, monkeypatch, form, backend, use_mtime_argv):
    """Three back-to-back ct-cake invocations on a template-only gcc
    named-module interface unit must leave exactly ONE <cmd_hash>/
    subdir in cas-pcmdir/<variant>/ for that module.

    The bug report claimed form 1 produced 3 distinct subdirs over 8
    invocations while form 2 stayed at 1. If the report is reproducible
    on this toolchain, this test fails for form 1; if the report is not
    reproducible (source-code review suggested it isn't), this test
    passes for both forms.
    """
    cxx = _gcc_modules_cxx()
    assert cxx, "requires_gcc_modules guard should have skipped"

    backend_id = backend or "default"
    workdir = _fixture_workdir(tmp_path, form, f"cmdhash-{backend_id}")
    monkeypatch.chdir(workdir)

    seeds = [42, 999, 1337]
    for i, seed in enumerate(seeds):
        r = _run_ct_cake(workdir, cxx, seed, *use_mtime_argv)
        assert r.returncode == 0, (
            f"ct-cake invocation #{i + 1} (seed={seed}, form={form}, "
            f"backend={backend_id}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )

    subdirs = _cmd_hash_subdirs(workdir / "cas-pcmdir")
    assert len(subdirs) == 1, (
        f"Form {form!r} under backend {backend_id!r}: expected exactly 1 "
        f"<cmd_hash>/ subdir under cas-pcmdir/<variant>/ after 3 back-to-back "
        f"ct-cake invocations on byte-identical source, got {len(subdirs)}: "
        f"{subdirs}. This is the 2026-05-28 cmd_hash-drift symptom — run "
        "`ct-debug-pcm-hash-inputs rounding.cppm` before each ct-cake "
        "invocation and diff the JSON to identify which input drifted."
    )


@requires_gcc_modules
@pytest.mark.parametrize("form", ["form1", "form2"])
@pytest.mark.parametrize(
    ("backend", "use_mtime_argv"),
    [
        pytest.param(None, [], id="default-cas-only"),
        pytest.param("make", ["--backend=make", "--use-mtime=True"], id="make-use-mtime"),
    ],
)
def test_gcc_module_consumer_o_keys_stable_across_invocations(tmp_path, monkeypatch, form, backend, use_mtime_argv):
    """The set of .o filenames in cas-objdir must be byte-identical
    across three back-to-back ct-cake invocations on unchanged source.

    The bug report claimed form 1 caused 217 brand-new .o files in
    cas-objdir per touch-one-consumer cycle; this fixture only has 3
    consumers + 1 module so the worst-case growth is 4 new .o files
    per drift event — still trivial to detect as a set inequality.
    """
    cxx = _gcc_modules_cxx()
    assert cxx, "requires_gcc_modules guard should have skipped"

    backend_id = backend or "default"
    workdir = _fixture_workdir(tmp_path, form, f"okey-{backend_id}")
    monkeypatch.chdir(workdir)

    seeds = [42, 999, 1337]
    snapshots: list[set[str]] = []
    for i, seed in enumerate(seeds):
        r = _run_ct_cake(workdir, cxx, seed, *use_mtime_argv)
        assert r.returncode == 0, (
            f"ct-cake invocation #{i + 1} (seed={seed}, form={form}, "
            f"backend={backend_id}) failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )
        snapshots.append(_o_filenames(workdir / "cas-objdir"))

    assert snapshots[0] == snapshots[1] == snapshots[2], (
        f"Form {form!r} under backend {backend_id!r}: cas-objdir .o "
        f"filename set drifted across three back-to-back ct-cake runs on "
        f"unchanged source.\n"
        f"  run 1: {sorted(snapshots[0])}\n"
        f"  run 2 - run 1 (new):    {sorted(snapshots[1] - snapshots[0])}\n"
        f"  run 3 - run 1 (new):    {sorted(snapshots[2] - snapshots[0])}\n"
        f"  run 1 - run 3 (lost):   {sorted(snapshots[0] - snapshots[2])}\n"
        "This is the consumer-`.o` cascade described in the 2026-05-28 "
        "bug report; the BMI cmd_hash likely drifted between invocations "
        "(check cas-pcmdir/<variant>/ for >1 subdir)."
    )
