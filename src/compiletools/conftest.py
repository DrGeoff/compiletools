"""Shared pytest configuration for compiletools tests.

This conftest.py provides session-wide fixtures that are automatically
applied to all tests in src/compiletools/ and subdirectories.
"""

import os
import sys

import pytest


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
