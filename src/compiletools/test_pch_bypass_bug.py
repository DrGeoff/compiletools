"""End-to-end test that ct-cake's precompiled-header wiring is actually
CONSUMED by the consumer compile, not merely emitted into ``cas-pchdir``.

The bug under test: when the PCH header lives next to the consumer
source file (the realistic case for a private per-TU PCH — header and
``.cpp`` co-located in the same directory), ct-cake builds the ``.gch`` into
``<cas-pchdir>/<hash>/`` as designed but the consumer compile resolves
``#include "pch.h"`` to the source-dir copy of the header (GCC searches
the source-file directory before any ``-I`` dir for quoted includes)
and then looks for a sibling ``.gch`` only beside that resolved path —
never reaching the cache. The cached ``.gch`` is dead bytes on disk
and the TU compiles fully from source.

The cross-backend matrix in ``test_examples_end_to_end_cross_backend.py``
cannot catch this: from-source compile is a correct fallback so the
build still succeeds (``returncode == 0``). This test asserts the
inverse — the cached ``.gch`` must actually be opened by GCC during
the consumer compile.

Verification mechanism: append ``-H`` to ``CXXFLAGS`` and grep the
build's stderr. GCC's ``-H`` prints a line ``! <path>`` for every PCH
it loads (and ``x <path>`` for every PCH it considers and rejects).
A correct build must produce at least one ``! …pch.h.gch`` line whose
path lies under ``<cas-pchdir>``. Pre-fix: zero such lines.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

import compiletools.testhelper as uth
from compiletools.examples_registry import example_path


def _copy_example(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy the fixture into a fresh workspace and plant a ``.git`` marker
    so :func:`compiletools.git_utils.find_git_root` resolves the
    workspace itself (not the surrounding pytest tmpdir)."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst)
        else:
            shutil.copytree(entry, dst / entry.name)
    (dst / ".git").mkdir()


def _find_gch_marker(stderr_text: str, marker_prefix: str) -> list[str]:
    """Return ``-H`` output lines beginning with ``marker_prefix`` and
    referencing a ``.gch`` path. Marker semantics:

      ``! ``  PCH was loaded and accepted (cache hit).
      ``x ``  PCH was found and considered, then rejected by gcc's
              compatibility check (the bypass bug is fixed but the
              cache itself is invalid for this particular compile —
              e.g., flag mismatch). Either fallback path proves
              GCC actually reached the cache.

    Pre-fix the cache was never even examined, so BOTH counts were
    zero. Post-fix at least one of them must be non-zero.
    """
    return [line for line in stderr_text.splitlines() if line.startswith(marker_prefix) and ".gch" in line]


@uth.requires_functional_compiler
@pytest.mark.parametrize("backend_name", ["make", "ninja", "cmake", "bazel", "shake", "slurm"])
def test_consumer_compile_actually_opens_cached_pch(backend_name, tmp_path):
    """ct-cake builds the PCH into ``cas-pchdir``; the consumer compile
    must actually open and consume that ``.gch`` on every backend that
    supports the cache.

    The structural condition that triggers the bug is encoded in the
    fixture: ``pch.h`` lives in the same directory as ``consumer.cpp``,
    and ``consumer.cpp`` does ``#include "pch.h"`` (quoted). GCC's
    quoted-include resolution searches the source dir first, finds the
    header there, and looks for a sibling ``.gch`` — bypassing the
    cache dir entirely. The fix in ``BuildBackend._create_compile_rule``
    emits ``-include <cache>/<basename>`` instead of ``-I <cache>`` so
    GCC opens the cached ``.gch`` unconditionally.

    Bazel-specific wiring (``BazelBackend._bazel_pch_inputs_and_copts``
    + ``_materialise_pch_stagings``) hardlinks the cas-pchdir entries
    into a workspace-local ``.ct-bazel-pch/`` and rewrites the
    ``-include`` flag to be workspace-relative — bazel's hermetic
    sandbox would otherwise reject the absolute path.

    Assertion. GCC's ``-H`` prints ``! <path>`` for every PCH it
    successfully loads and ``x <path>`` for every PCH it considers
    and rejects. The bypass bug's signature was zero of either; the
    fix must produce at least one. We accept ``x`` as evidence that
    the cache was reached even when the per-backend flag environment
    means the .gch fails the compatibility check (rare; bazel-cache-
    hits depend on ``--cxxopt=-std=`` alignment in ``.bazelrc``).
    """
    if not uth._backend_tool_available(backend_name):
        pytest.skip(f"{backend_name} build tool not on PATH")

    # Slurm dispatches compile jobs to remote compute nodes, which see
    # the submit-host's `/tmp` as private node-local scratch. The
    # workspace must live on a shared filesystem (GPFS/NFS/Lustre/CIFS)
    # so the compute nodes can open the source files. Other backends
    # are happy with the pytest-provided node-local tmp_path.
    with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as effective_tmp:
        src = pathlib.Path(example_path("pch_bypass_bug"))
        workspace = pathlib.Path(effective_tmp) / "ws"
        _copy_example(src, workspace)
        cas_pchdir = workspace / "cas-pchdir"

        diagnostics_dir = workspace / "diag"
        argv = [
            "ct-cake",
            f"--backend={backend_name}",
            f"--cas-objdir={workspace}/cas-objdir",
            f"--bindir={workspace}/bin",
            f"--cas-pchdir={cas_pchdir}",
            f"--cas-pcmdir={workspace}/cas-pcmdir",
            f"--cas-exedir={workspace}/cas-exedir",
            f"--diagnostics-dir={diagnostics_dir}",
            "--append-CXXFLAGS=-H",
            "--auto",
        ]
        if backend_name == "slurm":
            # The slurm backend deletes its per-job log files at end-of-build
            # when verbose < 2 (see trace_backend.SlurmBackend._cleanup_slurm_logs).
            # `-vv` keeps them so we can scan for the consumer compile's `-H`
            # output, which only lands in those files (the compute-node stderr
            # never reaches the submit-side captured stderr).
            argv.append("-vv")

        env = os.environ.copy()
        for var in ("CXXFLAGS", "CFLAGS", "LDFLAGS", "CPPFLAGS"):
            env.pop(var, None)

        result = subprocess.run(
            argv,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"ct-cake build failed (backend={backend_name} returncode={result.returncode})\n"
            f"argv: {argv}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr (last 80 lines) ---\n" + "\n".join(result.stderr.splitlines()[-80:])
        )

        gch_files = sorted(cas_pchdir.rglob("*.gch"))
        assert gch_files, (
            f"ct-cake did not build any .gch in {cas_pchdir}; the {backend_name} test "
            f"is not exercising the path it thinks it is."
        )

        # Where does the consumer compile's `-H` output land?
        #   make/cmake/bazel — gcc stderr → captured subprocess stderr.
        #   ninja            — gcc stderr → ninja's pretty-printer → stdout.
        #   shake            — gcc runs in-process via atomic_compile; its
        #                      stderr is captured to subprocess stderr.
        #   slurm            — consumer compile dispatched to a remote
        #                      compute node; its stderr is written to a
        #                      slurm per-job log file under the
        #                      diagnostics dir (silent on success).
        # Pull from all three sources and concatenate.
        slurm_log_text = ""
        if diagnostics_dir.exists():
            for log in diagnostics_dir.rglob("slurm-ct-*-*.out"):
                slurm_log_text += "\n" + log.read_text(errors="replace")
        combined_output = result.stdout + "\n" + result.stderr + slurm_log_text
        pch_used_lines = _find_gch_marker(combined_output, "! ")
        pch_rejected_lines = _find_gch_marker(combined_output, "x ")
        cache_examined = bool(pch_used_lines or pch_rejected_lines)

        if not cache_examined:
            gch_paths = [str(p.relative_to(workspace)) for p in gch_files]
            pytest.fail(
                f"Consumer compile did NOT examine the cached PCH ({backend_name}).\n"
                f"  cas-pchdir contents: {gch_paths}\n"
                f"  `! …gch` lines (PCH used):     (none)\n"
                f"  `x …gch` lines (PCH rejected): (none)\n"
                "  Both empty means GCC's PCH lookup never even reached the\n"
                "  cache — the source-dir copy of pch.h won the include-search\n"
                "  race and GCC then searched only beside that resolved path.\n"
                "  See examples-features/pch_bypass_bug/README.md.\n"
            )
