"""Per-build-session state and caches.

BuildContext replaces module-level singletons by holding all mutable
state that was previously stored in module globals.  One BuildContext
is created per build invocation (by Cake or by tests) and threaded
through the object graph.

All per-build caches live here.  Creating a fresh BuildContext gives a
clean slate — there is no separate ``clear_cache()`` step needed.
"""

from __future__ import annotations

import argparse
import contextlib
from typing import TYPE_CHECKING

import stringzilla as sz

if TYPE_CHECKING:
    from collections.abc import Iterator

    from compiletools.build_inputs import PkgConfigResult
    from compiletools.build_timer import BuildTimer
    from compiletools.file_analyzer import FileAnalysisResult
    from compiletools.preprocessing_cache import FunctionParamsDict, MacroCacheKey, MacroDict, ProcessingResult

# Type alias for headerdeps cache values:
# (include_list, file_defines, file_undefs, file_function_params,
#  content_hash, condition_occurrences)
#
# The fourth slot carries the parameter lists of the function-like macros among
# file_defines.  Without it a replay restores a macro's body but not its arity,
# ``#if F(2, 0)`` downstream reads F as object-like, and the include graph takes
# the other branch on every warm traversal.
#
# The last two slots let a warm hit replay the producing run's condition
# occurrences against the deferred-warning stores
# (preprocessing_cache.replay_condition_occurrences).  magicflags drives
# headerdeps inside its ``converging`` region, so this tier can serve the only
# settled-state evaluation a convergence sees; without the replay, a pending
# "cannot evaluate" record from the convergence's own early pass survives to
# the flush and prints against a gate the build resolved.
IncludeCacheValue = tuple[list[sz.Str], "MacroDict", frozenset, "FunctionParamsDict", str, tuple]


