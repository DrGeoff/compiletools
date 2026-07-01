"""Unit tests for BuildContext.

Compiler-free and network-free: BuildContext is a plain state/cache holder,
so these exercise its documented contracts directly.
"""

from __future__ import annotations

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
