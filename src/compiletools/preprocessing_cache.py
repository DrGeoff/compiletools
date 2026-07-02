"""Unified preprocessing cache for compiletools.

This module provides a centralized cache for preprocessing results that can be
shared across SimplePreprocessor, DirectMagicFlags, and CppHeaderDeps.

The cache uses two strategies:
1. Macro-invariant files (no conditionals): cached by content_hash only
2. Macro-variant files (has conditionals): cached by (content_hash, macro_cache_key)

This optimizes the common case where files have #define but no #if/#ifdef.

IMPORTANT: MacroState.get_hash() uses stringzilla's deterministic hash function
for O(n) performance without sorting. XOR combination ensures order independence.
The hash is deterministic across Python runs, enabling future disk caching support.
"""

import sys
from dataclasses import dataclass, field
from typing import Optional

import stringzilla as sz

# Type aliases for macro dictionaries and cache keys
MacroDict = dict[sz.Str, sz.Str]
MacroCacheKey = frozenset[tuple[sz.Str, sz.Str]]


@dataclass
class ProcessingResult:
    """Result of preprocessing a file with conditional compilation.

    Attributes:
        active_lines: Line numbers that are active after preprocessing (0-based)
        active_includes: List of active #include directives with metadata
        active_magic_flags: List of active magic flags with metadata
        active_defines: List of active #define directives with metadata
        updated_macros: Macro state after processing (input + defines - undefs)
        file_defines: Macros defined BY this file only (for cache reconstruction)
    """

    active_lines: list[int]
    active_includes: list[dict]
    active_magic_flags: list[dict]
    active_defines: list[dict]
    updated_macros: "MacroState"  # Forward reference
    file_defines: MacroDict = field(default_factory=dict)
    file_undefs: frozenset = field(default_factory=frozenset)