class BuildContext:
    """Holds all per-build-session state and caches.

    Create one at the start of a build and pass it through the call chain.
    When the build (or test) is done, discard the context — all caches are
    garbage-collected with it.

    One piece of state does NOT die with the context: the process-wide
    ``PKG_CONFIG_PATH`` mutation applied by
    ``build_apply.apply_effects`` (the ``SetEnv`` effect computed by the
    build-state core). Library embedders driving multiple builds in one
    process should wrap each build in ``pkg_config_path_restored()`` so
    project A's auto-discovered pkg-config dirs don't bleed into project
    B's environment (the ``ct-cake`` CLI does this in ``cake.main``).
    """

    def __init__(self) -> None:
        # -- global_hash_registry state --
        self.file_hashes: dict[str, str] | None = None
        self.reverse_hashes: dict[str, list[str]] | None = None
        self.hash_ops: dict[str, int] = {"registry_hits": 0, "computed_hashes": 0}

        # -- preprocessing_cache state --
        self.invariant_preprocessing_cache: dict[str, ProcessingResult] = {}
        self.variant_preprocessing_cache: dict[tuple[str, MacroCacheKey], ProcessingResult] = {}
        self.preprocessing_stats: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "total_calls": 0,
            "invariant_hits": 0,
            "variant_hits": 0,
            "invariant_misses": 0,
            "variant_misses": 0,
        }

        # -- simple_preprocessor diagnostics --
        # (filepath, directive_type, condition) already reported as unevaluable.
        # Shared across preprocessor instances because magicflags builds a fresh
        # one per pass over the same file.
        self.warned_preprocessor_conditions: set[tuple[str, str, str]] = set()

        # Unevaluable conditions recorded during a magicflags convergence,
        # holding the message to print. A later pass that evaluates the same
        # condition deletes its entry, so only conditions still unevaluable once
        # the macro state settles are ever reported. Insertion-ordered, so the
        # flush prints in discovery order.
        #
        # Keyed by (filepath, directive_type, condition, line_num) — one entry
        # per OCCURRENCE, unlike the two sets either side of it. Reachability is
        # position-dependent: the same condition text can appear twice in a file
        # with one occurrence live and the other under a dead branch, and a
        # position-free key lets the dead one's retraction delete the live one's
        # report. The flush collapses back to one line per condition.
        self.pending_preprocessor_warnings: dict[tuple[str, str, str, int], str] = {}

        # Occurrences some pass of the current convergence evaluated successfully.
        # Retracting on success is not enough on its own: passes interleave, and
        # a pass that fails AFTER the one that succeeded would otherwise re-record
        # an occurrence already shown to be evaluable. Keyed per occurrence like
        # the pending store: evaluability depends on the macro state, and the
        # state can change between two textually identical occurrences (#undef
        # plus a different-arity redefinition), so one spelling's success must
        # not vouch for another's. Both stores are emptied when the convergence
        # flushes.
        self.resolved_preprocessor_conditions: set[tuple[str, str, str, int]] = set()

        # Depth of the enclosing magicflags convergence. Zero means no pass will
        # follow, so a diagnostic is printed immediately and nothing is deferred;
        # every consumer that drives the preprocessor without magicflags
        # (ct-headertree, ct-filelist) therefore keeps immediate reporting.
        self.preprocessor_convergence_depth: int = 0

        # -- headerdeps module-level caches --
        self.include_list_cache: dict[tuple[str, MacroCacheKey], IncludeCacheValue] = {}
        self.invariant_include_cache: dict[str, IncludeCacheValue] = {}

        # -- file_analyzer state --
        self.analyzer_args: argparse.Namespace | None = None
        self.file_reading_strategy: str | None = None
        self.warned_low_ulimit: bool = False
        self.analyze_file_cache: dict[str, FileAnalysisResult] = {}

        # -- build timer --
        self.timer: BuildTimer | None = None

        # -- git_sha_report state --
        self.repo_has_symlinks: bool | None = None

        # -- apptools pkg-config state --
        # Keyed (package_spec, pkg_config_path, errors_policy, want_libs) by
        # build_inputs._query_pkg_config; the policy is in the key because a
        # warn-mode result must not be served once strict mode is armed, and
        # want_libs because a libs-free entry must not be served to a caller
        # that needs libs.
        self.pkg_config_query_cache: dict[tuple[str, str | None, str, bool], PkgConfigResult] = {}
        # Sentinel: True means PKG_CONFIG_PATH was unset before override.
        # str means we saved that prior value. None means no override active.
        self._original_pkg_config_path: str | bool | None = None

    @contextlib.contextmanager
    def pkg_config_path_restored(self) -> Iterator[None]:
        """Scope within which any PKG_CONFIG_PATH mutation recorded on this
        context is undone at exit (success or exception).

        Safe to hold around code that may or may not apply overrides: the
        sentinel is recorded at apply time by
        ``build_apply.apply_effects`` (the ``SetEnv`` effect), and
        ``restore_pkg_config_path`` is a no-op when nothing was applied
        (``None`` sentinel). Preferred over calling
        ``restore_pkg_config_path`` directly.
        """
        try:
            yield
        finally:
            self.restore_pkg_config_path()

    def restore_pkg_config_path(self) -> None:
        """Undo any PKG_CONFIG_PATH mutation recorded on this context by
        ``build_apply.apply_effects`` (the ``SetEnv`` effect).

        Long-lived processes (test sessions, library embedders) that
        create more than one BuildContext should call this between
        contexts to avoid bleeding pkg-config state across builds.
        Prefer the ``pkg_config_path_restored()`` context manager, which
        pairs apply-scope and restore automatically.

        Unlocked, like the ``apply_effects`` write it undoes: both run
        single-threaded by call context. A lock on this half alone was
        never mutual exclusion -- it serialized restores against each
        other and against a writer that no longer exists, while the apply
        side wrote the same variable without taking it.
        """
        import os

        if self._original_pkg_config_path is True:
            os.environ.pop("PKG_CONFIG_PATH", None)
        elif isinstance(self._original_pkg_config_path, str):
            os.environ["PKG_CONFIG_PATH"] = self._original_pkg_config_path
        # else: nothing was applied; nothing to undo.
        self._original_pkg_config_path = None
