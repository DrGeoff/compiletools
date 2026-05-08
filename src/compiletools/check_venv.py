"""Detect whether the ``ct-cake`` on PATH imports the same compiletools
as the caller.

Two consumers:

1. The test suite -- ``compiletools.testhelper.skipif_e2e_unavailable``
   uses ``cached_venv_mismatch_reason`` to skip subprocess-driven e2e
   tests when the venv's editable install points at a different
   worktree (otherwise the tests would silently exercise the wrong
   code).

2. The ``ct-check-venv`` CLI -- humans run it from the shell to
   diagnose "why does ct-cake behave like an old version" before
   chasing real bugs.

The check works by:

1. Locating ``ct-cake`` on PATH.
2. Reading its shebang to find the Python interpreter the venv
   installed it for.
3. Running that interpreter to print the parent directory of
   ``compiletools``.
4. Comparing the realpath result to the caller-supplied expected
   src root.
"""

import functools
import os
import shutil
import subprocess
import sys


def venv_mismatch_reason(expected_src_root: str) -> str | None:
    """Return ``None`` when ct-cake's compiletools resolves to
    ``expected_src_root``, else a human-readable explanation.

    ``expected_src_root`` is the directory expected to contain
    ``compiletools/__init__.py`` -- typically
    ``os.path.dirname(os.path.dirname(os.path.abspath(__file__)))``
    from the caller.
    """
    cake = shutil.which("ct-cake")
    if not cake:
        return ("ct-cake not on PATH (e2e checks need a venv with "
                "compiletools installed)")
    try:
        with open(cake) as f:
            first = f.readline().strip()
    except OSError as e:
        return f"can't read ct-cake script {cake!r}: {e}"
    if not first.startswith("#!"):
        # On Linux, both `pip install -e .` and `uv pip install -e .`
        # always generate shebang scripts for console entry points, so
        # this branch is only reachable if a future packaging change
        # ships ct-cake as a native binary (PyInstaller bundle, uv
        # binary launcher, etc.). If you hit this, return a skip-with-
        # reason string instead of silently passing the venv check --
        # otherwise e2e tests would exercise an unverified install.
        return None
    interpreter = first[2:].split()[0]
    try:
        r = subprocess.run(
            [interpreter, "-c",
             "import compiletools, os; "
             "print(os.path.dirname(os.path.dirname(os.path.realpath(compiletools.__file__))))"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"can't run ct-cake's Python ({interpreter!r}): {e}"
    if r.returncode != 0:
        return f"ct-cake's Python failed to introspect compiletools: {r.stderr.strip()}"
    actual = r.stdout.strip()
    expected = os.path.realpath(expected_src_root)
    if os.path.realpath(actual) == expected:
        return None
    return (
        f"venv mismatch: ct-cake imports compiletools from {actual!r}, "
        f"but the caller expects {expected!r}. The venv's editable "
        "install points at a different worktree, so e2e tests / "
        "ct-cake invocations would exercise the wrong code. Fix with "
        "`uv pip install -e .` (or `pip install -e .`) from this "
        "worktree."
    )


@functools.lru_cache(maxsize=8)
def cached_venv_mismatch_reason(expected_src_root: str) -> str | None:
    """Like :func:`venv_mismatch_reason` but caches per src root.

    Pytest collects single-threaded, so a per-root LRU is safe and
    keeps the introspection subprocess from running once per
    ``skipif`` marker (six markers in test_cxx_modules.py alone).
    """
    return venv_mismatch_reason(expected_src_root)


def main():
    """``ct-check-venv`` entry point.

    Compares the ct-cake on PATH against the compiletools that THIS
    process imports (i.e., the editable install behind the
    ``ct-check-venv`` invocation itself). Exits 0 when they match,
    1 when they don't.
    """
    import compiletools
    expected = os.path.dirname(os.path.dirname(os.path.realpath(compiletools.__file__)))
    reason = venv_mismatch_reason(expected)
    if reason is None:
        cake = shutil.which("ct-cake") or "ct-cake"
        print(f"ok: ct-cake ({cake}) and ct-check-venv both resolve "
              f"compiletools to {expected!r}")
        return 0
    print(reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