@dataclass
class MacroState:
    """Structured macro state and build context for preprocessing.

    Separates static (core) from dynamic (variable) macros, reducing cache key
    computation cost by ~80% by avoiding repeated hashing of unchanging macros.

    Also carries build context fields (compiler_path, cppflags, cflags, cxxflags)
    — these are not macros, but they are build-invariant context.  compiler_path
    and cppflags are needed by the preprocessor to evaluate __has_* functions
    (e.g., __has_include(<iostream>)).  All four fields live here rather than
    being threaded as separate parameters because MacroState already flows
    through the entire preprocessing pipeline.  Like core macros, they are set
    once at construction and propagated automatically by with_updates()/
    without_keys().

    Build context fields are included in the full hash (include_core=True) used
    for object file naming, so that different compilers, include paths, or
    optimization flags produce different object names.  They are NOT included in
    preprocessing cache keys (include_core=False) because the compiler and flags
    are constant per-process.

    Not a dict: callers that want to read macros must do so through the explicit
    attributes (``.core``, ``.variable``) or ``all_macros()`` for the merged view.
    The narrow API surface keeps the immutability story tight and avoids tempting
    callers to mutate or hash macros without going through ``get_cache_key()`` /
    ``get_hash()``.

    Attributes:
        core: Static macros (compiler built-ins + cmdline -D flags). ~388 macros.
              These never change during a build, so we exclude them from cache keys.
        variable: Dynamic macros accumulated from #define directives in files.
                  These grow as files are processed and determine cache behavior.
        compiler_path: Compiler executable (e.g., 'gcc') for __has_* evaluation.
                       Not a macro — build context carried for convenience.
        cppflags: Raw preprocessor flags (e.g., '-I/usr/include').  The -I paths
                  are needed so __has_include can search the right directories.
                  Not macros — only the -D portions are extracted into core.
        cflags: Raw C compiler flags (e.g., '-O2 -fPIC').  Hashed for object
                naming so different optimization levels produce different objects.
        cxxflags: Raw C++ compiler flags (e.g., '-std=c++17').  Hashed for object
                  naming so different C++ standards produce different objects.
        cmdline_origin: Names in `core` that came from cmdline -D flags (vs
                        compiler built-ins). When non-empty, callers can pass a
                        scope_filter to get_hash(include_core=True) so only the
                        cmdline -D macros actually referenced by the TU are
                        included in the hash. Default empty: no filtering.
        cppflags_tokens: Optional structured token list for cppflags with -D/-U
                         already stripped. When provided, get_hash() hashes the
                         tokens instead of the raw cppflags string (so the -D
                         tokens that scope_filter is meant to filter don't sneak
                         back in via the build-context hash). None = today's
                         raw-string hashing.
        cflags_tokens: Same idea for cflags.
        cxxflags_tokens: Same idea for cxxflags.
        compiler_identity: Stable identity string for the compiler binary
                           (realpath|size|mtime_ns) produced by
                           ``apptools.compiler_identity``. Folded into the
                           build-context portion of the include_core hash so
                           an in-place toolchain swap that does not change
                           the user-visible ``compiler_path`` (e.g. ``g++``)
                           still invalidates stale objects. Default ``""``
                           preserves backward compat for tests that don't
                           set it.  Symmetric with the PCH cache key in
                           ``build_backend._pch_command_hash``.
    """

    core: MacroDict  # Static: compiler + cmdline macros
    variable: MacroDict  # Dynamic: file #defines
    compiler_path: str  # Build context: compiler executable for __has_* queries
    cppflags: str  # Build context: raw flags (-I paths etc.) for __has_* queries
    cflags: str  # Build context: C compiler flags for object naming
    cxxflags: str  # Build context: C++ compiler flags for object naming
    cmdline_origin: frozenset  # Names in core that came from cmdline -D flags
    cppflags_tokens: Optional[list]  # Structured cppflags tokens (-D/-U stripped)
    cflags_tokens: Optional[list]  # Structured cflags tokens (-D/-U stripped)
    cxxflags_tokens: Optional[list]  # Structured cxxflags tokens (-D/-U stripped)
    compiler_identity: str  # Build context: compiler binary identity (realpath|size|mtime_ns)
    anchor_root: str  # Build context: gitroot used to canonicalize -I paths in the hash
    _cache_key: Optional[MacroCacheKey]  # Cached frozenset for cache keys
    _hash: Optional[str]  # Cached hex digest for convergence detection (variable only)
    _hash_full: Optional[str]  # Cached hex digest including core + variable + build context
    _build_context_hash: Optional[int]  # Cached sz.hash() of the canonicalised build_context block

    def __init__(
        self,
        core: MacroDict,
        variable: Optional[MacroDict] = None,
        compiler_path: str = "",
        cppflags: str = "",
        cflags: str = "",
        cxxflags: str = "",
        cmdline_origin: frozenset = frozenset(),
        cppflags_tokens: Optional[list] = None,
        cflags_tokens: Optional[list] = None,
        cxxflags_tokens: Optional[list] = None,
        compiler_identity: str = "",
        *,
        anchor_root: str,  # required: gitroot for canonicalisation; pass "" only in tests
    ):
        """Initialize macro state.

        Args:
            core: Static macros (compiler built-ins + cmdline flags)
            variable: Dynamic macros (file defines). Defaults to empty dict.
            compiler_path: Compiler executable for evaluating __has_* functions
            cppflags: Additional preprocessor flags forwarded to __has_* queries
            cflags: C compiler flags (e.g., '-O2') for object naming hash
            cxxflags: C++ compiler flags (e.g., '-std=c++17') for object naming hash
            cmdline_origin: Names in `core` that came from cmdline -D flags
                (the rest of `core` is compiler built-ins). Default empty.
            cppflags_tokens: Optional tokenized cppflags with -D/-U stripped.
                None falls back to hashing the raw cppflags string.
            cflags_tokens: Optional tokenized cflags with -D/-U stripped.
            cxxflags_tokens: Optional tokenized cxxflags with -D/-U stripped.
            compiler_identity: Stable identity for the compiler binary
                (realpath|size|mtime_ns) from ``apptools.compiler_identity``.
                Folded into the include_core hash so an in-place toolchain
                swap invalidates objects. Default ``""`` when not applicable.
            anchor_root: Gitroot prefix used by ``canonicalize_path_for_cache_key``
                to make flag-token hashes workspace-independent. Pass ``""``
                only in tests or when the gitroot cannot be resolved (graceful
                no-op). Required: omitting it silently re-introduces the
                gitroot-leak bug the canonicaliser exists to prevent.
        """
        self.core = core
        self.variable = variable if variable is not None else {}
        self.compiler_path = compiler_path
        self.cppflags = cppflags
        self.cflags = cflags
        self.cxxflags = cxxflags
        self.cmdline_origin = cmdline_origin
        self.cppflags_tokens = cppflags_tokens
        self.cflags_tokens = cflags_tokens
        self.cxxflags_tokens = cxxflags_tokens
        self.compiler_identity = compiler_identity
        self.anchor_root = anchor_root
        self._cache_key = None  # Lazy-computed cache key
        self._hash = None  # Lazy-computed hash (variable only)
        self._hash_full = None  # Lazy-computed hash (core + variable + build context)
        self._build_context_hash = None  # Lazy-computed sz.hash() of build_context block

    def all_macros(self) -> MacroDict:
        """Get merged view of all macros (core + variable).

        Returns:
            Dictionary containing all macros. Variable macros override core if conflicts.
        """
        result = self.core.copy()
        result.update(self.variable)
        return result

    def with_updates(self, new_macros: MacroDict) -> "MacroState":
        """Create new MacroState with additional macros merged into variable.

        Args:
            new_macros: Macros to merge (typically from file #defines)

        Returns:
            New MacroState with same core but updated variable macros.
            Returns self if new_macros is empty or contains no effective changes.
        """
        # Short-circuit: if no new macros, return self to preserve cached state
        if not new_macros:
            return self

        # Filter out no-op updates to ensure immutability efficiency
        # Only apply updates that actually change the value or add a new key
        actual_updates = {k: v for k, v in new_macros.items() if k not in self.variable or self.variable[k] != v}

        if not actual_updates:
            return self

        updated_variable = self.variable.copy()
        updated_variable.update(actual_updates)
        new_state = MacroState(
            self.core,
            updated_variable,
            compiler_path=self.compiler_path,
            cppflags=self.cppflags,
            cflags=self.cflags,
            cxxflags=self.cxxflags,
            cmdline_origin=self.cmdline_origin,
            cppflags_tokens=self.cppflags_tokens,
            cflags_tokens=self.cflags_tokens,
            cxxflags_tokens=self.cxxflags_tokens,
            compiler_identity=self.compiler_identity,
            anchor_root=self.anchor_root,
        )

        # Optimization: incrementally compute cache key when possible
        # Only for pure additions (no key overwrites) since frozenset union
        # doesn't replace - it adds. Macro definitions are typically additive
        # (include guards, feature flags), so pure additions are the common case.
        if self._cache_key is not None:
            overwrites = any(k in self.variable for k in actual_updates)
            if not overwrites:
                # Pure addition - O(k) frozenset union instead of O(n) rebuild
                new_state._cache_key = self._cache_key | frozenset(actual_updates.items())

        return new_state

    def without_keys(self, keys) -> "MacroState":
        """Create new MacroState with specified keys removed from variable."""
        removed = {k for k in keys if k in self.variable}
        if not removed:
            return self
        updated_variable = {k: v for k, v in self.variable.items() if k not in removed}
        return MacroState(
            self.core,
            updated_variable,
            compiler_path=self.compiler_path,
            cppflags=self.cppflags,
            cflags=self.cflags,
            cxxflags=self.cxxflags,
            cmdline_origin=self.cmdline_origin,
            cppflags_tokens=self.cppflags_tokens,
            cflags_tokens=self.cflags_tokens,
            cxxflags_tokens=self.cxxflags_tokens,
            compiler_identity=self.compiler_identity,
            anchor_root=self.anchor_root,
        )

    def get_cached_key_if_available(self) -> Optional[MacroCacheKey]:
        """Get cache key if already computed, None otherwise.

        Use this to avoid recomputing the cache key when it might already be available.
        Useful in hot paths where you want to check before computing.

        Returns:
            Cached frozenset if available, None if not yet computed
        """
        return self._cache_key

    def get_cache_key(self) -> MacroCacheKey:
        """Get or compute cache key for this MacroState.

        Returns cached key if available, otherwise computes and caches it.
        """
        if not self.variable:
            return _EMPTY_FROZENSET

        if self._cache_key is None:
            self._cache_key = frozenset(self.variable.items())

        return self._cache_key

    def get_relevant_key(self, relevant_macros: frozenset[sz.Str]) -> MacroCacheKey:
        """Get cache key filtered to only macros that affect the target file.

        For variant caching, only macros referenced in conditionals (#ifdef, #if, etc.)
        can affect preprocessing. Other macros in the state are irrelevant for this file
        and should not create unique cache keys.

        Args:
            relevant_macros: Set of macro names from file_result.conditional_macros

        Returns:
            Frozenset of (name, value) pairs for only the relevant variable macros
        """
        if not relevant_macros:
            return _EMPTY_FROZENSET

        # Build filtered key - only include variable macros that matter
        relevant_items = tuple((m, self.variable[m]) for m in relevant_macros if m in self.variable)
        return frozenset(relevant_items) if relevant_items else _EMPTY_FROZENSET

    def get_hash(
        self,
        include_core: bool = False,
        scope_filter: Optional[frozenset] = None,
    ) -> str:
        """Get or compute stable hash of this MacroState for convergence detection.

        Args:
            include_core: If True, include core macros + build context in hash.
                If False (default), only hash variable macros (preprocessing cache).
            scope_filter: Optional set of cmdline -D macro names to include from
                `core`. Names in `core` but NOT in `cmdline_origin` (compiler
                builtins) are always hashed; names in BOTH `core` and
                `cmdline_origin` are hashed only if they appear in scope_filter.
                Variable macros are always hashed (unaffected by scope_filter).
                Ignored when include_core is False. None preserves today's
                behavior (no filtering — all of core is hashed).

        Returns a hex string of stable 64-bit hash using stringzilla's deterministic hash.
        Hash is deterministic across Python runs (suitable for disk caching).
        Uses cached hash to avoid recomputation on repeated calls (only for the
        unfiltered include_core path; filtered hashes are not cached because
        each TU may pass a different scope_filter).

        INVARIANT: equal cache keys produce equal hashes (1-to-1 mapping)
        Performance: O(n) with no sorting - XOR is commutative so order doesn't matter
        """
        # Variable-only path (preprocessing cache) — unaffected by scope_filter.
        if not include_core:
            if self._hash is not None:
                return self._hash
            combined = 0
            for name, value in self.get_cache_key():
                combined ^= sz.hash(bytes(name))
                combined ^= sz.hash(bytes(value))
            self._hash = format(combined, "016x")
            return self._hash

        # Full hash path. Cache only the unfiltered call.
        if scope_filter is None and self._hash_full is not None:
            return self._hash_full

        combined = self._compute_full_hash_combined(scope_filter)
        result = format(combined, "016x")
        if scope_filter is None:
            self._hash_full = result
        return result

    def _compute_full_hash_combined(self, scope_filter: Optional[frozenset]) -> int:
        """Compute the XOR-combined 64-bit hash for the full (include_core) path.

        Filters cmdline-origin core macros against scope_filter when provided,
        and hashes structured flag tokens instead of raw flag strings when
        tokens are available.
        """
        combined = 0
        # Core + variable macros. Match original behavior: dedup (name, value)
        # pairs via frozenset so that duplicates between core and variable
        # don't cancel under XOR.
        if scope_filter is None or not self.cmdline_origin:
            items_to_hash = frozenset(list(self.core.items()) + list(self.variable.items()))
        else:
            filtered_core = [(n, v) for n, v in self.core.items() if n not in self.cmdline_origin or n in scope_filter]
            items_to_hash = frozenset(filtered_core + list(self.variable.items()))
        for name, value in items_to_hash:
            combined ^= sz.hash(bytes(name))
            combined ^= sz.hash(bytes(value))

        combined ^= self._get_build_context_hash()
        return combined

    def _get_build_context_hash(self) -> int:
        """Hash of the canonicalised build_context block (compiler path,
        identity, flag tokens). All inputs are MacroState invariants —
        cache so the scope-filtered hash path doesn't re-canonicalise on
        every per-TU call.
        """
        cached = self._build_context_hash
        if cached is not None:
            return cached

        # Deferred import: apptools transitively pulls in many modules and
        # preprocessing_cache is imported very early at startup.
        from compiletools.apptools import (
            canonicalize_for_cache_key,
            canonicalize_path_for_cache_key,
            filter_hash_irrelevant_tokens,
            tokenize_compile_flags,
        )

        # tokenize_compile_flags accepts raw strings OR pre-tokenized lists
        # (idempotent on the latter — it strips -D/-U which upstream callers
        # already stripped), so passing whichever the caller populated works.
        cpp_in = self.cppflags if self.cppflags_tokens is None else self.cppflags_tokens
        c_in = self.cflags if self.cflags_tokens is None else self.cflags_tokens
        cxx_in = self.cxxflags if self.cxxflags_tokens is None else self.cxxflags_tokens
        cppflags_tokens, cflags_tokens, cxxflags_tokens = tokenize_compile_flags(cpp_in, c_in, cxx_in)

        def _canon(toks):
            return canonicalize_for_cache_key(filter_hash_irrelevant_tokens(toks), self.anchor_root)

        cppflags_part = "CPPFLAGS_TOKENS=" + "\x00".join(_canon(cppflags_tokens))
        cflags_part = "CFLAGS_TOKENS=" + "\x00".join(_canon(cflags_tokens))
        cxxflags_part = "CXXFLAGS_TOKENS=" + "\x00".join(_canon(cxxflags_tokens))

        canonical_cc = canonicalize_path_for_cache_key(self.compiler_path, self.anchor_root)
        build_context = (
            f"CC={canonical_cc}\x00"
            f"COMPILER_IDENTITY={self.compiler_identity}\x00"
            f"{cppflags_part}\x00{cflags_part}\x00{cxxflags_part}"
        )
        result = int(sz.hash(bytes(build_context, "utf-8")))
        self._build_context_hash = result
        return result


