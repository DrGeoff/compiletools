"""Unified preprocessing cache for compiletools.

This module provides a centralized cache for preprocessing results that can be
shared across SimplePreprocessor, DirectMagicFlags, and CppHeaderDeps.

The cache uses two strategies:
1. Macro-invariant files (no conditionals): cached by content_hash only
2. Macro-variant files (has conditionals): cached by (content_hash, macro_cache_key)

This optimizes the common case where files have #define but no #if/#ifdef.
"""

from typing import List, Dict, FrozenSet, Tuple
from dataclasses import dataclass
import sys
import stringzilla as sz
from compiletools.simple_preprocessor import compute_macro_hash


@dataclass
class ProcessingResult:
    """Result of preprocessing a file with conditional compilation.

    Attributes:
        active_lines: Line numbers that are active after preprocessing (0-based)
        active_includes: List of active #include directives with metadata
        active_magic_flags: List of active magic flags with metadata
        active_defines: List of active #define directives with metadata
        updated_macros: Macro state after processing (input + defines - undefs)
    """
    active_lines: List[int]
    active_includes: List[dict]
    active_magic_flags: List[dict]
    active_defines: List[dict]
    updated_macros: Dict[sz.Str, sz.Str]


# Type alias for macro dictionaries
MacroDict = Dict[sz.Str, sz.Str]


def _make_macro_cache_key(macros: MacroDict) -> FrozenSet[Tuple[str, str]]:
    """Create fast hashable cache key from macro dictionary.

    Uses frozenset for optimal Python dict performance in cache lookups.

    Args:
        macros: Dictionary of macro definitions

    Returns:
        Frozenset of (key, value) tuples suitable as dict key
    """
    return frozenset((str(k), str(v)) for k, v in macros.items())


def is_macro_invariant(file_result) -> bool:
    """Determine if a file's active lines are independent of macro state.

    A file is macro-invariant if it contains no conditional compilation directives.
    Such files always have the same active lines regardless of input macros.

    Examples of macro-invariant files:
    - Headers with only #define, #include, #pragma
    - Implementation files without #ifdef/#ifndef

    Args:
        file_result: FileAnalysisResult to check

    Returns:
        True if file has no conditional directives, False otherwise
    """
    conditional_directives = {'if', 'ifdef', 'ifndef', 'elif'}
    return all(
        len(file_result.directive_positions.get(dtype, [])) == 0
        for dtype in conditional_directives
    )


# Dual cache strategy:
# 1. Invariant cache: content_hash -> ProcessingResult (for files without conditionals)
# 2. Variant cache: (content_hash, macro_cache_key) -> ProcessingResult (for files with conditionals)
#
# NOTE: We use manual caching instead of @lru_cache because:
# 1. Function arguments (FileAnalysisResult, Dict) are not hashable
# 2. Cache key must be extracted from file_result and macros
# 3. We need full objects to compute results, not just hashes
# 4. Provides enhanced debugging (dump_cache_keys with file path resolution)
_invariant_cache: Dict[str, ProcessingResult] = {}
_variant_cache: Dict[Tuple[str, FrozenSet[Tuple[str, str]]], ProcessingResult] = {}

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
    input_macros: MacroDict,
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
        input_macros: Initial macro state for this file
        verbose: Verbosity level for debugging

    Returns:
        ProcessingResult with active lines, includes, magic flags, defines, and updated macros
    """
    from compiletools.simple_preprocessor import SimplePreprocessor

    _cache_stats['total_calls'] += 1

    content_hash = file_result.content_hash
    invariant = is_macro_invariant(file_result)

    # Check appropriate cache
    if invariant:
        # Macro-invariant: cache key is content_hash only
        if content_hash in _invariant_cache:
            _cache_stats['hits'] += 1
            _cache_stats['invariant_hits'] += 1
            if verbose >= 9:
                from compiletools.global_hash_registry import get_filepath_by_hash
                filepath = get_filepath_by_hash(content_hash) or '<unknown>'
                print(f"Invariant cache hit: {filepath}")
            return _invariant_cache[content_hash]

        _cache_stats['misses'] += 1
        _cache_stats['invariant_misses'] += 1
        if verbose >= 9:
            from compiletools.global_hash_registry import get_filepath_by_hash
            filepath = get_filepath_by_hash(content_hash) or '<unknown>'
            print(f"Invariant cache miss: {filepath}")
    else:
        # Macro-variant: cache key is (content_hash, macro_cache_key)
        macro_key = _make_macro_cache_key(input_macros)
        cache_key = (content_hash, macro_key)

        # Track macro states for analysis
        if content_hash not in _macro_states_by_content:
            _macro_states_by_content[content_hash] = []
        macro_hash = compute_macro_hash(input_macros)
        _macro_states_by_content[content_hash].append((macro_hash, input_macros.copy()))

        if cache_key in _variant_cache:
            _cache_stats['hits'] += 1
            _cache_stats['variant_hits'] += 1
            if verbose >= 9:
                from compiletools.global_hash_registry import get_filepath_by_hash
                filepath = get_filepath_by_hash(content_hash) or '<unknown>'
                print(f"Variant cache hit: {filepath} (macro_hash={macro_hash})")
            return _variant_cache[cache_key]

        _cache_stats['misses'] += 1
        _cache_stats['variant_misses'] += 1
        if verbose >= 9:
            from compiletools.global_hash_registry import get_filepath_by_hash
            filepath = get_filepath_by_hash(content_hash) or '<unknown>'
            print(f"Variant cache miss: {filepath} (macro_hash={macro_hash})")

    # Compute result
    preprocessor = SimplePreprocessor(input_macros.copy(), verbose=verbose)
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

    # Updated macros are in preprocessor.macros after processing
    updated_macros = preprocessor.macros.copy()

    # Create result
    result = ProcessingResult(
        active_lines=active_lines,
        active_includes=active_includes,
        active_magic_flags=active_magic_flags,
        active_defines=active_defines,
        updated_macros=updated_macros
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

    Useful for:
    - Testing to ensure clean state
    - Benchmarking to measure from scratch
    - Memory management in long-running processes
    """
    _invariant_cache.clear()
    _variant_cache.clear()
    _macro_states_by_content.clear()
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


# Track macro states for analysis
_macro_states_by_content = {}  # content_hash -> list of (macro_hash, input_macros)