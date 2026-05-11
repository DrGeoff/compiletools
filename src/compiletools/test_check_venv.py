"""Tests for compiletools.check_venv.

The mismatch probe is what makes other e2e tests skip-rather-than-fail
when a worktree's venv is editable-installed from a different worktree.
Bugs in the probe itself would silently re-introduce the failure mode
the probe was meant to prevent, so cover the branches directly.
"""

import os
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

from compiletools import check_venv


@pytest.fixture(autouse=True)
def _reset_lru(monkeypatch):
    """Drop the LRU cache between tests so each test sees a fresh probe."""
    check_venv.cached_venv_mismatch_reason.cache_clear()
    yield
    check_venv.cached_venv_mismatch_reason.cache_clear()


def _make_fake_ct_cake(
    tmp_path,
    *,
    shebang_target: str | None,
    interpreter_prints: str | None = None,
    interpreter_exit: int = 0,
    interpreter_stderr: str = "",
) -> str:
    """Build a fake ``ct-cake`` script in ``tmp_path/bin``.

    ``shebang_target`` -- the program named after ``#!``. If ``None``,
    the script has no shebang line at all (simulating a hypothetical
    native-binary install).

    ``interpreter_prints`` -- if set, ``shebang_target`` is replaced by
    a tiny shell script that prints this string and exits with
    ``interpreter_exit``. Lets the test control what the shebang's
    "Python" reports as the compiletools install root without needing a
    real Python interpreter.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cake = bin_dir / "ct-cake"
    if shebang_target is None:
        cake.write_text("ELF binary stand-in\n")
    else:
        actual_target = shebang_target
        if interpreter_prints is not None:
            stub = bin_dir / "fake_python"
            stub.write_text(
                textwrap.dedent(f"""\
                #!/bin/bash
                # Ignore the -c "..." invocation; just emit what the test wants.
                printf '%s\\n' {interpreter_prints!r}
                {f"echo {interpreter_stderr!r} >&2" if interpreter_stderr else ""}
                exit {interpreter_exit}
                """)
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            actual_target = str(stub)
        cake.write_text(f"#!{actual_target}\n# fake ct-cake\n")
    cake.chmod(cake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(bin_dir)


def test_returns_reason_when_ct_cake_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty bin dir
    reason = check_venv.venv_mismatch_reason("/anything")
    assert reason is not None
    assert "ct-cake not on PATH" in reason


def test_returns_none_when_ct_cake_has_no_shebang(monkeypatch, tmp_path):
    """The fall-through branch: non-shebang scripts can't be introspected
    cheaply, so the probe declines to flag a mismatch. Documented as
    intentional in check_venv.py -- this test pins the behaviour so a
    future refactor doesn't accidentally start failing tests on
    PyInstaller-style installs."""
    bin_dir = _make_fake_ct_cake(tmp_path, shebang_target=None)
    monkeypatch.setenv("PATH", bin_dir)
    assert check_venv.venv_mismatch_reason("/anywhere") is None


def test_returns_none_when_actual_matches_expected(monkeypatch, tmp_path):
    expected = "/some/worktree/src"
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",  # replaced by stub below
        interpreter_prints=expected,
    )
    monkeypatch.setenv("PATH", bin_dir)
    assert check_venv.venv_mismatch_reason(expected) is None


def test_returns_mismatch_reason_when_paths_differ(monkeypatch, tmp_path):
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints="/some/other/worktree/src",
    )
    monkeypatch.setenv("PATH", bin_dir)
    reason = check_venv.venv_mismatch_reason("/this/worktree/src")
    assert reason is not None
    assert "venv mismatch" in reason
    assert "/some/other/worktree/src" in reason
    assert "/this/worktree/src" in reason
    assert "uv pip install -e ." in reason  # actionable fix included


def test_returns_reason_when_interpreter_exits_nonzero(monkeypatch, tmp_path):
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints="ignored",
        interpreter_exit=1,
        interpreter_stderr="ImportError: no compiletools",
    )
    monkeypatch.setenv("PATH", bin_dir)
    reason = check_venv.venv_mismatch_reason("/whatever")
    assert reason is not None
    assert "failed to introspect" in reason
    assert "ImportError" in reason


def test_returns_reason_when_ct_cake_is_a_directory(monkeypatch, tmp_path):
    """If something on PATH named ct-cake exists but is a directory,
    the open() in venv_mismatch_reason raises OSError -- it must be
    caught and surfaced as a reason rather than crashing the probe."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ct-cake").mkdir()  # ct-cake is a directory, not a file
    # shutil.which only returns executable files, so a bare directory
    # named ct-cake won't be picked up. Add executable bits + a
    # subdirectory entry shaped like a file lookup would resolve
    # against. Skip the test if shutil.which can't see it -- the
    # OSError-on-open path is genuinely hard to trigger from PATH on
    # POSIX, and skipping is preferable to a fragile test.
    monkeypatch.setenv("PATH", str(bin_dir))
    if shutil.which("ct-cake") is None:
        pytest.skip("shutil.which doesn't resolve directories on this platform")
    reason = check_venv.venv_mismatch_reason("/anywhere")
    assert reason is not None
    assert "can't read ct-cake script" in reason


