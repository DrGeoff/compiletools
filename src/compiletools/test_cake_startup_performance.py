"""Focused regression tests for ct-cake startup-time work."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import compiletools.apptools
from compiletools.testhelper import CakeTestContext


def test_auto_discovery_creates_analysis_objects_once_after_targets_are_known():
    """Pure --auto target discovery should not instantiate Hunter/HeaderDeps twice."""
    events = []

    class FakeFindTargets:
        def __init__(self, args, *, context):
            events.append("find-init")

        def process(self, args):
            events.append("find-process")
            args.filename.append("main.cpp")

    with CakeTestContext("make", auto=True) as (cake, _tmpdir):
        cake.args.filename = []
        cake.args.static = []
        cake.args.dynamic = []
        cake.args.tests = []
        cake._createctobjs = MagicMock(side_effect=lambda: events.append("create"))

        with (
            patch("compiletools.findtargets.FindTargets", FakeFindTargets),
            patch.object(
                compiletools.apptools,
                "substitutions",
                side_effect=lambda args, verbose=0: events.append("substitutions"),
            ),
            patch.object(compiletools.apptools, "verboseprintconfig"),
            patch.object(cake, "_call_backend", side_effect=lambda: events.append("backend")),
        ):
            cake.process()

    assert events == ["find-init", "find-process", "substitutions", "create", "backend"]


def test_cake_argument_registration_keeps_backend_modules_cold():
    """Parser construction should not import every backend before backend dispatch."""
    src_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import json
        import sys

        import compiletools.apptools
        import compiletools.cake

        argv = [
            "--backend=slurm",
            "--slurm-mem=4G",
            "--slurm-time=00:10:00",
            "--slurm-mem-tiers=1:1G,4:4G",
        ]
        cap = compiletools.apptools.create_parser("startup probe", argv=argv)
        compiletools.cake.Cake.add_arguments(cap)
        args = cap.parse_args(argv)
        backend_modules = sorted(
            name
            for name in sys.modules
            if name
            in {
                "compiletools.bazel_backend",
                "compiletools.cmake_backend",
                "compiletools.makefile_backend",
                "compiletools.ninja_backend",
                "compiletools.trace_backend",
            }
        )
        print(
            json.dumps(
                {
                    "backend_modules": backend_modules,
                    "slurm_mem": args.slurm_mem,
                    "slurm_time": args.slurm_time,
                    "slurm_mem_tiers": args.slurm_mem_tiers,
                }
            )
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "backend_modules": [],
        "slurm_mem": "4G",
        "slurm_time": "00:10:00",
        "slurm_mem_tiers": [[1, "1G"], [4, "4G"]],
    }


def test_cold_backend_registry_only_treats_shake_as_always_available():
    """Cold registry enumeration should not advertise external-tool backends as available."""
    src_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import json

        from compiletools.build_backend import available_backends, known_backend_names

        print(
            json.dumps(
                {
                    "available": available_backends(),
                    "known": known_backend_names(),
                }
            )
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["available"] == ["shake"]
    assert "ninja" in data["known"]
    assert "make" in data["known"]


def test_fast_backend_metadata_matches_loaded_backend_registry():
    """Fast backend metadata must stay in sync with real registered backend classes."""
    src_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import json

        from compiletools import build_backend

        fast_known = build_backend.known_backend_names()
        fast_always_available = build_backend.available_backends()

        build_backend.ensure_backends_registered()

        registered_names = sorted(build_backend._REGISTRY)
        self_executing_names = sorted(
            name
            for name, cls in build_backend._REGISTRY.items()
            if getattr(cls, "tool_command", lambda: None)() is None
        )
        loaded_available = build_backend.available_backends()

        print(
            json.dumps(
                {
                    "fast_known": fast_known,
                    "registered": registered_names,
                    "fast_always_available": fast_always_available,
                    "self_executing": self_executing_names,
                    "loaded_available": loaded_available,
                }
            )
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["fast_known"] == data["registered"]
    assert data["fast_always_available"] == data["self_executing"] == ["shake"]
    assert data["loaded_available"] == data["registered"]


def test_ninja_backend_choice_does_not_import_ninja_backend():
    """--backend=ninja should parse from metadata without importing the backend module."""
    src_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import json
        import sys

        import compiletools.apptools
        import compiletools.cake

        argv = ["--backend=ninja"]
        cap = compiletools.apptools.create_parser("ninja startup probe", argv=argv)
        compiletools.cake.Cake.add_arguments(cap)
        args = cap.parse_args(argv)
        print(
            json.dumps(
                {
                    "backend": args.backend,
                    "ninja_imported": "compiletools.ninja_backend" in sys.modules,
                }
            )
        )
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"backend": "ninja", "ninja_imported": False}