# Simple cache: if variable dict is empty, return cached empty frozenset
_EMPTY_FROZENSET: MacroCacheKey = frozenset()


def is_permanently_invariant(file_result) -> bool:
    """Determine if a file is permanently invariant (no conditionals).

    Files with no conditional compilation directives are always invariant
    regardless of macro state. They can be processed once and never need
    reprocessing during convergence iterations.

    Args:
        file_result: FileAnalysisResult with conditional_macros field

    Returns:
        True if file has no conditionals at all
    """
    return not file_result.conditional_macros


def is_macro_invariant(file_result, input_macros: "MacroState") -> bool:
    """Determine if a file's active lines are independent of current macro state.

    A file is effectively invariant if none of its conditional macros are currently defined
    in the VARIABLE macros. We only check variable macros because core macros (compiler
    built-ins + cmdline) are identical for all files in a build.

    Examples of effectively invariant files:
    - Headers with #ifdef __GNUC__ when __GNUC__ is in core (always invariant for that file)
    - Files with platform checks that don't match current build
    - Headers with only #define, #include, #pragma (no conditionals at all)

    Args:
        file_result: FileAnalysisResult with conditional_macros field
        input_macros: MacroState with current macro state

    Returns:
        True if none of the file's conditional macros are defined in variable macros
    """
    # If file has no conditionals at all, it's always invariant
    if is_permanently_invariant(file_result):
        return True

    # Only check variable macros - core macros are the same for all files
    return not any(m in input_macros.variable for m in file_result.conditional_macros)


