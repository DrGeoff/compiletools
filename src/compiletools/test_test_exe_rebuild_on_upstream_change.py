"""User-reported regression: editing an upstream H or C file must rebuild and
rerun the test executable that depends on it -- for every registered backend.

Pre-existing coverage:
* ``test_cake.py::test_*_edit_recompiles`` covers regular **executables**
  (main.cpp) via ``ct-cake`` (default = make backend only).
* ``test_makefile_backend.py::TestMakeRuntestsCasContract`` covers test exe
  re-run on header change for the **make backend only**, driving ``make``
  directly (not the ``ct-cake`` -> backend dispatch path).

This module adds the missing matrix: parameterise across every registered
backend (make / ninja / cmake / bazel / shake / slurm) and assert the
end-to-end ``ct-cake`` -> backend.execute('runtests') flow re-runs a test
when an upstream header or implied source changes.

The test driver invokes ``compiletools.cake.main`` -- the canonical user
entry point -- so a regression in ANY layer (headerdeps -> dep_hash -> object
key -> link key -> exe path -> runtests skip predicate) shows up here.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

import compiletools.cake
import compiletools.testhelper as uth
from compiletools.build_backend import available_backends, ensure_backends_registered

ensure_backends_registered()

# Only make and ninja consume the prereq list as a literal mtime comparison,
# so they are the only backends that honor ``--use-mtime=True``. Every other
# backend (bazel/cmake/shake/slurm) hard-fails on that flag — see
# ``build_backend.BuildBackend.__init__`` and the contract test in
# ``test_build_backend.py``. The use-mtime arm of this matrix is therefore
# only meaningful for these two; the rest are skipped (the hard-fail itself
# is asserted in test_build_backend.py, not re-derived per scenario here).
_MTIME_HONORING_BACKENDS = frozenset({"make", "ninja"})

# Heavy real-subprocess backends (slurm sbatch / bazel server) contend under
# ``-n auto`` and flake; bound their concurrency via sharded xdist load-groups
# (requires ``--dist loadgroup`` in pyproject addopts). Group names
# (``e2e-<backend>-<shard>``) and shard count match
# test_examples_end_to_end_cross_backend.py so the bound is GLOBAL across both
# heavy-e2e files. These ~5-per-backend cells are keyed on the backend alone, so
# each backend's rebuild cells land in one shard (joining that shard's
# cross-backend cells) -- negligible imbalance. Other backends are unmarked.
_HEAVY_E2E_BACKENDS = frozenset({"slurm", "bazel"})
_HEAVY_E2E_SHARDS = 8


def _heavy_e2e_group(backend_name: str) -> str:
    shard = int(hashlib.sha1(backend_name.encode()).hexdigest(), 16) % _HEAVY_E2E_SHARDS
    return f"e2e-{backend_name}-{shard}"


_BACKEND_PARAMS = [
    pytest.param(
        _b,
        marks=(pytest.mark.xdist_group(_heavy_e2e_group(_b)),) if _b in _HEAVY_E2E_BACKENDS else (),
    )
    for _b in available_backends()
]


# Each successful test invocation appends exactly one fixed-length record
# (the integer returned by val()) to the marker file. Strict equality on
# the marker length catches both under-execution (missed re-run) AND
# over-execution (ran N times when should have run once).
_RECORD_BYTES = 1
_MARKER_NAME = "ran.count"


# ---------------------------------------------------------------------------
# Source-tree fixtures
# ---------------------------------------------------------------------------
#
# Each ``_scenario_*`` function returns a mapping consumed by ``uth.write_sources``.
# Marker-emitting test sources use ``// ct-testmarker`` only as documentation;
# the file is wired in via ``--tests=<path>`` so findtargets is bypassed.


def _test_source(call_expr: str) -> str:
    """The standard marker-emitting ``main`` body. ``call_expr`` is C++ that
    yields an int (e.g. ``"val()"``); its value is appended to the marker
    file once per invocation. The literal ``{marker}`` placeholder is
    expanded by ``_materialise``.
    """
    return f"""\
        // ct-testmarker
        #include <fstream>
        #include "a.hpp"
        int main() {{
            std::ofstream f("{{marker}}", std::ios::app);
            f << {call_expr};
            return 0;
        }}
    """


def _scenario_single_header() -> dict[str, str]:
    """Test depends directly on one header that defines an inline function."""
    return {
        "a.hpp": "#pragma once\ninline int val() { return 1; }\n",
        "test_count.cpp": _test_source("val()"),
    }


def _scenario_implied_source() -> dict[str, str]:
    """Test depends on a header whose impl lives in an implied .cpp."""
    return {
        "a.hpp": "#pragma once\nint val();\n",
        "a.cpp": '#include "a.hpp"\nint val() { return 1; }\n',
        "test_count.cpp": _test_source("val()"),
    }


def _scenario_chain3_headers() -> dict[str, str]:
    """3-deep transitive header chain: test -> a.hpp -> b.hpp -> c.hpp.

    ``c.hpp`` defines the inline function; editing it must propagate
    through the transitive include set into the test exe's rebuild signal.
    """
    return {
        "a.hpp": '#pragma once\n#include "b.hpp"\n',
        "b.hpp": '#pragma once\n#include "c.hpp"\n',
        "c.hpp": "#pragma once\ninline int val() { return 1; }\n",
        "test_count.cpp": _test_source("val()"),
    }


def _scenario_chain3_implied_sources() -> dict[str, str]:
    """3-deep implied-source chain.

    Each foo.hpp implies foo.cpp; calls cascade a_val -> b_val -> c_val
    where c.cpp is the deepest implementation. Editing c.cpp does NOT
    touch any header so the bug surface is in the link layer (object
    file_hash + link key) rather than headerdeps / dep_hash.
    """
    return {
        "a.hpp": "#pragma once\nint a_val();\n",
        "a.cpp": '#include "a.hpp"\n#include "b.hpp"\nint a_val() { return b_val(); }\n',
        "b.hpp": "#pragma once\nint b_val();\n",
        "b.cpp": '#include "b.hpp"\n#include "c.hpp"\nint b_val() { return c_val(); }\n',
        "c.hpp": "#pragma once\nint c_val();\n",
        "c.cpp": '#include "c.hpp"\nint c_val() { return 1; }\n',
        "test_count.cpp": _test_source("a_val()"),
    }


def _materialise(workdir: pathlib.Path, mapping: dict[str, str]) -> tuple[dict[str, pathlib.Path], pathlib.Path]:
    """Render ``{path: content}`` into ``workdir`` (marker placeholder
    expanded), returning ``(paths, marker_path)``.
    """
    marker = workdir / _MARKER_NAME
    expanded = {rel: text.replace("{marker}", str(marker)) for rel, text in mapping.items()}
    paths = uth.write_sources(expanded, target_dir=str(workdir))
    return paths, marker


# ---------------------------------------------------------------------------
# Build / runtests harness
# ---------------------------------------------------------------------------


def _bump_mtime(*paths: pathlib.Path) -> None:
    """Force mtime advance past the next-second boundary so backends that
    gate on mtime (make / ninja in ``--use-mtime`` mode; cmake's own
    incremental tracking) detect an edit landing within the same second.
    Content is changed by the caller's ``write_text`` -- this purely lifts
    mtime above any FS whole-second granularity.
    """
    if not paths:
        return
    target = max(os.stat(p).st_mtime for p in paths) + 2
    for p in paths:
        os.utime(p, (target, target))


@dataclass(frozen=True)
class _RebuildHarness:
    """Bundles the per-test invariants so the body of each test reads as
    a sequence of edits + ``expect_marker_records`` assertions, with no
    repeated argument plumbing. Construct via :func:`_build_session`.
    """

    workdir: pathlib.Path
    backend_name: str
    source: pathlib.Path
    marker: pathlib.Path
    capped_parallel_argv: list
    capfd: pytest.CaptureFixture
    use_mtime: bool

    def _cake(self) -> None:
        """One ``cake.main`` invocation with the harness's pinned config.
        Wraps the bazel env-error skip so individual tests don't repeat it.
        """
        with uth.TempConfigContext(tempdir=str(self.workdir)) as cfg:
            uth.create_temp_ct_conf(tempdir=str(self.workdir))
            argv = [
                f"--config={cfg}",
                f"--backend={self.backend_name}",
                f"--use-mtime={'True' if self.use_mtime else 'False'}",
                "--no-file-locking",
                "--no-compilation-database",
                "--quiet",
                # Serial test runner: parallel test execution would race
                # the marker-append on multi-test scenarios; immaterial
                # here, but the determinism is worth keeping.
                "--serialise-tests",
                "--tests",
                str(self.source),
                *self.capped_parallel_argv,
            ]
            uth.reset()
            with uth.ParserContext():
                try:
                    compiletools.cake.main(argv)
                except Exception:
                    if self.backend_name == "bazel":
                        uth.skip_if_bazel_env_error(self.capfd.readouterr().err)
                    raise

    def expect_marker_records(self, expected_records: int, label: str) -> None:
        """Build + runtests, then assert the marker length equals
        ``expected_records * _RECORD_BYTES``. Both under-execution
        (missed rerun) and over-execution (ran N times) fail the equality.
        """
        self._cake()
        actual = len(self.marker.read_text()) if self.marker.exists() else 0
        expected = expected_records * _RECORD_BYTES
        contents = self.marker.read_text() if self.marker.exists() else "<missing>"
        assert actual == expected, (
            f"{self.backend_name}/use_mtime={self.use_mtime}: marker length "
            f"{actual} != expected {expected} after {label!r}. "
            f"Marker content: {contents!r}"
        )

    def marker_records(self) -> int:
        """Build + runtests, then return marker length in records (no
        equality assertion). Used by tests that range-check the result
        instead of pinning to one value (e.g. round-trip).
        """
        self._cake()
        return len(self.marker.read_text()) // _RECORD_BYTES if self.marker.exists() else 0


@contextlib.contextmanager
def _build_session(
    backend_name: str,
    tmp_path,
    monkeypatch,
    capfd,
    capped_parallel_argv,
    scenario: dict[str, str],
    *,
    use_mtime: bool = False,
) -> Iterator[tuple[_RebuildHarness, dict[str, pathlib.Path]]]:
    """Bracket every test with a clean parser/cache state, materialise the
    chosen scenario into a workdir suitable for the backend (shared FS for
    slurm, fallback for the rest), and yield a fully-wired harness plus
    the materialised path map.
    """
    if use_mtime and backend_name not in _MTIME_HONORING_BACKENDS:
        pytest.skip(
            f"--use-mtime=True is unsupported on the {backend_name!r} backend "
            "(hard-fails; only make/ninja honor it — see test_build_backend.py)"
        )
    uth.reset()
    try:
        with uth.shared_filesystem_tmpdir(backend_name, tmp_path) as workdir:
            workdir = pathlib.Path(workdir)
            monkeypatch.chdir(workdir)
            paths, marker = _materialise(workdir, scenario)
            harness = _RebuildHarness(
                workdir=workdir,
                backend_name=backend_name,
                source=paths["test_count.cpp"],
                marker=marker,
                capped_parallel_argv=capped_parallel_argv,
                capfd=capfd,
                use_mtime=use_mtime,
            )
            yield harness, paths
    finally:
        uth.reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_mtime", [False, True], ids=["cas-only", "use-mtime"])
@pytest.mark.parametrize("backend_name", _BACKEND_PARAMS)
@uth.requires_functional_compiler
@uth.requires_backend_tool()
def test_test_exe_reruns_when_upstream_header_changes(
    backend_name, use_mtime, tmp_path, monkeypatch, capfd, capped_parallel_argv
):
    """Edit an included header in-place; the test must execute again.

    Build v1 -> marker = 1; edit header to v2, build -> marker = 2.
    A short marker on the second build means the test exe was either NOT
    re-linked (link key missed the new header content) OR was re-linked
    but the runtests skip predicate falsely treated the previous .result
    as valid for the new exe.

    Parameterised over ``use_mtime`` because the two modes go through
    different skip-predicate code paths (CAS-only marker presence vs.
    legacy ``mtime(result) >= mtime(exe)``).
    """
    with _build_session(
        backend_name,
        tmp_path,
        monkeypatch,
        capfd,
        capped_parallel_argv,
        _scenario_single_header(),
        use_mtime=use_mtime,
    ) as (h, paths):
        h.expect_marker_records(1, "initial v1 build")

        paths["a.hpp"].write_text("#pragma once\ninline int val() { return 2; }\n")
        _bump_mtime(paths["a.hpp"], paths["test_count.cpp"])
        h.expect_marker_records(2, "upstream-header edit (v1 -> v2)")


@pytest.mark.parametrize("use_mtime", [False, True], ids=["cas-only", "use-mtime"])
@pytest.mark.parametrize("backend_name", _BACKEND_PARAMS)
@uth.requires_functional_compiler
@uth.requires_backend_tool()
def test_test_exe_reruns_when_upstream_implied_source_changes(
    backend_name, use_mtime, tmp_path, monkeypatch, capfd, capped_parallel_argv
):
    """Edit ``a.cpp`` (implied from ``a.hpp``); the test must execute again.

    The test only includes ``a.hpp``; ``a.cpp`` is pulled in via the
    implied-source mechanism. Editing it changes the linked behaviour
    even though the test's own source tree is byte-identical.
    """
    with _build_session(
        backend_name,
        tmp_path,
        monkeypatch,
        capfd,
        capped_parallel_argv,
        _scenario_implied_source(),
        use_mtime=use_mtime,
    ) as (h, paths):
        h.expect_marker_records(1, "initial v1 build")

        paths["a.cpp"].write_text('#include "a.hpp"\nint val() { return 2; }\n')
        _bump_mtime(paths["a.cpp"])
        h.expect_marker_records(2, "upstream-impl edit (v1 -> v2)")


@pytest.mark.parametrize("use_mtime", [False, True], ids=["cas-only", "use-mtime"])
@pytest.mark.parametrize("backend_name", _BACKEND_PARAMS)
@uth.requires_functional_compiler
@uth.requires_backend_tool()
def test_test_exe_reruns_when_3_deep_upstream_header_changes(
    backend_name, use_mtime, tmp_path, monkeypatch, capfd, capped_parallel_argv
):
    """Edit the deepest header in a 3-deep transitive include chain.

    Layout: ``test_count.cpp -> a.hpp -> b.hpp -> c.hpp`` (deepest).
    A failure here would localise the bug to either ``headerdeps``
    (transitive walk dropped a level), ``compute_dep_hash`` (didn't
    fold in the deep header content), or the namer (didn't surface
    the new ``dep_hash`` into the object path).
    """
    with _build_session(
        backend_name,
        tmp_path,
        monkeypatch,
        capfd,
        capped_parallel_argv,
        _scenario_chain3_headers(),
        use_mtime=use_mtime,
    ) as (h, paths):
        h.expect_marker_records(1, "initial v1 build")

        paths["c.hpp"].write_text("#pragma once\ninline int val() { return 2; }\n")
        _bump_mtime(paths["c.hpp"], paths["test_count.cpp"])
        h.expect_marker_records(2, "3-deep header edit (c.hpp v1 -> v2)")


@pytest.mark.parametrize("use_mtime", [False, True], ids=["cas-only", "use-mtime"])
@pytest.mark.parametrize("backend_name", _BACKEND_PARAMS)
@uth.requires_functional_compiler
@uth.requires_backend_tool()
def test_test_exe_reruns_when_3_deep_upstream_implied_source_changes(
    backend_name, use_mtime, tmp_path, monkeypatch, capfd, capped_parallel_argv
):
    """Edit the deepest .cpp in a 3-deep implied-source chain.

    Layout: ``a.hpp/a.cpp -> b.hpp/b.cpp -> c.hpp/c.cpp`` (deepest).
    Headers are unchanged; only ``c.cpp``'s body shifts. The bug surface
    is in the link layer:
      * implied-source discovery (c.cpp not actually walked / linked), or
      * the per-file file_hash component of the .o cache key (didn't
        change despite a content-only edit), or
      * the link key (didn't pick up the new c.o path).
    """
    with _build_session(
        backend_name,
        tmp_path,
        monkeypatch,
        capfd,
        capped_parallel_argv,
        _scenario_chain3_implied_sources(),
        use_mtime=use_mtime,
    ) as (h, paths):
        h.expect_marker_records(1, "initial v1 build")

        paths["c.cpp"].write_text('#include "c.hpp"\nint c_val() { return 2; }\n')
        _bump_mtime(paths["c.cpp"])
        h.expect_marker_records(2, "3-deep impl edit (c.cpp v1 -> v2)")


@pytest.mark.parametrize("backend_name", _BACKEND_PARAMS)
@uth.requires_functional_compiler
@uth.requires_backend_tool()
def test_round_trip_v1_v2_v1_does_not_double_run_v1(backend_name, tmp_path, monkeypatch, capfd, capped_parallel_argv):
    """Round-trip: edit v1 -> v2 -> back to v1 (byte-identical to step 1).

    In CAS-only mode the third build hits the v1 cas-exedir entry and its
    sibling ``<cas_v1>.result`` from step 1 is still present, so the test
    must NOT be re-run -- the bytes have already been tested. A re-run here
    indicates the cas-exedir hard-link mtime confusion that commit 74a32d64
    fixed (for make) is leaking back via a non-make backend.

    cmake/bazel use their own build cache (no cas-exedir hard-link issue)
    and may legitimately re-run in step 3 -- assertion is relaxed there
    to "no double-run", i.e. ``records in {2, 3}``.
    """
    with _build_session(
        backend_name,
        tmp_path,
        monkeypatch,
        capfd,
        capped_parallel_argv,
        _scenario_single_header(),
        use_mtime=False,
    ) as (h, paths):
        h.expect_marker_records(1, "step 1: v1 initial")

        paths["a.hpp"].write_text("#pragma once\ninline int val() { return 2; }\n")
        _bump_mtime(paths["a.hpp"], paths["test_count.cpp"])
        h.expect_marker_records(2, "step 2: v2 edit")

        # Step 3: revert to v1 (byte-identical to step 1).
        paths["a.hpp"].write_text("#pragma once\ninline int val() { return 1; }\n")
        _bump_mtime(paths["a.hpp"], paths["test_count.cpp"])
        records = h.marker_records()

        if backend_name in ("cmake", "bazel"):
            assert records in (2, 3), (
                f"{backend_name}: step-3 round-trip records={records}; expected 2 "
                f"(skipped via cache) or 3 (re-run once). A larger value "
                f"indicates an over-execution bug."
            )
        else:
            assert records == 2, (
                f"{backend_name}: step-3 round-trip re-ran a previously-"
                f"tested cas-exedir hit: 2 -> {records} (expected 2). "
                f"The v1 exe's bytes were tested in step 1 and "
                f"``<cas_v1>.result`` should still be present, so the "
                f"runtests skip predicate should treat the test as up-to-date."
            )
