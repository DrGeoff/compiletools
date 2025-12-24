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

from typing import List, Dict, Tuple, FrozenSet, Optional
from dataclasses import dataclass, field
import sys
import stringzilla as sz


# Type aliases for macro dictionaries and cache keys
MacroDict = Dict[sz.Str, sz.Str]
MacroCacheKey = FrozenSet[Tuple[sz.Str, sz.Str]]


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
    active_lines: List[int]
    active_includes: List[dict]
    active_magic_flags: List[dict]
    active_defines: List[dict]
    updated_macros: 'MacroState'  # Forward reference
    file_defines: MacroDict = field(default_factory=dict)


@dataclass
class MacroState:
    """Structured macro state separating static (core) from dynamic (variable) macros.

    This optimization reduces cache key computation cost by ~80% by avoiding
    repeated hashing of unchanging macros (compiler built-ins + cmdline flags).

    Acts as a dict-like container that can be used as a drop-in replacement for
    plain macro dictionaries, but with optimized caching behavior.

    Attributes:
        core: Static macros (compiler built-ins + cmdline -D flags). ~388 macros.
              These never change during a build, so we exclude them from cache keys.
        variable: Dynamic macros accumulated from #define directives in files.
                  These grow as files are processed and determine cache behavior.
        _version: Integer counter incremented on any mutation to variable macros.
                  Used for fast convergence detection without expensive cache key comparisons.
                  IMPORTANT: Any new mutation pathway must increment this counter.
    """
    core: MacroDict  # Static: compiler + cmdline macros
    variable: MacroDict  # Dynamic: file #defines
    _cache_key: Optional[MacroCacheKey]  # Cached frozenset for cache keys
    _hash: Optional[str]  # Cached hex digest for convergence detection (variable only)
    _hash_full: Optional[str]  # Cached hex digest including core + variable
    _version: int  # Version counter incremented on any mutation for fast equality checks

    def __init__(self, core: MacroDict, variable: Optional[MacroDict] = None):
        """Initialize macro state.

        Args:
            core: Static macros (compiler built-ins + cmdline flags)
            variable: Dynamic macros (file defines). Defaults to empty dict.
        """
        self.core = core
        self.variable = variable if variable is not None else {}
        self._cache_key = None  # Lazy-computed cache key
        self._hash = None  # Lazy-computed hash (variable only)
        self._hash_full = None  # Lazy-computed hash (core + variable)
        self._version = 0  # Increments on mutations for fast convergence detection

    def all_macros(self) -> MacroDict:
        """Get merged view of all macros (core + variable).

        Returns:
            Dictionary containing all macros. Variable macros override core if conflicts.
        """
        result = self.core.copy()
        result.update(self.variable)
        return result

    def with_updates(self, new_macros: MacroDict) -> 'MacroState':
        """Create new MacroState with additional macros merged into variable.

        Args:
            new_macros: Macros to merge (typically from file #defines)

        Returns:
            New MacroState with same core but updated variable macros
        """
        updated_variable = self.variable.copy()
        updated_variable.update(new_macros)
        return MacroState(self.core, updated_variable)

    # Dict-like interface for easy drop-in replacement
    def __len__(self) -> int:
        """Return total number of macros (core + variable)."""
        return len(self.core) + len(self.variable)

    def __getitem__(self, key):
        """Get macro value by key. Variable overrides core."""
        if key in self.variable:
            return self.variable[key]
        return self.core[key]

    def __setitem__(self, key, value):
        """Set macro value. Always sets in variable dict."""
        # Only mutate and increment version if value actually changed
        if key not in self.variable or self.variable[key] != value:
            self.variable[key] = value
            self._version += 1  # Increment version for convergence detection
            self._cache_key = None  # Invalidate caches
            self._hash = None
            self._hash_full = None  # Invalidate full hash too

    def __contains__(self, key) -> bool:
        """Check if macro key exists in either core or variable."""
        return key in self.variable or key in self.core

    def get(self, key, default=None):
        """Get macro value with optional default."""
        if key in self.variable:
            return self.variable[key]
        return self.core.get(key, default)

    def keys(self):
        """Return all macro keys (core + variable)."""
        all_keys = set(self.core.keys())
        all_keys.update(self.variable.keys())
        return all_keys

    def items(self):
        """Return all macro items (core + variable, variable overrides)."""
        return self.all_macros().items()

    def values(self):
        """Return all macro values (core + variable)."""
        return self.all_macros().values()

    def update(self, other: 'MacroState'):
        """Update variable macros from another MacroState.

        Optimized to skip redundant updates using version comparison.
        Only increments version when actual changes occur.
        """
        # Early exit if nothing to update
        if not other.variable:
            return

        # Check if any actual changes would occur
        has_changes = False
        for key, value in other.variable.items():
            if key not in self.variable or self.variable[key] != value:
                has_changes = True
                break

        if not has_changes:
            return  # No changes, skip update

        # Perform update and increment version
        self.variable.update(other.variable)
        self._version += 1  # Increment version on mutation
        self._cache_key = None
        self._hash = None
        self._hash_full = None  # Invalidate full hash too

    def copy(self) -> 'MacroState':
        """Create shallow copy of this MacroState."""
        result = MacroState(self.core, self.variable.copy())
        result._version = self._version  # Preserve version in copy
        return result

    def get_version(self) -> int:
        """Get version counter for fast convergence detection.

        The version increments whenever variable macros are modified.
        Used to detect convergence without expensive cache key comparisons.

        Returns:
            Integer version counter (starts at 0, increments on mutations)
        """
        return self._version

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

    def get_hash(self, include_core=False) -> str:
        """Get or compute stable hash of this MacroState for convergence detection.

        Args:
            include_core: If True, include core macros in hash. If False (default),
                         only hash variable macros for preprocessing cache compatibility.

        Returns a hex string of stable 64-bit hash using stringzilla's deterministic hash.
        Hash is deterministic across Python runs (suitable for disk caching).
        Uses cached hash to avoid recomputation on repeated calls.

        INVARIANT: equal cache keys produce equal hashes (1-to-1 mapping)
        Performance: O(n) with no sorting - XOR is commutative so order doesn't matter
        """
        # Use separate cache attributes for variable-only vs full hash
        cache_attr = '_hash_full' if include_core else '_hash'

        cached_value = getattr(self, cache_attr, None)
        if cached_value is not None:
            return cached_value

        # Determine which macros to hash
        if include_core:
            # Hash both core and variable macros
            all_items = list(self.core.items()) + list(self.variable.items())
            items_to_hash = frozenset(all_items)
        else:
            # Hash only variable macros (existing behavior for preprocessing cache)
            items_to_hash = self.get_cache_key()

        # XOR stringzilla hashes of each (name, value) pair
        # No sorting needed - XOR is commutative (a^b == b^a)
        # stringzilla.hash() is deterministic across Python runs
        # Empty frozenset: XOR of 0 items = 0, format as '0000000000000000'
        combined = 0
        for name, value in items_to_hash:
            combined ^= sz.hash(bytes(name))
            combined ^= sz.hash(bytes(value))

        result = format(combined, '016x')
        setattr(self, cache_attr, result)
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


