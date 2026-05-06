"""Shared pytest configuration for compiletools tests.

This conftest.py provides session-wide fixtures that are automatically
applied to all tests in src/compiletools/ and subdirectories.
"""

import os
import sys

import pytest

# Each compile/link rule fans out to ~4 children (cpp -MM, cc1, as,
# collect2/ld). Cap each xdist worker's view of nproc to nproc /
# (workers * fanout) so the total fork budget across all workers stays
# under RLIMIT_NPROC on many-core shared hosts. Empirically tuned on a
# 127-core RHEL8 host; raising to 8 didn't measurably reduce residual
# flakes (which trace to peer-tenant ulimit pressure, not us). Override
# via CT_TEST_FORK_FANOUT env var if a different host needs a different
# budget.
_FORK_FANOUT_PER_RULE = 4
_MIN_PARALLEL = 2


def pytest_configure(config):
    """Cap default --parallel under pytest-xdist to avoid the fork storm.

    pytest -n N x make -j nproc x bazel --jobs=nproc on a many-core host
    spawns thousands of compile/link children concurrently, exhausting
    RLIMIT_NPROC and triggering sporadic failures across unrelated tests
    (atomic_compile signal-forwarding races, slurm sbatch contention,
    bazel JVM thread OOMs, etc.). Cap each xdist worker's view of nproc
    by setting CT_PARALLEL — compiletools.jobs.add_arguments registers
    it as the env_var for --parallel, so configargparse picks it up as
    the default whenever a test parser is built. Tests that pass
    --parallel=K explicitly still get K; an existing CT_PARALLEL setting
    is respected.
    """
    del config
    if "PYTEST_XDIST_WORKER" not in os.environ:
        return
    if "CT_PARALLEL" in os.environ:
        return
    try:
        workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1") or "1")
    except ValueError:
        return
    try:
        fanout = int(os.environ.get("CT_TEST_FORK_FANOUT") or _FORK_FANOUT_PER_RULE)
    except ValueError:
        fanout = _FORK_FANOUT_PER_RULE

    import compiletools.jobs

    actual = compiletools.jobs._cpu_count()
    capped = max(_MIN_PARALLEL, actual // (workers * fanout))
    if capped < actual:
        os.environ["CT_PARALLEL"] = str(capped)


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

    from compiletools.testhelper import samplesdir

    # Save original PKG_CONFIG_PATH
    original_pkg_config_path = os.environ.get("PKG_CONFIG_PATH")

    # Set PKG_CONFIG_PATH to shared pkgs directory
    shared_pkgconfig = Path(samplesdir()) / "pkgs"
    os.environ["PKG_CONFIG_PATH"] = str(shared_pkgconfig)

    # Yield to test
    yield str(shared_pkgconfig)

    # Restore original PKG_CONFIG_PATH
    if original_pkg_config_path is None:
        os.environ.pop("PKG_CONFIG_PATH", None)
    else:
        os.environ["PKG_CONFIG_PATH"] = original_pkg_config_path