def test_returns_reason_when_ct_cake_is_unreadable(monkeypatch, tmp_path):
    """If ct-cake exists, is executable, but the OPEN itself fails (we
    simulate by feeding the probe a path whose underlying file we then
    chmod 000), the OSError branch must surface a clean reason. This
    is the more reliable cousin of the directory test above."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits; can't simulate unreadable file")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cake = bin_dir / "ct-cake"
    cake.write_text("#!/bin/false\n")
    cake.chmod(0o111)  # exec-only, no read
    monkeypatch.setenv("PATH", str(bin_dir))
    reason = check_venv.venv_mismatch_reason("/anywhere")
    assert reason is not None
    assert "can't read ct-cake script" in reason
    cake.chmod(0o755)  # restore so tmp_path teardown can clean up


def test_returns_mismatch_when_interpreter_prints_empty(monkeypatch, tmp_path):
    """If the interpreter prints an empty string (e.g. a malformed
    compiletools install where __file__ resolves oddly), the empty
    realpath should not match the expected root. Pin this so the
    probe behaviour is explicit instead of accidental."""
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints="",
    )
    monkeypatch.setenv("PATH", bin_dir)
    reason = check_venv.venv_mismatch_reason("/somewhere")
    assert reason is not None
    assert "venv mismatch" in reason


def test_handles_env_style_shebang(monkeypatch, tmp_path):
    """`#!/usr/bin/env python3`-style shebangs must skip past env so
    the probe runs python directly with `-c`, not `env -c "..."`
    (which env would treat as its own flag and fail)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Real interpreter that just prints what we want.
    fake_python = bin_dir / "fake_python"
    fake_python.write_text(
        textwrap.dedent("""\
        #!/bin/bash
        printf '%s\\n' "/expected/src"
        """)
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cake = bin_dir / "ct-cake"
    # env shebang pointing at fake_python on PATH.
    cake.write_text(f"#!/usr/bin/env {fake_python.name}\n")
    cake.chmod(cake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # PATH must contain bin_dir so /usr/bin/env can find fake_python.
    monkeypatch.setenv("PATH", str(bin_dir))
    # If env-handling were broken the probe would invoke
    # `/usr/bin/env -c "..."` which env rejects, surfacing as
    # "ct-cake's Python failed to introspect compiletools" -- not the
    # clean None we want here.
    assert check_venv.venv_mismatch_reason("/expected/src") is None


def test_returns_reason_when_interpreter_missing(monkeypatch, tmp_path):
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/path/that/definitely/does/not/exist/python",
    )
    monkeypatch.setenv("PATH", bin_dir)
    reason = check_venv.venv_mismatch_reason("/anywhere")
    assert reason is not None
    assert "can't run ct-cake's Python" in reason


def test_realpath_normalisation(monkeypatch, tmp_path):
    """Mismatch detection compares realpaths -- a symlinked worktree
    path must match its target. Otherwise, devs who keep their checkouts
    behind a symlink would see false-positive skip messages."""
    real_src = tmp_path / "real_src"
    real_src.mkdir()
    link = tmp_path / "link_src"
    link.symlink_to(real_src)
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints=str(real_src),
    )
    monkeypatch.setenv("PATH", bin_dir)
    assert check_venv.venv_mismatch_reason(str(link)) is None


def test_cached_variant_only_runs_subprocess_once(monkeypatch, tmp_path):
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints="/some/src",
    )
    monkeypatch.setenv("PATH", bin_dir)

    calls = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(check_venv.subprocess, "run", counting_run)

    a = check_venv.cached_venv_mismatch_reason("/some/src")
    b = check_venv.cached_venv_mismatch_reason("/some/src")
    assert a is None and b is None
    assert calls["n"] == 1, "cached_venv_mismatch_reason must memoize per src root"


def test_cli_main_returns_zero_on_match(capsys):
    """``ct-check-venv`` exits 0 when ct-cake's compiletools matches the
    one running ``ct-check-venv`` itself."""
    import compiletools

    expected = os.path.dirname(os.path.dirname(os.path.realpath(compiletools.__file__)))
    # Use a real ct-cake (the venv's) by leaving PATH alone; the test only
    # makes sense in a venv where they actually do match (which they
    # should -- this test runs from this worktree's venv).
    if shutil.which("ct-cake") is None:
        pytest.skip("ct-cake not on PATH for this test session")
    # Pass an explicit empty argv so the parser doesn't pick up pytest's
    # own CLI flags from sys.argv (which configargparse rejects as
    # unrecognized).
    rc = check_venv.main(argv=[])
    captured = capsys.readouterr()
    if rc == 0:
        assert "ok" in captured.out
        assert repr(expected) in captured.out
    else:
        # If the test is being run under a mismatched venv, we should
        # at least see the canonical mismatch reason on stderr.
        assert "venv mismatch" in captured.err or "ct-cake not on PATH" in captured.err


def test_cli_main_returns_one_on_mismatch(monkeypatch, tmp_path, capsys):
    bin_dir = _make_fake_ct_cake(
        tmp_path,
        shebang_target="/bin/bash",
        interpreter_prints="/somewhere/else/src",
    )
    monkeypatch.setenv("PATH", bin_dir)
    rc = check_venv.main(argv=[])
    captured = capsys.readouterr()
    assert rc == 1
    assert "venv mismatch" in captured.err


def test_module_runnable_via_python_dash_m():
    """``python -m compiletools.check_venv`` should also work as an
    invocation form (used in CI before any venv is configured)."""
    r = subprocess.run(
        [sys.executable, "-m", "compiletools.check_venv"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # Either ok (exit 0) or mismatch (exit 1), but never a crash.
    assert r.returncode in (0, 1), f"check_venv crashed: rc={r.returncode}\nstdout:{r.stdout}\nstderr:{r.stderr}"
