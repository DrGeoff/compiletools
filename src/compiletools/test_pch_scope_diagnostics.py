"""Tests for the per-PCH scope-diagnostics sidecar (PCH-B).

When ``--scope-diagnostics`` is enabled and ``resolve_diagnostics_dir``
yields a path, ``_pch_scope_macro_hash`` writes a per-PCH JSON sidecar
under ``<diagnostics-dir>/<invocation-id>/scope/pch/`` listing which
cmdline ``-D`` macros were included vs excluded from the PCH cache key.

Mirrors :mod:`test_scope_diagnostics` (which covers the per-TU object
hash side); piggy-backs on the same diagnostics-dir infrastructure;
silently skips the write when no diagnostics dir is resolvable so
callers without ``--bindir`` / ``--diagnostics-dir`` set don't crash.
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
from compiletools.build_backend import _pch_scope_macro_hash
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

    Mirrors the helper in test_scope_diagnostics.py / test_pch_cache_scoping.py.
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
    """Drive the hunter's magicflags pipeline so _pch_scope_macro_hash is callable."""
    try:
        hunter.magicflags(sample_path)
    except RuntimeError as e:
        if "No functional C++ compiler detected" in str(e):
            pytest.skip("No functional C++ compiler detected")
        raise


def _sample(rel):
    return uth.example_file(f"cache_scoping/{rel}")


def _pch_scope_dir(diag_root):
    """The per-invocation scope/pch subdir under a diagnostics-dir root."""
    iid = compiletools.diagnostics.invocation_id()
    return os.path.join(str(diag_root), iid, "scope", "pch")


def test_pch_scope_diagnostics_off_by_default(tmp_path):
    """Without ``--scope-diagnostics``, no scope/pch/ subdir is written
    even when a diagnostics-dir is resolvable and _pch_scope_macro_hash runs."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        # Make a diagnostics dir resolvable but leave --scope-diagnostics off.
        hntr.args.diagnostics_dir = str(tmp_path)
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)

        _pch_scope_macro_hash(hntr, sample)

        scope_pch_dir = _pch_scope_dir(tmp_path)
        assert not os.path.exists(scope_pch_dir), (
            f"scope/pch dir must not be created when --scope-diagnostics is off, but found: {scope_pch_dir}"
        )


def test_pch_scope_diagnostics_writes_json_when_on(tmp_path):
    """With ``--scope-diagnostics`` on and a diagnostics-dir resolvable,
    a JSON sidecar lands at <diag>/<iid>/scope/pch/<basename>.json."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)

        _pch_scope_macro_hash(hntr, sample)

        expected = os.path.join(
            _pch_scope_dir(tmp_path),
            f"{os.path.basename(sample)}.json",
        )
        assert os.path.isfile(expected), f"expected per-PCH scope JSON at {expected}"


def test_pch_scope_diagnostics_payload_has_required_keys(tmp_path):
    """The JSON payload must carry the keys downstream consumers rely on."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)

        _pch_scope_macro_hash(hntr, sample)

        payload_path = os.path.join(
            _pch_scope_dir(tmp_path),
            f"{os.path.basename(sample)}.json",
        )
        with open(payload_path) as f:
            payload = json.load(f)

        for key in (
            "pch_header",
            "cmdline_d_macros_total",
            "cmdline_d_macros_in_hash",
            "cmdline_d_macros_excluded",
        ):
            assert key in payload, f"missing key {key!r} in {payload}"


def test_pch_scope_diagnostics_lists_included_excluded_correctly(tmp_path):
    """Build with two cmdline -D macros; only one is referenced by the PCH header.

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

        _pch_scope_macro_hash(hntr, sample)

        payload_path = os.path.join(
            _pch_scope_dir(tmp_path),
            f"{os.path.basename(sample)}.json",
        )
        with open(payload_path) as f:
            payload = json.load(f)

        assert "APP_NAME" in payload["cmdline_d_macros_in_hash"], (
            f"APP_NAME should be included (PCH references it); got payload={payload}"
        )
        assert "UNUSED_NAME" in payload["cmdline_d_macros_excluded"], (
            f"UNUSED_NAME should be excluded (PCH does not reference it); got payload={payload}"
        )
        # Sorting invariant for deterministic diffs.
        assert payload["cmdline_d_macros_in_hash"] == sorted(payload["cmdline_d_macros_in_hash"])
        assert payload["cmdline_d_macros_excluded"] == sorted(payload["cmdline_d_macros_excluded"])


def test_pch_scope_diagnostics_empty_scope_still_writes(tmp_path):
    """Even when scope_filter is empty (no cmdline-D macro is referenced),
    the diagnostic file is still written so users can see "0 macros mattered
    for this PCH" -- the success path for cross-app PCH reuse.
    """
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=foo"], temp_config)
        hntr.args.diagnostics_dir = str(tmp_path)
        hntr.args.scope_diagnostics = True
        # no_ref.cpp does not reference APP_NAME, so scope_filter is empty.
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        _pch_scope_macro_hash(hntr, sample)

        payload_path = os.path.join(
            _pch_scope_dir(tmp_path),
            f"{os.path.basename(sample)}.json",
        )
        assert os.path.isfile(payload_path), (
            f"expected per-PCH scope JSON at {payload_path} even with empty scope_filter"
        )
        with open(payload_path) as f:
            payload = json.load(f)
        assert payload["cmdline_d_macros_in_hash"] == [], (
            f"in_hash should be empty when no cmdline-D macro is referenced; got {payload}"
        )
        assert payload["cmdline_d_macros_excluded"] == ["APP_NAME"], f"excluded should list APP_NAME; got {payload}"


def test_pch_scope_diagnostics_skipped_when_no_diagnostics_dir_resolvable(tmp_path):
    """If neither --diagnostics-dir nor --bindir is set, the diagnostic
    write must be silently skipped (resolve_diagnostics_dir raises
    RuntimeError) -- _pch_scope_macro_hash must NOT crash."""
    with uth.TempConfigContext() as temp_config:
        hntr = _make_hunter(["--append-CPPFLAGS=-DAPP_NAME=A"], temp_config)
        hntr.args.scope_diagnostics = True
        # Force both to be unresolvable.
        hntr.args.diagnostics_dir = None
        hntr.args.bindir = None
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)

        # Should not raise.
        result = _pch_scope_macro_hash(hntr, sample)
        assert isinstance(result, str) and len(result) > 0
