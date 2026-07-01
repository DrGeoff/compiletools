"""Prove the ``_hermetic_git_env`` autouse fixture (in ``conftest.py``) isolates
every test's git subprocesses from ambient system/global gitconfig.

Production ``fetch._git_env()`` intentionally does NOT neutralise ambient git
config; the test suite's determinism instead relies on the centralised autouse
fixture setting the isolation vars in ``os.environ`` so any git/ct-fetch/ct-cake
subprocess a test spawns inherits them. These tests are the regression guard for
that mechanism.
"""

from __future__ import annotations

import os
import subprocess


def _git_config_get(cwd: str, key: str) -> str:
    """Return ``git config --get <key>`` (stripped) or '' if unset.

    Inherits ``os.environ`` on purpose — that is exactly what a git subprocess
    spawned by ct-fetch / ct-cake does, so this exercises the fixture's effect.
    """
    proc = subprocess.run(
        ["git", "config", "--get", key],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_isolation_env_vars_are_set() -> None:
    """The autouse fixture puts the isolation vars into os.environ so any
    subprocess a test spawns (inheriting os.environ) is hermetic."""
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"
    global_cfg = os.environ.get("GIT_CONFIG_GLOBAL")
    assert global_cfg is not None
    # Points at a throwaway empty file, not the developer's ~/.gitconfig.
    assert os.path.abspath(global_cfg) != os.path.abspath(os.path.expanduser("~/.gitconfig"))
    assert os.environ.get("GIT_CONFIG_SYSTEM") == os.devnull


def test_hostile_ambient_home_gitconfig_is_not_seen(tmp_path, monkeypatch) -> None:
    """A hostile developer ``~/.gitconfig`` must not leak into git subprocesses.

    Simulate an ambient user config by pointing HOME at a dir whose
    ``.gitconfig`` carries a hostile ``init.defaultBranch``. Because the autouse
    fixture has already redirected GIT_CONFIG_GLOBAL to an empty file (which
    takes precedence over the HOME-derived ``~/.gitconfig``), a subprocess must
    NOT observe the hostile value.
    """
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    (hostile_home / ".gitconfig").write_text(
        "[init]\n\tdefaultBranch = hostile-ambient-branch\n[commit]\n\tgpgsign = true\n"
    )
    monkeypatch.setenv("HOME", str(hostile_home))

    repo = tmp_path / "repo"
    repo.mkdir()

    # The hostile value lives in the HOME ~/.gitconfig, but GIT_CONFIG_GLOBAL
    # (set by the fixture to an empty file) shadows it.
    assert _git_config_get(str(repo), "init.defaultBranch") == ""
    assert _git_config_get(str(repo), "commit.gpgsign") == ""


def test_commits_succeed_without_local_or_ambient_identity(tmp_path) -> None:
    """The fixture supplies a commit identity, so a fresh repo can commit even
    with system/global config neutralised and no per-repo user.name/email set."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == "ct-test <ct-test@example.com>"