def is_macro_invariant(file_result, input_macros: 'MacroState') -> bool:
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
_invariant_cache: Dict[str, ProcessingResult] = {}  # str = content_hash (SHA1)
_variant_cache: Dict[Tuple[str, MacroCacheKey], ProcessingResult] = {}  # str = content_hash (SHA1)

# Cache statistics
_cache_stats = {
    'hits': 0,
    'misses': 0,
    'total_calls': 0,
    'invariant_hits': 0,
    'variant_hits': 0,
    'invariant_misses': 0,
    'variant_misses': 0
}


def get_or_compute_preprocessing(
    file_result,
    input_macros: 'MacroState',
    verbose: int = 0
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
        verbose: Verbosity level for debugging

    Returns:
        ProcessingResult with active lines, includes, magic flags, defines, and updated MacroState
    """
    from compiletools.simple_preprocessor import SimplePreprocessor

    _cache_stats['total_calls'] += 1

    content_hash = file_result.content_hash
    invariant = is_macro_invariant(file_result, input_macros)

    # Check appropriate cache
    if invariant:
        # Macro-invariant: cache key is content_hash only
        if content_hash in _invariant_cache:
            _cache_stats['hits'] += 1
            _cache_stats['invariant_hits'] += 1
            cached = _invariant_cache[content_hash]
            # CRITICAL FIX: Reconstruct updated_macros from caller's input + file's defines
            # This prevents stale macro pollution from first caller's context
            reconstructed_macros = input_macros.with_updates(cached.file_defines)
            return ProcessingResult(
                active_lines=cached.active_lines,
                active_includes=cached.active_includes,
                active_magic_flags=cached.active_magic_flags,
                active_defines=cached.active_defines,
                updated_macros=reconstructed_macros,
                file_defines=cached.file_defines
            )

        _cache_stats['misses'] += 1
        _cache_stats['invariant_misses'] += 1
    else:
        # Macro-variant: cache key is (content_hash, macro_cache_key)
        # Try to use cached key if available to avoid recomputation
        macro_key = input_macros.get_cached_key_if_available()
        if macro_key is None:
            macro_key = input_macros.get_cache_key()
        cache_key = (content_hash, macro_key)

        if cache_key in _variant_cache:
            _cache_stats['hits'] += 1
            _cache_stats['variant_hits'] += 1
            cached = _variant_cache[cache_key]
            # Apply same reconstruction pattern for consistency
            reconstructed_macros = input_macros.with_updates(cached.file_defines)
            return ProcessingResult(
                active_lines=cached.active_lines,
                active_includes=cached.active_includes,
                active_magic_flags=cached.active_magic_flags,
                active_defines=cached.active_defines,
                updated_macros=reconstructed_macros,
                file_defines=cached.file_defines
            )

        _cache_stats['misses'] += 1
        _cache_stats['variant_misses'] += 1

    # Compute result - pass all macros to preprocessor
    all_macros = input_macros.all_macros()
    preprocessor = SimplePreprocessor(all_macros, verbose=verbose)
    active_lines = preprocessor.process_structured(file_result)
    active_line_set = set(active_lines)

    # Extract active includes
    active_includes = []
    for inc in file_result.includes:
        if inc['line_num'] in active_line_set:
            active_includes.append(inc)

    # Extract active magic flags
    active_magic_flags = []
    for magic in file_result.magic_flags:
        if magic['line_num'] in active_line_set:
            active_magic_flags.append(magic)

    # Extract active defines
    active_defines = []
    for define in file_result.defines:
        if define['line_num'] in active_line_set:
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
    file_defines: MacroDict = {}
    for k, v in new_variable_macros.items():
        if k not in input_macros.variable:
            file_defines[k] = v

    # CRITICAL: Use with_updates to preserve existing variable macros during traversal
    # Creates MacroState with input_macros.variable + new_variable_macros
    # This ensures macros from previously processed files (e.g., base.hpp) are preserved
    # when processing subsequent files (e.g., conditional.hpp)
    updated_macro_state = input_macros.with_updates(new_variable_macros)

    # Create result
    result = ProcessingResult(
        active_lines=active_lines,
        active_includes=active_includes,
        active_magic_flags=active_magic_flags,
        active_defines=active_defines,
        updated_macros=updated_macro_state,
        file_defines=file_defines
    )

    # Store in appropriate cache
    if invariant:
        _invariant_cache[content_hash] = result
    else:
        _variant_cache[cache_key] = result

    return result


def get_cache_stats() -> dict:
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
    total_size = 0
    for result in _invariant_cache.values():
        total_size += sys.getsizeof(result.active_lines)
        total_size += sys.getsizeof(result.active_includes)
        total_size += sys.getsizeof(result.active_magic_flags)
        total_size += sys.getsizeof(result.active_defines)
        total_size += sys.getsizeof(result.updated_macros)

    for result in _variant_cache.values():
        total_size += sys.getsizeof(result.active_lines)
        total_size += sys.getsizeof(result.active_includes)
        total_size += sys.getsizeof(result.active_magic_flags)
        total_size += sys.getsizeof(result.active_defines)
        total_size += sys.getsizeof(result.updated_macros)

    hit_rate = 0.0
    if _cache_stats['total_calls'] > 0:
        hit_rate = (_cache_stats['hits'] / _cache_stats['total_calls']) * 100

    return {
        'entries': len(_invariant_cache) + len(_variant_cache),
        'invariant_entries': len(_invariant_cache),
        'variant_entries': len(_variant_cache),
        'hits': _cache_stats['hits'],
        'invariant_hits': _cache_stats['invariant_hits'],
        'variant_hits': _cache_stats['variant_hits'],
        'misses': _cache_stats['misses'],
        'invariant_misses': _cache_stats['invariant_misses'],
        'variant_misses': _cache_stats['variant_misses'],
        'total_calls': _cache_stats['total_calls'],
        'hit_rate': hit_rate,
        'memory_bytes': total_size,
        'memory_mb': total_size / (1024 * 1024)
    }


def clear_cache():
    """Clear the preprocessing cache and reset statistics.

    Also clears the file_analyzer.analyze_file() cache since preprocessed
    results depend on file analysis.

    NOTE: For tests, this clears everything including any MacroState optimizations.

    Useful for:
    - Testing to ensure clean state
    - Benchmarking to measure from scratch
    - Memory management in long-running processes
    """
    _invariant_cache.clear()
    _variant_cache.clear()
    _cache_stats['hits'] = 0
    _cache_stats['misses'] = 0
    _cache_stats['invariant_hits'] = 0
    _cache_stats['variant_hits'] = 0
    _cache_stats['invariant_misses'] = 0
    _cache_stats['variant_misses'] = 0
    _cache_stats['total_calls'] = 0

    # Clear file analyzer cache since analysis results are used by preprocessing
    from compiletools.file_analyzer import analyze_file
    analyze_file.cache_clear()

    # Clear global hash registry to prevent stale hash lookups in tests
    from compiletools.global_hash_registry import clear_global_registry, get_file_hash
    clear_global_registry()
    get_file_hash.cache_clear()


def print_preprocessing_stats():
    """Print preprocessing cache and SimplePreprocessor statistics."""
    stats = get_cache_stats()

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