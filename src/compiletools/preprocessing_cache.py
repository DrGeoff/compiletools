"""Unified preprocessing cache for compiletools.

This module provides a centralized cache for preprocessing results that can be
shared across SimplePreprocessor, DirectMagicFlags, and CppHeaderDeps.

The cache key is (content_hash, macro_hash) which uniquely identifies:
- The file content being processed
- The macro state used for conditional compilation

This ensures correct cache hits only when both file and macro state match.
"""

from typing import List, Dict
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


# Global unified cache: (content_hash, macro_hash) -> ProcessingResult
#
# NOTE: We use manual caching instead of @lru_cache because:
# 1. Function arguments (FileAnalysisResult, Dict) are not hashable
# 2. Cache key must be extracted: (file_result.content_hash, compute_macro_hash(macros))
# 3. We need full objects to compute results, not just hashes
# 4. Provides enhanced debugging (dump_cache_keys with file path resolution)
_unified_cache = {}

# Cache statistics
_cache_stats = {
    'hits': 0,
    'misses': 0,
    'total_calls': 0
}


def get_or_compute_preprocessing(
    file_result,
    input_macros: Dict[sz.Str, sz.Str],
    verbose: int = 0
) -> ProcessingResult:
    """Get preprocessing result from cache or compute if not cached.

    IMPORTANT: Caller must propagate macro state across files:
        result1 = get_or_compute_preprocessing(file1, initial_macros, verbose)
        result2 = get_or_compute_preprocessing(file2, result1.updated_macros, verbose)

    Args:
        file_result: FileAnalysisResult with file content and metadata
        input_macros: Initial macro state for this file (used for cache key)
        verbose: Verbosity level for debugging

    Returns:
        ProcessingResult with active lines, includes, magic flags, defines, and updated macros

    The cache key is (file_result.content_hash, macro_hash) which ensures:
    - Same file content + same macro state = cache hit
    - Different content or different macros = cache miss (correct behavior)
    """
    from compiletools.simple_preprocessor import SimplePreprocessor

    _cache_stats['total_calls'] += 1

    # Compute cache key
    content_hash = file_result.content_hash
    macro_hash = compute_macro_hash(input_macros)
    cache_key = (content_hash, macro_hash)

    # Track macro states for analysis
    if content_hash not in _macro_states_by_content:
        _macro_states_by_content[content_hash] = []
    _macro_states_by_content[content_hash].append((macro_hash, input_macros.copy()))

    # Check cache
    if cache_key in _unified_cache:
        _cache_stats['hits'] += 1
        if verbose >= 9:
            from compiletools.global_hash_registry import get_filepath_by_hash
            filepath = get_filepath_by_hash(content_hash) or '<unknown>'
            print(f"Cache hit: {filepath} (content={content_hash[:8]}..., macro={macro_hash})")
        return _unified_cache[cache_key]

    _cache_stats['misses'] += 1

    if verbose >= 9:
        from compiletools.global_hash_registry import get_filepath_by_hash
        filepath = get_filepath_by_hash(content_hash) or '<unknown>'
        print(f"Cache miss: {filepath} (content={content_hash[:8]}..., macro={macro_hash})")

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

    # Store in cache
    _unified_cache[cache_key] = result

    return result


def get_cache_stats() -> dict:
    """Return cache statistics for debugging and monitoring.

    Returns:
        Dictionary with cache metrics:
        - entries: Number of cached results
        - hits: Number of cache hits
        - misses: Number of cache misses
        - total_calls: Total calls to get_or_compute_preprocessing
        - hit_rate: Percentage of cache hits (0-100)
        - memory_bytes: Approximate memory usage
        - memory_mb: Memory usage in MB
    """
    total_size = 0
    for result in _unified_cache.values():
        # Estimate size of each component
        total_size += sys.getsizeof(result.active_lines)
        total_size += sys.getsizeof(result.active_includes)
        total_size += sys.getsizeof(result.active_magic_flags)
        total_size += sys.getsizeof(result.active_defines)
        total_size += sys.getsizeof(result.updated_macros)

    hit_rate = 0.0
    if _cache_stats['total_calls'] > 0:
        hit_rate = (_cache_stats['hits'] / _cache_stats['total_calls']) * 100

    return {
        'entries': len(_unified_cache),
        'hits': _cache_stats['hits'],
        'misses': _cache_stats['misses'],
        'total_calls': _cache_stats['total_calls'],
        'hit_rate': hit_rate,
        'memory_bytes': total_size,
        'memory_mb': total_size / (1024 * 1024)
    }