# Dual cache strategy:
# 1. Invariant cache: content_hash -> ProcessingResult (for files without conditionals)
# 2. Variant cache: (content_hash, macro_frozenset) -> ProcessingResult (for files with conditionals)
#
# NOTE: We use manual caching instead of @lru_cache because:
# 1. Function arguments (FileAnalysisResult, Dict) are not hashable
# 2. Cache key must be extracted from file_result and macros
# 3. We need full objects to compute results, not just hashes
# 4. Provides enhanced debugging (dump_cache_keys with file path resolution)
def get_or_compute_preprocessing(
    file_result,
    input_macros: "MacroState",
    verbose: int = 0,
    *,
    context,
) -> ProcessingResult:
    """Get preprocessing result from cache or compute if not cached.

    Uses dual cache strategy:
    - Macro-invariant files: cached by content_hash only
    - Macro-variant files: cached by (content_hash, macro_cache_key)

    IMPORTANT: Caller must propagate macro state across files:
        result1 = get_or_compute_preprocessing(file1, initial_macros, verbose)
        result2 = get_or_compute_preprocessing(file2, result1.updated_macros, verbose)

    Args:
        file_result: FileAnalysisResult with file content and metadata
        input_macros: MacroState with current macro state for this file
            (compiler_path and cppflags are read from MacroState for __has_* evaluation)
        verbose: Verbosity level for debugging

    Returns:
        ProcessingResult with active lines, includes, magic flags, defines, and updated MacroState
    """
    from compiletools.simple_preprocessor import SimplePreprocessor

    inv_cache = context.invariant_preprocessing_cache
    var_cache = context.variant_preprocessing_cache
    stats = context.preprocessing_stats

    stats["total_calls"] += 1

    content_hash = file_result.content_hash
    invariant = is_macro_invariant(file_result, input_macros)
    cache_key: tuple = ()

    # Check appropriate cache
    if invariant:
        # Macro-invariant: cache key is content_hash only
        if content_hash in inv_cache:
            stats["hits"] += 1
            stats["invariant_hits"] += 1
            cached = inv_cache[content_hash]
            # Reconstruct updated_macros from caller's input + file's defines
            # to prevent stale macro pollution from first caller's context
            reconstructed_macros = input_macros
            if cached.file_defines:
                reconstructed_macros = reconstructed_macros.with_updates(cached.file_defines)
            if cached.file_undefs:
                reconstructed_macros = reconstructed_macros.without_keys(cached.file_undefs)
            return ProcessingResult(
                active_lines=cached.active_lines,
                active_includes=cached.active_includes,
                active_magic_flags=cached.active_magic_flags,
                active_defines=cached.active_defines,
                updated_macros=reconstructed_macros,
                file_defines=cached.file_defines,
                file_undefs=cached.file_undefs,
            )

        stats["misses"] += 1
        stats["invariant_misses"] += 1
    else:
        # Macro-variant: cache key is (content_hash, file_specific_macro_key)
        # Use file-specific key: only macros that affect this file's conditionals
        macro_key = input_macros.get_relevant_key(file_result.conditional_macros)
        cache_key = (content_hash, macro_key)

        if cache_key in var_cache:
            stats["hits"] += 1
            stats["variant_hits"] += 1
            cached = var_cache[cache_key]
            reconstructed_macros = input_macros
            if cached.file_defines:
                reconstructed_macros = reconstructed_macros.with_updates(cached.file_defines)
            if cached.file_undefs:
                reconstructed_macros = reconstructed_macros.without_keys(cached.file_undefs)
            return ProcessingResult(
                active_lines=cached.active_lines,
                active_includes=cached.active_includes,
                active_magic_flags=cached.active_magic_flags,
                active_defines=cached.active_defines,
                updated_macros=reconstructed_macros,
                file_defines=cached.file_defines,
                file_undefs=cached.file_undefs,
            )

        stats["misses"] += 1
        stats["variant_misses"] += 1

    # Compute result - pass all macros to preprocessor
    all_macros = input_macros.all_macros()
    preprocessor = SimplePreprocessor(
        all_macros, verbose=verbose, compiler_path=input_macros.compiler_path, cppflags=input_macros.cppflags
    )
    active_lines = preprocessor.process_structured(file_result, context)
    active_line_set = set(active_lines)

    # Extract active includes
    active_includes = []
    for inc in file_result.includes:
        if inc["line_num"] in active_line_set:
            active_includes.append(inc)

    # Resolve computed includes from directives
    for directive in file_result.directives:
        if directive.directive_type == "include" and directive.line_num in active_line_set and directive.condition:
            resolved = preprocessor.resolve_computed_include(directive.condition)
            if resolved:
                active_includes.append(
                    {
                        "line_num": directive.line_num,
                        "filename": sz.Str(resolved),
                        "is_system": False,
                        "is_commented": False,
                    }
                )

    # Extract active magic flags
    active_magic_flags = []
    for magic in file_result.magic_flags:
        if magic["line_num"] in active_line_set:
            active_magic_flags.append(magic)

    # Extract active defines
    active_defines = []
    for define in file_result.defines:
        if define["line_num"] in active_line_set:
            active_defines.append(define)

    # Build updated MacroState from preprocessor results
    # Core stays the same, variable gets new defines from this file
    new_variable_macros = {}
    for k, v in preprocessor.macros.items():
        # Only add to variable if not in core
        if k not in input_macros.core:
            new_variable_macros[k] = v

    # Store file-specific defines for cache reconstruction
    # file_defines should ONLY contain macros defined BY this file (not inherited from input)
    # Note: Include guards are already excluded by SimplePreprocessor._handle_define_structured()
    file_defines: MacroDict = {}
    for k, v in new_variable_macros.items():
        if k not in input_macros.variable:
            file_defines[k] = v

    # Active undef targets: macro names from #undef directives on active lines.
    # Input-independent: safe to cache for both invariant and variant entries.
    # without_keys() handles the intersection with the caller's variable macros.
    file_undefs = frozenset(
        d.macro_name
        for d in file_result.directives
        if d.directive_type == "undef" and d.macro_name and d.line_num in active_line_set
    )

    # Build updated state: new_variable_macros already reflects the correct
    # post-preprocessing state (input macros + file defines - file undefs)
    updated_macro_state = MacroState(
        input_macros.core,
        new_variable_macros,
        compiler_path=input_macros.compiler_path,
        cppflags=input_macros.cppflags,
        cflags=input_macros.cflags,
        cxxflags=input_macros.cxxflags,
        cmdline_origin=input_macros.cmdline_origin,
        cppflags_tokens=input_macros.cppflags_tokens,
        cflags_tokens=input_macros.cflags_tokens,
        cxxflags_tokens=input_macros.cxxflags_tokens,
        compiler_identity=input_macros.compiler_identity,
        anchor_root=input_macros.anchor_root,
    )

    # Create result
    result = ProcessingResult(
        active_lines=active_lines,
        active_includes=active_includes,
        active_magic_flags=active_magic_flags,
        active_defines=active_defines,
        updated_macros=updated_macro_state,
        file_defines=file_defines,
        file_undefs=file_undefs,
    )

    # Store in appropriate cache
    if invariant:
        inv_cache[content_hash] = result
    else:
        var_cache[cache_key] = result

    return result


