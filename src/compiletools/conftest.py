"""Shared pytest configuration for compiletools tests.

This conftest.py provides session-wide fixtures that are automatically
applied to all tests in src/compiletools/ and subdirectories.
"""

import os
import sys

import pytest

# Each compile/link rule fans out to ~4 children (cpp -MM, cc1, as,
# collect2/ld). The capped_parallel_argv fixture below uses this to derive
# a per-worker nproc budget that keeps the total fork count across all
# xdist workers under RLIMIT_NPROC on many-core shared hosts.
_FORK_FANOUT_PER_RULE = 4
_MIN_PARALLEL = 2


def _capped_parallel():
    """Compute a sustainable --parallel value for the current xdist worker.

    Returns None when not running under pytest-xdist or when the default
    nproc is already small enough — callers can short-circuit and pass no
    --parallel override in that case.
    """
    if "PYTEST_XDIST_WORKER" not in os.environ:
        return None
    try:
        workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1") or "1")
    except ValueError:
        return None
    try:
        fanout = int(os.environ.get("CT_TEST_FORK_FANOUT") or _FORK_FANOUT_PER_RULE)
    except ValueError:
        fanout = _FORK_FANOUT_PER_RULE

    import compiletools.jobs

    actual = compiletools.jobs._cpu_count()
    capped = max(_MIN_PARALLEL, actual // (workers * fanout))
    return capped if capped < actual else None


@pytest.fixture
def capped_parallel_argv():
    """Yield the argv flags needed to cap --parallel under pytest-xdist.

    pytest -n N x make -j nproc x bazel --jobs=nproc on a many-core host
    spawns thousands of compile/link children concurrently, exhausting
    RLIMIT_NPROC and triggering sporadic failures across unrelated tests.
    Tests that invoke a build backend (cake.main, etc.) opt into a
    sustainable cap by accepting this fixture and prepending the yielded
    list to their argv:

        def test_something(capped_parallel_argv):
            argv = capped_parallel_argv + ["--backend=bazel", ...]
            cake.main(argv)

    Returns an empty list when not under xdist (so opt-in tests are a
    no-op outside the fork-storm scenario). Honours an explicit
    CT_PARALLEL env override (returns empty so configargparse picks it
    up directly).
    """
    if "CT_PARALLEL" in os.environ:
        return []
    capped = _capped_parallel()
    if capped is None:
        return []
    return ["--parallel", str(capped)]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    """Append each test nodeid to ``$CT_PYTEST_CHECKPOINT`` (with fsync) before the test runs.

    Opt-in: does nothing unless the env var is set. The fsync guarantees the
    line hits disk before the test body executes, so when the kernel SIGKILLs
    the whole shell (e.g. Android OOM-killer on Termux), the last entry in
    the file is the test that triggered it. ``scripts/ct-pytest-monitor``
    sets this up automatically.
    """
    path = os.environ.get("CT_PYTEST_CHECKPOINT")
    if not path:
        return
    with open(path, "ab") as f:
        f.write(f"{nodeid}\n".encode())
        f.flush()
        os.fsync(f.fileno())


@pytest.fixture(scope="session", autouse=True)
def ensure_lock_helper_in_path():
    """Ensure ct-lock-helper is available in PATH.

    This fixture runs once per test session and is automatically applied to
    all tests (autouse=True). It's required for tests that use the
    --file-locking flag, which needs ct-lock-helper for file locking.

    ct-lock-helper is a Python entry point (installed via pip/uv), so it
    should be available in the venv's bin/ directory. This fixture verifies
    that and prints a warning if not found.
    """
    import shutil

    if not shutil.which("ct-lock-helper"):
        print(
            "\nWARNING: ct-lock-helper not found in PATH. Run 'uv pip install -e .' to install.",
            file=sys.stderr,
        )


@pytest.fixture(scope="function")
def pkgconfig_env():
    """Set PKG_CONFIG_PATH to shared test pkg-config directory.

    This fixture provides access to the consolidated test .pc files in
    samples/pkgs/ for tests that need to validate pkg-config functionality.

    The fixture:
    1. Sets PKG_CONFIG_PATH to samples/pkgs/ directory
    2. Yields control to the test
    3. Restores original PKG_CONFIG_PATH after test completes

    Usage in tests:
        def test_something(self, pkgconfig_env):
            # PKG_CONFIG_PATH now points to samples/pkgs/
            # Test code that uses pkg-config...

    Available test packages:
    - conditional.pc: For testing macro-dependent conditional includes
    - nested.pc: For testing basic nested header extraction
    - modified.pc: For testing cache invalidation and change detection
    """
    from pathlib import Path

    from compiletools.examples_registry import example_path

    # Save original PKG_CONFIG_PATH
    original_pkg_config_path = os.environ.get("PKG_CONFIG_PATH")

    # Set PKG_CONFIG_PATH to shared pkgs directory
    shared_pkgconfig = Path(example_path("pkgs"))
    os.environ["PKG_CONFIG_PATH"] = str(shared_pkgconfig)

    # Yield to test
    yield str(shared_pkgconfig)

    # Restore original PKG_CONFIG_PATH
    if original_pkg_config_path is None:
        os.environ.pop("PKG_CONFIG_PATH", None)
    else:
        os.environ["PKG_CONFIG_PATH"] = original_pkg_config_path