def clear_cache():
    """Clear the preprocessing cache and reset statistics.

    Useful for:
    - Testing to ensure clean state
    - Benchmarking to measure from scratch
    - Memory management in long-running processes
    """
    _unified_cache.clear()
    _cache_stats['hits'] = 0
    _cache_stats['misses'] = 0
    _cache_stats['total_calls'] = 0


def dump_cache_keys(limit: int = 20):
    """Print cache keys for debugging.

    Args:
        limit: Maximum number of keys to print
    """
    print(f"\nPreprocessing cache keys ({len(_unified_cache)} entries):")
    for i, (content_hash, macro_hash) in enumerate(_unified_cache.keys()):
        if i >= limit:
            remaining = len(_unified_cache) - limit
            print(f"  ... and {remaining} more")
            break
        from compiletools.global_hash_registry import get_filepath_by_hash
        filepath = get_filepath_by_hash(content_hash) or '<unknown>'
        print(f"  [{i+1}] {filepath}")
        print(f"      content={content_hash[:8]}...{content_hash[-8:]}")
        print(f"      macro={macro_hash}")


# Track macro states for analysis
_macro_states_by_content = {}  # content_hash -> list of (macro_hash, input_macros)


def analyze_cache_effectiveness():
    """Analyze why cache isn't hitting as expected."""
    from collections import defaultdict
    from compiletools.global_hash_registry import get_filepath_by_hash

    # Group by content_hash to see how many macro states per file
    content_to_macro_hashes = defaultdict(set)
    for content_hash, macro_hash in _unified_cache.keys():
        content_to_macro_hashes[content_hash].add(macro_hash)

    # Find files with multiple macro states
    files_with_multiple_states = []
    for content_hash, macro_hashes in content_to_macro_hashes.items():
        if len(macro_hashes) > 1:
            filepath = get_filepath_by_hash(content_hash) or '<unknown>'
            files_with_multiple_states.append((filepath, content_hash, len(macro_hashes)))

    print(f"\n=== Cache Effectiveness Analysis ===")
    print(f"Total cache entries: {len(_unified_cache)}")
    print(f"Unique files: {len(content_to_macro_hashes)}")
    print(f"Files with multiple macro states: {len(files_with_multiple_states)}")

    if files_with_multiple_states:
        print(f"\nTop 10 files with most macro state variations:")
        files_with_multiple_states.sort(key=lambda x: x[2], reverse=True)
        for filepath, content_hash, count in files_with_multiple_states[:10]:
            print(f"  {count:3}x  {filepath}")

            # Analyze macro differences if we tracked them
            if content_hash in _macro_states_by_content:
                macro_states = _macro_states_by_content[content_hash]
                print(f"       Analyzing {len(macro_states)} macro states:")

                # Compare first two states to show differences
                if len(macro_states) >= 2:
                    state1_hash, state1_macros = macro_states[0]
                    state2_hash, state2_macros = macro_states[1]

                    keys1 = set(state1_macros.keys())
                    keys2 = set(state2_macros.keys())

                    only_in_1 = keys1 - keys2
                    only_in_2 = keys2 - keys1
                    common = keys1 & keys2

                    different_values = []
                    for key in common:
                        if state1_macros[key] != state2_macros[key]:
                            different_values.append(key)

                    print(f"       State 1 has {len(state1_macros)} macros, State 2 has {len(state2_macros)} macros")
                    if only_in_1:
                        print(f"       Only in state 1: {list(only_in_1)[:5]}")
                    if only_in_2:
                        print(f"       Only in state 2: {list(only_in_2)[:5]}")
                    if different_values:
                        print(f"       Different values: {different_values[:5]}")

    return {
        'total_entries': len(_unified_cache),
        'unique_files': len(content_to_macro_hashes),
        'files_with_multiple_states': len(files_with_multiple_states)
    }