def get_cache_stats(context) -> dict:
    """Return cache statistics for debugging and monitoring.

    Returns:
        Dictionary with cache metrics:
        - entries: Total number of cached results
        - invariant_entries: Number of macro-invariant cache entries
        - variant_entries: Number of macro-variant cache entries
        - hits: Number of cache hits
        - invariant_hits: Number of invariant cache hits
        - variant_hits: Number of variant cache hits
        - misses: Number of cache misses
        - invariant_misses: Number of invariant cache misses
        - variant_misses: Number of variant cache misses
        - total_calls: Total calls to get_or_compute_preprocessing
        - hit_rate: Percentage of cache hits (0-100)
        - memory_bytes: Approximate memory usage
        - memory_mb: Memory usage in MB
    """
    inv_c = context.invariant_preprocessing_cache
    var_c = context.variant_preprocessing_cache
    st = context.preprocessing_stats

    # sys.getsizeof always raises TypeError on PyPy unless a default is supplied;
    # memory_bytes is documented as approximate, so 0 is fine as the fallback.
    total_size = 0
    for result in inv_c.values():
        total_size += sys.getsizeof(result.active_lines, 0)
        total_size += sys.getsizeof(result.active_includes, 0)
        total_size += sys.getsizeof(result.active_magic_flags, 0)
        total_size += sys.getsizeof(result.active_defines, 0)
        total_size += sys.getsizeof(result.updated_macros, 0)

    for result in var_c.values():
        total_size += sys.getsizeof(result.active_lines, 0)
        total_size += sys.getsizeof(result.active_includes, 0)
        total_size += sys.getsizeof(result.active_magic_flags, 0)
        total_size += sys.getsizeof(result.active_defines, 0)
        total_size += sys.getsizeof(result.updated_macros, 0)

    hit_rate = 0.0
    if st["total_calls"] > 0:
        hit_rate = (st["hits"] / st["total_calls"]) * 100

    return {
        "entries": len(inv_c) + len(var_c),
        "invariant_entries": len(inv_c),
        "variant_entries": len(var_c),
        "hits": st["hits"],
        "invariant_hits": st["invariant_hits"],
        "variant_hits": st["variant_hits"],
        "misses": st["misses"],
        "invariant_misses": st["invariant_misses"],
        "variant_misses": st["variant_misses"],
        "total_calls": st["total_calls"],
        "hit_rate": hit_rate,
        "memory_bytes": total_size,
        "memory_mb": total_size / (1024 * 1024),
    }


