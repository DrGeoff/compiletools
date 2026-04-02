"""Per-build-session state and caches.

BuildContext replaces module-level singletons by holding all mutable
state that was previously stored in module globals.  One BuildContext
is created per build invocation (by Cake or by tests) and threaded
through the object graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from compiletools.file_analyzer import FileAnalysisResult
    from compiletools.preprocessing_cache import MacroCacheKey, ProcessingResult


class BuildContext:
    """Holds all per-build-session state and caches.

    Create one at the start of a build and pass it through the call chain.
    When the build (or test) is done, discard the context — all caches are
    garbage-collected with it.
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

        # -- headerdeps module-level caches --
        self.include_list_cache: dict[tuple[str, Any], Any] = {}
        self.invariant_include_cache: dict[str, Any] = {}

        # -- file_analyzer state --
        self.analyzer_args: Any = None
        self.file_reading_strategy: str | None = None
        self.warned_low_ulimit: bool = False
        self.warned_mmap_failure: bool = False
        self.analyze_file_cache: dict[str, FileAnalysisResult] = {}

        # -- git_sha_report state --
        self.repo_has_symlinks: bool | None = None
