"""Shared pytest configuration for compiletools tests.

This conftest.py provides session-wide fixtures that are automatically
applied to all tests in src/compiletools/ and subdirectories.
"""

import hashlib
import os
import sys

import pytest

# Whole-file heavy real-subprocess e2e modules: every test shells out to
# ``ct-cake`` / real builds (often across multiple workspaces or every backend).
# On a many-core host ``-n auto`` spawns ~nproc workers (~127 here), so without
# bounding, ~all of these fire concurrent builds/locks and contend on the shared
# filesystem CAS / bazel servers, flaking intermittently (each passes in
# isolation). Bound their TOTAL concurrency by sharding every test in these
# modules across a fixed pool of xdist load-groups (requires ``--dist loadgroup``,
# set in pyproject addopts): at most ``_HEAVY_E2E_POOL`` run at once while the
# shards still parallelise. Sharding is by a stable content hash of the nodeid
# (NOT ``hash()``, which is per-process salted and would make xdist workers
# disagree on grouping). Backend-specific bounding for the slurm/bazel cells of
# the cross-backend matrix + the rebuild test is done with per-param marks in
# those two files; this hook covers the modules whose every test is heavy.
_HEAVY_E2E_MODULES = frozenset(
    {
        "test_e2e_cas_reuse_across_workspaces",
        "test_multiuser_cache",
        "test_backend_integration",
    }
)
_HEAVY_E2E_POOL = 8


def pytest_collection_modifyitems(items):
    """Assign sharded xdist load-groups to whole-file heavy e2e tests so they
    don't oversubscribe the box under ``-n auto`` (see ``_HEAVY_E2E_MODULES``)."""
    for item in items:
        module = os.path.basename(item.nodeid.split("::", 1)[0]).removesuffix(".py")
        if module not in _HEAVY_E2E_MODULES:
            continue
        if item.get_closest_marker("xdist_group") is not None:
            continue
        shard = int(hashlib.sha1(item.nodeid.encode()).hexdigest(), 16) % _HEAVY_E2E_POOL
        item.add_marker(pytest.mark.xdist_group(f"ct_e2e_{shard}"))


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


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Translate the std-support error into a skip, in-process and out.

    ``apptools._check_compiler_supports_requested_standard`` raises
    ``RuntimeError`` at parseargs time when the resolved default variant
    pins (e.g.) ``-std=c++26`` but only an older gcc/clang is on PATH.
    On under-spec systems this would fail the ~190 tests that exercise
    the default variant; per-test decorators don't scale, so handle it
    centrally — same intent as ``uth.requires_compiler_supports_default_std``,
    just applied to every test that happens to trip the check.

    Tests fall into three flavours, all with the same root cause:
      * In-process apptools check: ``RuntimeError("...does not support
        -std=...")`` from the parseargs guard.
      * Out-of-process apptools check: ``ct-*`` subprocess raises the
        same RuntimeError; the text reaches us via the AssertionError
        message that captured stderr.
      * Out-of-process compiler-level rejection: a variant that bypasses
        the apptools probe still trips the compiler itself, surfacing as
        ``g++: error: unrecognized command-line option '-std=c++26'``.
    """
    try:
        return (yield)
    except (RuntimeError, AssertionError) as exc:
        reason = _std_skip_reason(str(exc))
        if reason is not None:
            pytest.skip(reason)
        raise


def _std_skip_reason(msg: str) -> str | None:
    """Return the one-line std-mismatch summary if *msg* indicates the
    resolved compiler can't handle the resolved -std=, else ``None``.

    Recognises the apptools guard text and the gcc/clang driver
    rejections that surface when a variant bypasses the guard.
    """
    for line in msg.splitlines():
        if "does not support -std=" in line:
            return line.strip()
        if "unrecognized command-line option" in line and "-std=c++" in line:
            return line.strip()
        if "invalid value" in line and "-std=" in line:
            return line.strip()
    return None


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
def shutdown_orphan_bazel_servers():
    """Sweep bazel JVM servers rooted in pytest tmp workspaces at session end.

    Tests that build with ``--backend=bazel`` (test_pch_bypass_bug,
    test_test_exe_rebuild_on_upstream_change, test_cake_backend, etc.)
    start a long-lived bazel server per workspace. Bazel's default
    ``--max_idle_secs=10800`` (3 hours) means each one outlives pytest's
    own tmp_path cleanup and leaves dozens of zombie JVMs on the host.
    The cross-backend matrix test already does per-cell shutdown for the
    same reason (``test_examples_end_to_end_cross_backend.py:376-389``);
    this catches the rest at session end.

    Only servers whose ``--workspace_directory=`` points into a pytest
    tmp dir or a ``tempfile.mkdtemp`` (``/tmp/tmp...``) workspace are
    touched — real user-spawned bazel servers in checked-out source
    trees are left alone.
    """
    yield
    import signal
    import subprocess
    import tempfile

    tmp_root = os.path.realpath(tempfile.gettempdir())
    targets: list[tuple[int, str]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace").split("\x00")
        except OSError:
            continue
        ws_args = [a for a in cmdline if a.startswith("--workspace_directory=")]
        if not ws_args:
            continue
        ws = ws_args[0].split("=", 1)[1]
        ws_real = os.path.realpath(ws) if os.path.isabs(ws) else ws
        if not ws_real.startswith(tmp_root + os.sep):
            continue
        # First path component under tmp_root must be a pytest tmp_path
        # parent ('pytest-of-...') or a tempfile.mkdtemp directory ('tmp...').
        first = ws_real[len(tmp_root) + 1 :].split(os.sep, 1)[0]
        if not (first.startswith("pytest-") or first.startswith("tmp")):
            continue
        targets.append((int(entry), ws))

    for pid, ws in targets:
        if os.path.isdir(ws):
            try:
                subprocess.run(
                    ["bazel", "shutdown"],
                    cwd=ws,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                continue
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


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
def pkgconfig_env(monkeypatch):
    """Set PKG_CONFIG_PATH to shared test pkg-config directory.

    This fixture provides access to the consolidated test .pc files in
    examples-features/pkgs/ for tests that need to validate pkg-config functionality.

    The fixture sets PKG_CONFIG_PATH to examples-features/pkgs/ directory
    for the duration of the test.

    Usage in tests:
        def test_something(self, pkgconfig_env):
            # PKG_CONFIG_PATH now points to examples-features/pkgs/
            # Test code that uses pkg-config...

    Available test packages:
    - conditional.pc: For testing macro-dependent conditional includes
    - nested.pc: For testing basic nested header extraction
    - modified.pc: For testing cache invalidation and change detection
    """
    from pathlib import Path

    from compiletools.examples_registry import example_path

    shared_pkgconfig = Path(example_path("pkgs"))
    monkeypatch.setenv("PKG_CONFIG_PATH", str(shared_pkgconfig))

    yield str(shared_pkgconfig)