def clear_cache(context):
    """Clear the preprocessing cache and reset statistics on the given context.

    In production code, creating a fresh BuildContext is preferred over clearing.
    This function exists for tests that need to reset mid-test.
    """
    context.invariant_preprocessing_cache.clear()
    context.variant_preprocessing_cache.clear()
    for key in context.preprocessing_stats:
        context.preprocessing_stats[key] = 0


def clear_variant_cache(context):
    """Clear only the macro-variant preprocessing cache.

    Used during two-pass header discovery to ensure Pass 2 gets fresh results
    with converged macros. The invariant cache is preserved since those files
    have no conditionals and their results are truly macro-independent.
    """
    context.variant_preprocessing_cache.clear()


def print_preprocessing_stats(context):
    """Print preprocessing cache and SimplePreprocessor statistics."""
    stats = get_cache_stats(context)

    print("\n=== Preprocessing Cache Statistics ===")
    print(f"Total preprocessing calls: {stats['total_calls']}")
    print(f"Cache hits: {stats['hits']}")
    print(f"Cache misses: {stats['misses']}")
    print(f"Cache hit rate: {stats['hit_rate']:.1f}%")
    print("\nCache entries:")
    print(f"  Invariant entries: {stats['invariant_entries']}")
    print(f"  Variant entries: {stats['variant_entries']}")
    print(f"  Total entries: {stats['entries']}")
    print("\nHit breakdown:")
    print(f"  Invariant hits: {stats['invariant_hits']}")
    print(f"  Variant hits: {stats['variant_hits']}")
    print("\nMiss breakdown:")
    print(f"  Invariant misses: {stats['invariant_misses']}")
    print(f"  Variant misses: {stats['variant_misses']}")

    # Print SimplePreprocessor call statistics
    from compiletools.simple_preprocessor import print_preprocessor_stats

    print_preprocessor_stats()
