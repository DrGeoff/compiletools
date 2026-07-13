"""Unit tests for BuildContext.

Compiler-free and network-free: BuildContext is a plain state/cache holder,
so these exercise its documented contracts directly.
"""

from __future__ import annotations

import os

import pytest

from compiletools.apptools import _setup_pkg_config_overrides
from compiletools.build_context import BuildContext


def test_no_custom_deepcopy_hook() -> None:
    """BuildContext must NOT define a custom ``__deepcopy__``.

    The former identity-aliasing ``__deepcopy__`` (return self) existed solely
    so ``fetch._augmented_headerdeps`` could ``copy.deepcopy`` an args namespace
    that transitively referenced the live context. That deepcopy is gone (fetch
    now threads external include dirs through ``headerdeps.create``'s
    ``extra_include_dirs`` parameter), so the class-wide aliasing hook — a latent
    trap on a shared type — is removed. Guard against it silently returning.
    """
    assert "__deepcopy__" not in vars(BuildContext), (
        "BuildContext should not redefine __deepcopy__; the fetch deepcopy that required it has been removed."
    )


def test_restore_pkg_config_path_is_idempotent_when_nothing_applied() -> None:
    """restore_pkg_config_path is a no-op when no override was applied."""
    ctx = BuildContext()
    assert ctx._original_pkg_config_path is None
    ctx.restore_pkg_config_path()  # must not raise
    assert ctx._original_pkg_config_path is None
    assert ctx.pkg_config_overrides_applied is False


def test_restore_pkg_config_path_acquires_pkg_config_override_lock(monkeypatch) -> None:
    """restore_pkg_config_path must serialize its env mutation under the same
    module-level lock the apply side (``_setup_pkg_config_overrides``) takes,
    rather than merely documenting single-process/serial behavior as a
    convention."""
    import compiletools.apptools_pkgconfig as apptools_pkgconfig

    real_lock = apptools_pkgconfig._PKG_CONFIG_OVERRIDE_LOCK
    calls: list[str] = []

    class SpyLock:
        def __enter__(self):
            calls.append("enter")
            return real_lock.__enter__()

        def __exit__(self, *exc_info):
            calls.append("exit")
            return real_lock.__exit__(*exc_info)

    monkeypatch.setattr(apptools_pkgconfig, "_PKG_CONFIG_OVERRIDE_LOCK", SpyLock())
    monkeypatch.setenv("PKG_CONFIG_PATH", "/whatever")

    ctx = BuildContext()
    ctx._original_pkg_config_path = True
    ctx.restore_pkg_config_path()

    assert calls == ["enter", "exit"]


def _stub_gitroot_with_pkgconfig(monkeypatch, tmp_path):
    """Create <tmp_path>/ct.conf.d/pkgconfig/ and stub gitroot/cwd to tmp_path
    so _setup_pkg_config_overrides takes the auto-discovery path (same fixture
    shape as TestSetupPkgConfigOverrides in test_apptools.py)."""
    (tmp_path / "ct.conf.d" / "pkgconfig").mkdir(parents=True)
    monkeypatch.setattr("compiletools.git_utils.find_git_root", lambda filename=None: str(tmp_path))
    monkeypatch.chdir(tmp_path)


class TestPkgConfigPathRestoredContextManager:
    """Regression tests for the leak the manager closes: a long-lived
    embedder running repeated builds in one process must not carry one
    project's auto-discovered pkg-config dirs into the next project's
    PKG_CONFIG_PATH."""

    def test_restores_prior_value_when_var_was_set(self, monkeypatch, tmp_path):
        _stub_gitroot_with_pkgconfig(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        with ctx.pkg_config_path_restored():
            _setup_pkg_config_overrides(ctx)
            assert os.environ["PKG_CONFIG_PATH"] != "/original/path"

        assert os.environ.get("PKG_CONFIG_PATH") == "/original/path"
        assert ctx.pkg_config_overrides_applied is False

    def test_removes_var_when_it_was_unset(self, monkeypatch, tmp_path):
        _stub_gitroot_with_pkgconfig(monkeypatch, tmp_path)
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        with ctx.pkg_config_path_restored():
            _setup_pkg_config_overrides(ctx)
            assert "PKG_CONFIG_PATH" in os.environ

        assert "PKG_CONFIG_PATH" not in os.environ

    def test_restores_when_body_raises(self, monkeypatch, tmp_path):
        _stub_gitroot_with_pkgconfig(monkeypatch, tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", "/original/path")

        ctx = BuildContext()
        with pytest.raises(RuntimeError, match="simulated build failure"):
            with ctx.pkg_config_path_restored():
                _setup_pkg_config_overrides(ctx)
                assert os.environ["PKG_CONFIG_PATH"] != "/original/path"
                raise RuntimeError("simulated build failure")

        assert os.environ.get("PKG_CONFIG_PATH") == "/original/path"

    def test_noop_when_no_override_applied(self, monkeypatch):
        monkeypatch.setenv("PKG_CONFIG_PATH", "/untouched")

        ctx = BuildContext()
        with ctx.pkg_config_path_restored():
            pass

        assert os.environ.get("PKG_CONFIG_PATH") == "/untouched"
        assert ctx._original_pkg_config_path is None

    def test_noop_when_var_unset_and_no_override_applied(self, monkeypatch):
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)

        ctx = BuildContext()
        with ctx.pkg_config_path_restored():
            pass

        assert "PKG_CONFIG_PATH" not in os.environ
