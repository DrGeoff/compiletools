"""Tests for diagnostics module."""

from __future__ import annotations

import os
import re
import time
from types import SimpleNamespace

import pytest

from compiletools import diagnostics


@pytest.fixture(autouse=True)
def _reset_diagnostics_cache():
    diagnostics._reset_for_tests()
    yield
    diagnostics._reset_for_tests()


def test_invocation_id_format():
    iid = diagnostics.invocation_id()
    assert re.match(r"^\d{8}T\d{6}-\d+$", iid), iid


def test_invocation_id_cached_within_process():
    a = diagnostics.invocation_id()
    b = diagnostics.invocation_id()
    assert a == b


def test_reset_for_tests_clears_cache():
    a = diagnostics.invocation_id()
    # Sleep > 1 second so the YYYYMMDDTHHMMSS portion is guaranteed to change.
    time.sleep(1.1)
    diagnostics._reset_for_tests()
    b = diagnostics.invocation_id()
    assert a != b


def test_resolve_diagnostics_dir_uses_explicit_dir(tmp_path):
    parent = tmp_path / "diag"
    args = SimpleNamespace(diagnostics_dir=str(parent), bindir=None)
    result = diagnostics.resolve_diagnostics_dir(args)
    iid = diagnostics.invocation_id()
    assert result == os.path.join(str(parent), iid)
    assert os.path.isdir(result)


def test_resolve_diagnostics_dir_falls_back_to_bindir(tmp_path):
    bindir = tmp_path / "bin"
    args = SimpleNamespace(diagnostics_dir=None, bindir=str(bindir))
    result = diagnostics.resolve_diagnostics_dir(args)
    iid = diagnostics.invocation_id()
    assert result == os.path.join(str(bindir), "diagnostics", iid)
    assert os.path.isdir(result)


def test_resolve_diagnostics_dir_idempotent(tmp_path):
    args = SimpleNamespace(diagnostics_dir=str(tmp_path / "diag"), bindir=None)
    first = diagnostics.resolve_diagnostics_dir(args)
    second = diagnostics.resolve_diagnostics_dir(args)
    assert first == second
    assert os.path.isdir(first)


def test_resolve_diagnostics_dir_raises_when_neither_set():
    args = SimpleNamespace(diagnostics_dir=None, bindir=None)
    with pytest.raises(RuntimeError):
        diagnostics.resolve_diagnostics_dir(args)


def test_resolve_diagnostics_dir_empty_string_falls_back_to_bindir(tmp_path):
    bindir = tmp_path / "bin"
    args = SimpleNamespace(diagnostics_dir="", bindir=str(bindir))
    result = diagnostics.resolve_diagnostics_dir(args)
    iid = diagnostics.invocation_id()
    assert result == os.path.join(str(bindir), "diagnostics", iid)
    assert os.path.isdir(result)
