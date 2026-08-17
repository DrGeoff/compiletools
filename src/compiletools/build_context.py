"""Per-build-session state and caches.

BuildContext holds all per-build-session mutable state.  One BuildContext
is created per build invocation (by Cake or by tests) and threaded
through the object graph, so each build gets full isolation.

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
    from compiletools.preprocessing_cache import FileEffects, MacroCacheKey, ProcessingResult

# Type alias for headerdeps cache values: (include_list, FileEffects).
#
# Only include_list is derived here; everything a warm hit must replay
# (defines, undefs, function params, condition occurrences) travels as the
# producing ProcessingResult's FileEffects, applied via FileEffects.apply so
# this tier cannot drift from the preprocessing cache's replay contract.
# magicflags drives headerdeps inside its ``converging`` region, so this tier
# can serve the only settled-state evaluation a convergence sees; apply()'s
# occurrence replay is what retracts a pending "cannot evaluate" record from
# the convergence's own early pass.
IncludeCacheValue = tuple[tuple[sz.Str, ...], "FileEffects"]


class VerdictPartition:
    """Deferred condition/expansion verdicts for one root target's walks.

    One partition per root target (plus the ``None`` partition for
    unattributed walks). All four stores keep the shapes their former
    context-level counterparts had, so the preprocessor binds them
    unchanged:

    - ``pending`` — occurrence-keyed unevaluable-condition records, each a
      ``(message, audible, reason)`` triple (reason = the evaluator's error
      text, carried separately so the conflict report never re-parses the
      composed message), insertion-ordered so the flush prints in
      discovery order. Keyed by (filepath, directive_type, condition,
      line_num): reachability is position-dependent, and a position-free key
      would let a dead occurrence's retraction delete a live one's report.
      Recorded at EVERY verbosity — conflict classification must see the
      assume-false verdict even when the warning itself is inaudible —
      with ``audible`` carrying the verbose gate the flush honours.
    - ``resolved`` — occurrences some pass evaluated successfully, mapped to
      the truth value the evaluation produced. Sticky within the partition:
      passes interleave, and a pass that fails AFTER one that succeeded must
      not re-record. The truth value is what conflict classification needs —
      a target that resolves a shared gate TRUE genuinely diverges from a
      sibling that assumes it false, while a FALSE resolution merely
      coincides with the assumption.
    - ``pending_expansions`` / ``resolved_expansions`` — the expansion-cap
      pair, keyed (filepath, directive_type, line_num, kind, expression).
    """

    def __init__(self) -> None:
        self.pending: dict[tuple[str, str, str, int], tuple[str, bool, str]] = {}
        self.resolved: dict[tuple[str, str, str, int], bool] = {}
        self.pending_expansions: dict[tuple[str, str, int, str, str], str] = {}
        self.resolved_expansions: set[tuple[str, str, int, str, str]] = set()


def get_verdict_partition(context) -> VerdictPartition | None:
    """The partition for the context's current verdict root, created on demand.

    Returns None for a duck-typed context without ``verdict_partitions`` —
    such consumers (direct preprocessor driving in tests) keep the
    preprocessor's instance-local stores.
    """
    partitions = getattr(context, "verdict_partitions", None)
    if partitions is None:
        return None
    root = getattr(context, "current_verdict_root", None)
    partition = partitions.get(root)
    if partition is None:
        partition = partitions[root] = VerdictPartition()
    return partition


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
        # one per pass over the same file. Session-wide, NOT partitioned: it is
        # print-dedup, and the output contract is one line per condition per
        # file per build session.
        self.warned_preprocessor_conditions: set[tuple[str, str, str]] = set()

        # Deferred-diagnostic stores, PARTITIONED BY ROOT TARGET. Each
        # partition holds the verdicts recorded while walking one target's
        # closure (pre-fetch scan and settled build alike — same target, same
        # partition, so a scan's provisional record is retracted by that
        # target's own settled walk). Retraction never crosses partitions:
        # two targets' settled states are distinct final answers that coexist
        # in the product, not approximations of each other, and one target
        # resolving a shared header's gate must not silence another target's
        # genuine "cannot evaluate". The None partition collects verdicts
        # from walks with no target attribution (target discovery, direct
        # driving); it is retractable by ANY settled partition at flush and
        # never participates in conflict classification.
        self.verdict_partitions: dict[str | None, VerdictPartition] = {}
        self.current_verdict_root: str | None = None

        # Session-wide message library for unevaluable-condition records,
        # keyed by occurrence. A warm cache hit replays an "unevaluable"
        # occurrence into the CURRENT partition, and the replay needs the
        # message text the live run composed (it carries the evaluator's
        # reason, which cannot be reconstructed from the occurrence tuple).
        # Write-once per key; the value carries the audible flag (verbose
        # gate of the recording run) and the evaluator's reason alongside
        # the message.
        self.verdict_messages: dict[tuple[str, str, str, int], tuple[str, bool, str]] = {}

        # Depth of the enclosing convergence region. Zero means no pass will
        # follow, so a diagnostic is printed immediately and nothing is deferred;
        # every consumer that drives the preprocessor without one
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

    # The four historical store names, now views onto the CURRENT verdict
    # partition. Kept as properties so the preprocessor's duck-typed getattr
    # binding and the existing diagnostic tests keep working; the partition
    # boundary is what changed, not the store shapes' addressability.
    @property
    def pending_preprocessor_warnings(self) -> dict[tuple[str, str, str, int], tuple[str, bool, str]]:
        partition = get_verdict_partition(self)
        assert partition is not None
        return partition.pending

    @property
    def resolved_preprocessor_conditions(self) -> dict[tuple[str, str, str, int], bool]:
        partition = get_verdict_partition(self)
        assert partition is not None
        return partition.resolved

    @property
    def pending_preprocessor_expansion_warnings(self) -> dict[tuple[str, str, int, str, str], str]:
        partition = get_verdict_partition(self)
        assert partition is not None
        return partition.pending_expansions

    @property
    def resolved_preprocessor_expansions(self) -> set[tuple[str, str, int, str, str]]:
        partition = get_verdict_partition(self)
        assert partition is not None
        return partition.resolved_expansions
