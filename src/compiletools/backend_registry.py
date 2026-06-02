"""Backend registry, decorator, and CLI-argument registration for the build backends.

This module owns the single source of truth for backend *discovery*:

* the ``_REGISTRY`` dict that ``@register_backend`` populates and
  ``get_backend_class`` reads,
* the built-in backend module map and availability helpers, and
* the per-backend CLI-argument registrars (make / bazel / slurm) plus the
  Slurm argparse type-validators.

It is a deliberately leaf layer. It imports only stdlib plus genuinely-leaf
compiletools modules (``apptools``, ``utils``) and **never imports
``build_backend`` at runtime** -- the registry only stores and returns
backend classes, so it needs the ``BuildBackend`` type as a hint only
(resolved under ``TYPE_CHECKING``). ``build_backend`` binds these names back
into its own namespace, preserving object identity so that:

* backend modules that ``from compiletools.build_backend import register_backend``
  decorate into the *same* ``_REGISTRY`` dict this module exposes, and
* ``unittest.mock.patch`` targets and direct importers that reference
  ``compiletools.build_backend.<name>`` keep resolving after the move.

Registration timing is unchanged: ``_import_builtin_backend`` /
``ensure_backends_registered`` import the concrete backend modules
(``makefile_backend``, ``ninja_backend``, ``cmake_backend``,
``bazel_backend``, ``trace_backend``); that import triggers their
``@register_backend`` decorators, which mutate this shared ``_REGISTRY``.
The import stays lazy (driven by callers that enumerate the registry) to
keep startup cost low and to avoid the build_backend <- bazel_backend <-
build_backend cycle.
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

import compiletools.apptools
import compiletools.utils

if TYPE_CHECKING:
    from compiletools.build_backend import BuildBackend


_REGISTRY: dict[str, type[BuildBackend]] = {}

_BUILTIN_BACKEND_MODULES: Mapping[str, str] = MappingProxyType(
    {
        "bazel": "compiletools.bazel_backend",
        "cmake": "compiletools.cmake_backend",
        "make": "compiletools.makefile_backend",
        "ninja": "compiletools.ninja_backend",
        "shake": "compiletools.trace_backend",
        "slurm": "compiletools.trace_backend",
    }
)

_ALWAYS_AVAILABLE_BACKENDS = frozenset({"shake"})

_DEFAULT_MEM_TIERS_STR = "1:1G,2:2G,4:4G,8:8G,16:16G"

# LD_LIBRARY_PATH is included because non-system-installed compilers (Spack, Lmod,
# environment modules, custom installs) almost always need it to find their shared
# libs on the compute node. Other HPC vars (MODULEPATH, LMOD_*, SPACK_ROOT, etc.)
# are deliberately excluded — sites using those toolchains can extend this via
# --slurm-export.
_DEFAULT_SLURM_EXPORT = "PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH"

_BackendT = TypeVar("_BackendT", bound="BuildBackend")


def register_backend(cls: type[_BackendT]) -> type[_BackendT]:
    """Register a backend class. Can be used as a decorator.

    Adding a new backend should be a single drop-in: implement
    BuildBackend, declare ``@staticmethod tool_command()`` if the backend
    needs an external tool (return None / ``("a", "b")`` for fallbacks),
    and register. The registry is the single source of truth for
    discovery, availability, and CLI argument registration.
    """
    _REGISTRY[cls.name()] = cls
    return cls


def _import_builtin_backend(name: str) -> None:
    module_name = _BUILTIN_BACKEND_MODULES.get(name)
    if module_name is not None:
        importlib.import_module(module_name)


def get_backend_class(name: str) -> type[BuildBackend]:
    """Look up a backend class by name. Raises ValueError if not found."""
    if name not in _REGISTRY:
        _import_builtin_backend(name)
    if name not in _REGISTRY:
        available = ", ".join(known_backend_names()) or "(none)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return _REGISTRY[name]


def known_backend_names() -> list[str]:
    """Return sorted backend names accepted by the CLI without importing them."""
    return sorted(set(_REGISTRY.keys()) | set(_BUILTIN_BACKEND_MODULES.keys()))


def available_backends() -> list[str]:
    """Return sorted list of registered backends plus always-available built-ins."""
    return sorted(set(_REGISTRY.keys()) | _ALWAYS_AVAILABLE_BACKENDS)


def ensure_backends_registered() -> None:
    """Import all built-in backend modules to trigger @register_backend.

    Called lazily by code that enumerates the registry rather than from this
    module's import time, to keep startup cost low for non-build code paths
    and to avoid the build_backend ← bazel_backend ← build_backend cycle.
    """
    for module_name in dict.fromkeys(_BUILTIN_BACKEND_MODULES.values()):
        importlib.import_module(module_name)


def backend_tool_command(name: str) -> str | None:
    """Return the external tool command for a backend, or None if
    self-executing. Reads ``cls.tool_command()`` from the registered
    backend; first element of any tuple is canonical."""
    cls = _REGISTRY.get(name)
    if cls is None:
        _import_builtin_backend(name)
        cls = _REGISTRY.get(name)
    if cls is None:
        return None
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return None
    if isinstance(tool, tuple):
        return tool[0]
    return tool


def is_backend_available(name: str) -> bool:
    """Check whether the external tool for a backend is installed.

    Backends declare their tool requirement via the optional
    ``tool_command()`` classmethod, which may return:

    * ``None``        — self-executing, always available
    * ``"name"``      — single binary; available iff on PATH
    * ``("a", "b")``  — alternates; available iff at least one on PATH
    """
    import shutil

    cls = _REGISTRY.get(name)
    if cls is None:
        _import_builtin_backend(name)
        cls = _REGISTRY.get(name)
    if cls is None:
        return False
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return True  # self-executing backends
    candidates = (tool,) if isinstance(tool, str) else tuple(tool)
    return any(shutil.which(t) for t in candidates)


def detect_available_backends(requested: list[str]) -> list[str]:
    """Filter requested backends to those whose build tool is installed."""
    available = []
    for backend in requested:
        if is_backend_available(backend):
            available.append(backend)
        else:
            tool = backend_tool_command(backend) or backend
            print(f"  Skipping backend '{backend}': '{tool}' not found on PATH")
    return available


def _parse_slurm_mem(mem_str: str) -> int:
    s = mem_str.strip().upper()
    if not s:
        raise ValueError("empty memory value")
    if s.endswith("G"):
        return int(s[:-1]) * 1024
    if s.endswith("M"):
        return int(s[:-1])
    return int(s)


def _slurm_mem_arg(value: str) -> str:
    try:
        if _parse_slurm_mem(value) <= 0:
            raise ValueError("memory must be positive")
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid Slurm memory '{value}': {e} (expected '<int>G', '<int>M', or '<int>')"
        ) from e
    return value


def _slurm_time_arg(value: str) -> str:
    s = value.strip()
    if not s:
        raise argparse.ArgumentTypeError("invalid Slurm time: empty")
    rest = s
    if "-" in rest:
        day_str, rest = rest.split("-", 1)
        try:
            if int(day_str) < 0:
                raise ValueError("days must be non-negative")
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': bad days field") from e
    parts = rest.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': expected HH:MM:SS or D-HH:MM:SS")
    try:
        for p in parts:
            if int(p) < 0:
                raise ValueError("time fields must be non-negative")
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid Slurm time '{value}': {e}") from e
    return value


def _slurm_mem_tiers_arg(value: str) -> list[tuple[int, str]]:
    if not value or not value.strip():
        raise argparse.ArgumentTypeError("invalid --slurm-mem-tiers: empty")
    tiers: list[tuple[int, str]] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers entry '{entry}': expected '<threshold>:<mem>'")
        thr_str, mem_str = entry.split(":", 1)
        try:
            threshold = int(thr_str.strip())
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers threshold '{thr_str}': {e}") from e
        mem = mem_str.strip()
        try:
            _parse_slurm_mem(mem)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid --slurm-mem-tiers memory '{mem}': {e}") from e
        tiers.append((threshold, mem))
    if not tiers:
        raise argparse.ArgumentTypeError("invalid --slurm-mem-tiers: no entries")
    tiers.sort(key=lambda t: t[0])
    return tiers


def _slurm_max_wait_arg(value: str) -> float:
    s = (value or "").strip()
    if not s:
        raise argparse.ArgumentTypeError("invalid --slurm-max-wait: empty")
    try:
        seconds = float(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid --slurm-max-wait '{value}': not a number") from e
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"invalid --slurm-max-wait '{value}': must be positive")
    return seconds


def _register_make_cli_arguments(cap) -> None:
    if compiletools.apptools._parser_has_option(cap, "--makefilename"):
        return
    cap.add_argument(
        "--makefilename",
        default="Makefile",
        help="Output filename for the Makefile",
    )
    cap.add_argument(
        "--build-only-changed",
        help=(
            "Only build the binaries depending on the source or header absolute filenames in this space-delimited list."
        ),
    )
    compiletools.apptools.add_locking_arguments(cap)
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="serialise-tests",
        dest="serialisetests",
        default=False,
        help="Force the unit tests to run serially rather than in parallel. Defaults to false because it is slower.",
    )
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="shuffle",
        dest="shuffle",
        default=False,
        help=(
            "Pass --shuffle to GNU Make (>= 4.4) to randomize prerequisite ordering. "
            "Useful for CI to detect missing dependencies."
        ),
    )


def _register_bazel_cli_arguments(cap) -> None:
    if compiletools.apptools._parser_has_option(cap, "--bazel-jvm-stack-size"):
        return
    cap.add_argument(
        "--bazel-jvm-stack-size",
        default="256k",
        help=(
            "Per-thread JVM stack size passed to bazel as --host_jvm_args=-Xss<value>. "
            "Bazel sizes its internal thread pool by --jobs and reserves the default 1MB stack per slot, "
            "which OOMs on many-core hosts. 256k is sufficient for bazel's worker threads. Set empty to skip."
        ),
    )


def _register_slurm_cli_arguments(cap) -> None:
    if compiletools.apptools._parser_has_option(cap, "--slurm-partition"):
        return
    cap.add_argument(
        "--slurm-partition",
        default=None,
        help="Slurm partition (queue) for compile jobs. Omit to use the site default partition.",
    )
    cap.add_argument(
        "--slurm-time",
        default="00:30:00",
        type=_slurm_time_arg,
        help="Wall-clock time limit per compile job (HH:MM:SS or D-HH:MM:SS). Default: 00:30:00",
    )
    cap.add_argument(
        "--slurm-mem",
        default="16G",
        type=_slurm_mem_arg,
        help="Memory ceiling per compile job (e.g. 16G, 8G, 512M). Default: 16G",
    )
    cap.add_argument(
        "--slurm-cpus",
        default=1,
        type=int,
        help="CPUs allocated per compile job. Default: 1",
    )
    cap.add_argument(
        "--slurm-account",
        default=None,
        help="Slurm account/project to charge for compile jobs.",
    )
    cap.add_argument(
        "--slurm-max-array",
        default=1000,
        type=int,
        help="Maximum job-array size per sbatch call. Larger projects are split into multiple arrays. Default: 1000",
    )
    cap.add_argument(
        "--slurm-poll-interval",
        default=2.0,
        type=float,
        help="Seconds between sacct polls when waiting for compile jobs. Default: 2.0",
    )
    cap.add_argument(
        "--slurm-job-name",
        default="ct-compile",
        help="Name applied to submitted Slurm jobs (visible in squeue/sacct). Default: ct-compile. "
        "Useful for distinguishing concurrent ct-cake invocations.",
    )
    cap.add_argument(
        "--slurm-mem-tiers",
        default=_DEFAULT_MEM_TIERS_STR,
        type=_slurm_mem_tiers_arg,
        help="Memory tier mapping as 'threshold:mem,threshold:mem,...' where threshold is "
        "the maximum work-weight for that tier (quoted-include count for compile rules, "
        "input-object count for link/library rules). Rules whose weight exceeds the largest "
        "threshold use --slurm-mem. Default: " + _DEFAULT_MEM_TIERS_STR,
    )
    cap.add_argument(
        "--slurm-sacct-failure-threshold",
        default=10,
        type=int,
        help="Consecutive sacct failures tolerated before _wait_for_arrays raises. Default: 10",
    )
    cap.add_argument(
        "--slurm-output-wait-timeout",
        default=30.0,
        type=float,
        help="Seconds to wait for compiled outputs to become visible on the submitter "
        "after sacct reports COMPLETED (network filesystem metadata lag). Default: 30.0",
    )
    cap.add_argument(
        "--slurm-export",
        default=_DEFAULT_SLURM_EXPORT,
        help="Value passed to sbatch --export=. Default propagates a curated allowlist "
        f"({_DEFAULT_SLURM_EXPORT}) instead of the submitter's full environment. "
        "Use 'ALL' to restore legacy behavior, 'NONE' for a fully isolated environment, "
        "or extend the default for Lmod/Spack sites (e.g. "
        "'PATH,HOME,USER,LANG,LC_ALL,CC,CXX,CPATH,LD_LIBRARY_PATH,MODULEPATH,LMOD_CMD'). "
        "See README.ct-backends for guidance.",
    )
    cap.add_argument(
        "--slurm-rule-retry-cap",
        default=3,
        type=int,
        help="Maximum OOM retries per rule before that rule is abandoned. Default: 3",
    )
    cap.add_argument(
        "--slurm-max-wait",
        default=7200.0,
        type=_slurm_max_wait_arg,
        help="Total wall-clock seconds to wait for all submitted Slurm arrays to reach a terminal state. "
        "Raised as RuntimeError if exceeded. Tune upward on busy clusters where queue waits exceed the default. "
        "Default: 7200.0 (2 hours)",
    )


def register_backend_cli_arguments(cap) -> None:
    """Register built-in backend CLI flags without importing backend modules.

    Built-in backends are imported only when their class is needed for dispatch
    or when callers explicitly enumerate registered classes. Any third-party
    backend that has already registered itself still gets a chance to add flags.
    """
    _register_make_cli_arguments(cap)
    _register_bazel_cli_arguments(cap)
    _register_slurm_cli_arguments(cap)

    for name, cls in list(_REGISTRY.items()):
        if name in _BUILTIN_BACKEND_MODULES:
            continue
        adder = getattr(cls, "add_arguments", None)
        if callable(adder):
            adder(cap)
