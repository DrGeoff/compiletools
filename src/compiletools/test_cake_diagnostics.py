"""Tests for ct-cake's --diagnostics-dir flag and timing.json routing.

Covers:
  - argparse registration (CLI/env/conf wiring via configargparse)
  - timing.json output is routed through resolve_diagnostics_dir(), not
    the old <objdir>/.ct-timing.json location.
"""

from __future__ import annotations

import os

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.diagnostics
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.build_timer import BuildTimer


@pytest.fixture(autouse=True)
def _reset_diagnostics_cache():
    compiletools.diagnostics._reset_for_tests()
    uth.reset()
    yield
    compiletools.diagnostics._reset_for_tests()
    uth.reset()


def _build_args(argv):
    """Build args via the same parser ct-cake uses."""
    cap = compiletools.apptools.create_parser("test cake diagnostics", argv=argv)
    compiletools.cake.Cake.add_arguments(cap)
    return compiletools.apptools.parseargs(cap, argv, verbose=0, context=BuildContext())


def _emulate_finally_block(args):
    """Reproduce the timer-write step of Cake.process()'s finally block."""
    timer = BuildTimer(enabled=True, variant=getattr(args, "variant", ""), backend=getattr(args, "backend", "make"))
    diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(args)
    out = os.path.join(diag_dir, "timing.json")
    timer.to_json(out)
    return out


def test_diagnostics_dir_default_falls_back_to_bindir(tmp_path):
    """With --bindir set and no --diagnostics-dir, timing.json lands under
    <bindir>/diagnostics/<invocation-id>/."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--objdir",
        str(objdir),
        "--timing",
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir is None
    out = _emulate_finally_block(args)
    iid = compiletools.diagnostics.invocation_id()
    expected = str(bindir / "diagnostics" / iid / "timing.json")
    assert out == expected
    assert os.path.isfile(out)


def test_diagnostics_dir_explicit_override(tmp_path):
    """With --diagnostics-dir set, timing.json lands under <diag>/<invocation-id>/."""
    bindir = tmp_path / "bin"
    diag = tmp_path / "diag"
    argv = [
        "--bindir",
        str(bindir),
        "--diagnostics-dir",
        str(diag),
        "--timing",
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(diag)
    out = _emulate_finally_block(args)
    iid = compiletools.diagnostics.invocation_id()
    expected = str(diag / iid / "timing.json")
    assert out == expected
    assert os.path.isfile(out)


def test_diagnostics_not_in_objdir(tmp_path):
    """Timing JSON must not land in --objdir (content-addressable cache)."""
    bindir = tmp_path / "bin"
    objdir = tmp_path / "obj"
    argv = [
        "--bindir",
        str(bindir),
        "--objdir",
        str(objdir),
        "--timing",
    ]
    args = _build_args(argv)
    objdir.mkdir(parents=True, exist_ok=True)
    _emulate_finally_block(args)
    assert not os.path.exists(objdir / ".ct-timing.json")
    assert not os.path.exists(objdir / "timing.json")


def test_diagnostics_dir_env_var(monkeypatch, tmp_path):
    """DIAGNOSTICS_DIR env var binds to args.diagnostics_dir via configargparse."""
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("DIAGNOSTICS_DIR", str(env_dir))
    argv = [
        "--bindir",
        str(tmp_path / "bin"),
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(env_dir)


def test_diagnostics_dir_config_file(tmp_path):
    """diagnostics-dir = <path> in a config file binds to args.diagnostics_dir."""
    conf_path = tmp_path / "diag.conf"
    conf_dir = tmp_path / "from-conf"
    conf_path.write_text(f"diagnostics-dir = {conf_dir}\n")
    argv = [
        "-c",
        str(conf_path),
        "--bindir",
        str(tmp_path / "bin"),
    ]
    args = _build_args(argv)
    assert args.diagnostics_dir == str(conf_dir)
