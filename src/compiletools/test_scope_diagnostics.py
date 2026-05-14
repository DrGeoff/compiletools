"""Tests for the ``--scope-diagnostics`` flag (Task F).

When ``--scope-diagnostics`` is enabled and ``resolve_diagnostics_dir``
yields a path, ``Hunter.macro_state_hash`` writes a per-TU JSON sidecar
under ``<diagnostics-dir>/<invocation-id>/scope/`` listing which cmdline
``-D`` macros were included vs excluded from the TU's hash.

Off by default; piggy-backs on the existing diagnostics-dir
infrastructure; silently skips the write when no diagnostics dir is
resolvable so callers without ``--bindir`` / ``--diagnostics-dir`` set
don't crash.
"""

from __future__ import annotations

import json
import os

import configargparse
import pytest

import compiletools.apptools
import compiletools.diagnostics
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _reset_diagnostics_cache():
    compiletools.diagnostics._reset_for_tests()
    uth.reset()
    yield
    compiletools.diagnostics._reset_for_tests()
    uth.reset()


def _make_hunter(extra_args, temp_config):
    """Build a fresh Hunter wired up with its own BuildContext.

    Mirrors the helper in test_hunter_cache_scoping.py.
    """
    argv = ["-c", temp_config, "--include", uth.ctdir()] + list(extra_args)
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.hunter.add_arguments(cap)
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)
    return hntr


def _process(hunter, sample_path):
    """Drive the hunter's magicflags pipeline so macro_state_hash is callable."""
    try:
        hunter.magicflags(sample_path)
    except RuntimeError as e:
        if "No functional C++ compiler detected" in str(e):
            pytest.skip("No functional C++ compiler detected")
        raise


def _sample(rel):
    return uth.example_file(f"cache_scoping/{rel}")


def _scope_dir(diag_root):
    """The per-invocation scope subdir under a diagnostics-dir root."""
    iid = compiletools.diagnostics.invocation_id()
    return os.path.join(str(diag_root), iid, "scope")


def test_scope_diagnostics_off_by_default(tmp_path):
    """Without ``--scope-diagnostics``, no scope/ subdir is written even
    when a diagnostics-dir is resolvable and macro_state_hash runs."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        # Make a diagnostics dir resolvable but leave --scope-diagnostics off.
        hntr.args.diagnostics_dir = str(tmp_path)
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        hntr.macro_state_hash(sample, dep_hash="0" * 16)

        scope_dir = _scope_dir(tmp_path)
        assert not os.path.exists(scope_dir), (
            f"scope dir must not be created when --scope-diagnostics is off, but found: {scope_dir}"
        )


def test_scope_diagnostics_writes_json_when_on(tmp_path):
    """With ``--scope-diagnostics`` on and a diagnostics-dir resolvable,
    a JSON sidecar lands at <diag>/<iid>/scope/<basename>.<dep_hash>.json."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        dep_hash = "0" * 16
        hntr.macro_state_hash(sample, dep_hash=dep_hash)

        expected = os.path.join(
            _scope_dir(tmp_path),
            f"{os.path.basename(sample)}.{dep_hash}.json",
        )
        assert os.path.isfile(expected), f"expected scope JSON at {expected}"


def test_scope_diagnostics_payload_has_required_keys(tmp_path):
    """The JSON payload must carry the keys downstream consumers rely on."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        dep_hash = "0" * 16
        hntr.macro_state_hash(sample, dep_hash=dep_hash)

        payload_path = os.path.join(
            _scope_dir(tmp_path),
            f"{os.path.basename(sample)}.{dep_hash}.json",
        )
        with open(payload_path) as f:
            payload = json.load(f)

        for key in (
            "tu",
            "dep_hash",
            "cmdline_d_macros_total",
            "cmdline_d_macros_in_hash",
            "cmdline_d_macros_excluded",
        ):
            assert key in payload, f"missing key {key!r} in {payload}"


def test_scope_diagnostics_lists_included_excluded_correctly(tmp_path):
    """Build with two cmdline -D macros; only one is referenced by the TU.

    The JSON must list the referenced macro under ``cmdline_d_macros_in_hash``
    and the unreferenced one under ``cmdline_d_macros_excluded``.
    """
    with uth.TempConfigContext() as temp_config:
        # with_ref.cpp references APP_NAME but not UNUSED_NAME.
        hntr = _make_hunter(
            ["--append-CPPFLAGS=-DAPP_NAME=A -DUNUSED_NAME=Z"],
            temp_config,
        )
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)

        dep_hash = "0" * 16
        hntr.macro_state_hash(sample, dep_hash=dep_hash)

        payload_path = os.path.join(
            _scope_dir(tmp_path),
            f"{os.path.basename(sample)}.{dep_hash}.json",
        )
        with open(payload_path) as f:
            payload = json.load(f)

        assert "APP_NAME" in payload["cmdline_d_macros_in_hash"], (
            f"APP_NAME should be included (TU references it); got payload={payload}"
        )
        assert "UNUSED_NAME" in payload["cmdline_d_macros_excluded"], (
            f"UNUSED_NAME should be excluded (TU does not reference it); got payload={payload}"
        )
        # Sorting invariant for deterministic diffs.
        assert payload["cmdline_d_macros_in_hash"] == sorted(payload["cmdline_d_macros_in_hash"])
        assert payload["cmdline_d_macros_excluded"] == sorted(payload["cmdline_d_macros_excluded"])


def test_scope_diagnostics_skipped_when_no_diagnostics_dir_resolvable(tmp_path):
    """If neither --diagnostics-dir nor --bindir is set, the diagnostic
    write must be silently skipped (resolve_diagnostics_dir raises
    RuntimeError) -- macro_state_hash must NOT crash."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.scope_diagnostics = True
        # Force both to be unresolvable.
        hntr.args.diagnostics_dir = None
        hntr.args.bindir = None
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        # Should not raise.
        result = hntr.macro_state_hash(sample, dep_hash="0" * 16)
        assert isinstance(result, str) and len(result) > 0


def test_scope_diagnostics_filename_includes_dep_hash(tmp_path):
    """Two distinct dep_hashes for the same TU must produce two distinct
    sidecar files -- the dep_hash is part of the filename so they can't
    collide in the diagnostics dir."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        dep_a = "a" * 16
        dep_b = "b" * 16
        hntr.macro_state_hash(sample, dep_hash=dep_a)
        hntr.macro_state_hash(sample, dep_hash=dep_b)

        scope_dir = _scope_dir(tmp_path)
        assert os.path.isfile(os.path.join(scope_dir, f"{os.path.basename(sample)}.{dep_a}.json"))
        assert os.path.isfile(os.path.join(scope_dir, f"{os.path.basename(sample)}.{dep_b}.json"))
