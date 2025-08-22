#!/usr/bin/env python3
"""Benchmark to determine potential savings from FileAnalyzer caching.

This script measures:
1. Time to analyze files without cache
2. Time to compute hash of file content 
3. Simulated cache lookup time
4. Potential time savings from caching
"""
import argparse
from compiletools.git_sha_report import batch_hash_objects
from pathlib import Path
import os
import pickle
import time
from typing import List, Tuple

import tempfile
from pathlib import Path

from compiletools.file_analyzer import create_file_analyzer, FileAnalysisResult
from compiletools.file_analyzer_cache import create_cache, NullCache, CACHE_FORMAT_VERSION
from compiletools.testhelper import samplesdir


def measure_analysis_time(filepath: str, repetitions: int = 10) -> Tuple[float, FileAnalysisResult]:
    """Measure time to analyze a file."""
    # Clear any existing LRU cache by creating new analyzer instances
    times = []
    result = None
    
    for _ in range(repetitions):
        start = time.perf_counter()
        analyzer = create_file_analyzer(filepath)
        result = analyzer.analyze()
        end = time.perf_counter()
        times.append(end - start)
    
    avg_time = sum(times) / len(times)
    return avg_time, result


def measure_hash_time(filepath: str, repetitions: int = 10) -> float:
    """Measure time to compute file hash."""
    times = []
    
    for _ in range(repetitions):
        start = time.perf_counter()
        hash_file_content(filepath)
        end = time.perf_counter()
        times.append(end - start)
    
    return sum(times) / len(times)


def measure_serialization_time(result: FileAnalysisResult, repetitions: int = 10) -> Tuple[float, float, int]:
    """Measure time to serialize/deserialize FileAnalysisResult."""
    # Use a dummy cache instance to access serialization methods
    dummy_cache = NullCache()
    
    serialize_times = []
    deserialize_times = []
    serialized_data = None
    
    for _ in range(repetitions):
        # Measure serialization
        start = time.perf_counter()
        serialized_data = dummy_cache._serialize_result(result)
        end = time.perf_counter()
        serialize_times.append(end - start)
        
        # Measure deserialization
        start = time.perf_counter()
        dummy_cache._deserialize_result(serialized_data)
        end = time.perf_counter()
        deserialize_times.append(end - start)
    
    avg_serialize = sum(serialize_times) / len(serialize_times)
    avg_deserialize = sum(deserialize_times) / len(deserialize_times)
    size = len(serialized_data) if serialized_data else 0
    
    return avg_serialize, avg_deserialize, size


def find_test_files(directory: str = ".", max_files: int = 50) -> List[str]:
    """Find C/C++ source files to test."""
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            if filename.endswith(('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.C', '.H')):
                filepath = os.path.join(root, filename)
                if os.path.getsize(filepath) > 0:  # Skip empty files
                    files.append(filepath)
                    if len(files) >= max_files:
                        return files
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark FileAnalyzer cache performance for different cache types.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "test_directory", 
        nargs="?", 
        default=None,
        help="Directory to search for test files (defaults to samples directory)"
    )
    parser.add_argument(
        "--max-files", 
        type=int, 
        default=20,
        help="Maximum number of files to test"
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=5,
        help="Number of repetitions for each measurement"
    )
    args = parser.parse_args()
    
    test_dir = args.test_directory if args.test_directory else samplesdir()
    
    print("FileAnalyzer Cache Performance Benchmark")
    print(f"Test directory: {test_dir}")
    print(f"Max files: {args.max_files}, Repetitions: {args.repetitions}")
    print(f"Cache format version: {CACHE_FORMAT_VERSION}")
    print("=" * 70)
    
    print(f"\nSearching for test files...")
    test_files = find_test_files(test_dir, args.max_files)
    
    if not test_files:
        print("No C/C++ files found for testing.")
        return
        
    print(f"Found {len(test_files)} test files. Starting benchmarks...")
    
    # Create a temporary directory for cache files
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        cache_configs = {
            'null': {},
            'memory': {},
            'disk': {'cache_dir': str(temp_path / 'disk_cache')},
            'sqlite': {'db_path': str(temp_path / 'file_analyzer_cache.db')}
        }

        results = {}

        # Initialize global hash registry once for all cache types
        from compiletools.global_hash_registry import initialize_global_hash_registry, get_file_hash, get_registry_stats
        print("\nInitializing global hash registry...")
        
        registry_start = time.perf_counter()
        initialize_global_hash_registry(use_git_hashes=True)
        registry_time = time.perf_counter() - registry_start
        
        stats = get_registry_stats()
        print(f"Registry loaded {stats['total_files']} files in {registry_time*1000:.1f}ms")

        for cache_type, config in cache_configs.items():
            print(f"\n--- Benchmarking {cache_type.upper()} Cache ---")
            
            # Create cache and clear it before test
            cache = create_cache(cache_type, **config)
            cache.clear()

            # Get file hashes from global registry (already loaded)
            file_hashes = {filepath: get_file_hash(filepath) or "" for filepath in test_files}

            total_analysis_time = 0
            total_cache_put_time = 0
            total_cache_get_time = 0
            file_count = 0

            for filepath in test_files:
                file_size = os.path.getsize(filepath)
                
                # 1. Full analysis (cache miss)
                analysis_time, result = measure_analysis_time(filepath, args.repetitions)
                content_hash = file_hashes.get(filepath, "")
                
                # 2. Cache put
                put_times = []
                for _ in range(args.repetitions):
                    start = time.perf_counter()
                    cache.put(filepath, content_hash, result)
                    end = time.perf_counter()
                    put_times.append(end - start)
                cache_put_time = sum(put_times) / len(put_times)

                # 3. Cache get (cache hit)
                get_times = []
                for _ in range(args.repetitions):
                    start = time.perf_counter()
                    cache.get(filepath, content_hash)
                    end = time.perf_counter()
                    get_times.append(end - start)
                cache_get_time = sum(get_times) / len(get_times)
                
                print(f"  {os.path.basename(filepath):<25} ({file_size:>7,} bytes) - "
                      f"Analysis: {analysis_time*1000000:>8.1f}μs, "
                      f"Put: {cache_put_time*1000000:>8.1f}μs, "
                      f"Get: {cache_get_time*1000000:>8.1f}μs")

                total_analysis_time += analysis_time
                total_cache_put_time += cache_put_time
                total_cache_get_time += cache_get_time
                file_count += 1
                
            # Close connection if it exists (for SQLite)
            if hasattr(cache, 'close'):
                cache.close()

            if file_count > 0:
                results[cache_type] = {
                    'avg_analysis': total_analysis_time / file_count,
                    'avg_put': total_cache_put_time / file_count,
                    'avg_get': total_cache_get_time / file_count,
                }

    # --- Summary ---
    print("\n" + "=" * 70)
    print("Benchmark Summary (average times in μs)")
    print("-" * 70)
    print(f"{'Cache Type':<15} | {'Analysis (Miss)':>20} | {'Cache Put':>15} | {'Cache Get (Hit)':>18}")
    print("-" * 70)

    if 'null' in results:
        baseline_miss = results['null']['avg_analysis']
        
        for cache_type, data in results.items():
            analysis_us = data['avg_analysis'] * 1000000
            put_us = data['avg_put'] * 1000000
            get_us = data['avg_get'] * 1000000
            
            # Savings calculation
            savings_us = (baseline_miss - data['avg_get']) * 1000000
            savings_percent = (savings_us / analysis_us) * 100 if analysis_us > 0 else 0
            
            print(f"{cache_type:<15} | {analysis_us:>20.1f} | {put_us:>15.1f} | {get_us:>18.1f} | {savings_percent:>6.1f}%")

    print("-" * 70)
    
    # Add hash performance comparison
    print("\nHash Performance Comparison:")
    print("-" * 30)
    
    # Test global registry lookup performance
    from compiletools.global_hash_registry import get_file_hash
    lookup_times = []
    for _ in range(100):
        start = time.perf_counter()
        for filepath in test_files[:3]:  # Test with first 3 files
            get_file_hash(filepath)
        end = time.perf_counter()
        lookup_times.append(end - start)
    
    avg_lookup_time = sum(lookup_times) / len(lookup_times)
    
    # Test traditional hashlib performance
    import hashlib
    hashlib_times = []
    for _ in range(10):  # Fewer iterations since this is slower
        start = time.perf_counter()
        for filepath in test_files[:3]:
            try:
                with open(filepath, 'rb') as f:
                    hashlib.sha256(f.read()).hexdigest()
            except:
                pass
        end = time.perf_counter()
        hashlib_times.append(end - start)
    
    avg_hashlib_time = sum(hashlib_times) / len(hashlib_times)
    
    print(f"Global registry lookups: {avg_lookup_time*1000:.2f}ms (for {len(test_files[:3])} files)")
    print(f"Traditional hashlib:     {avg_hashlib_time*1000:.2f}ms (for {len(test_files[:3])} files)")
    speedup = avg_hashlib_time / avg_lookup_time if avg_lookup_time > 0 else 0
    print(f"Registry speedup:        {speedup:.1f}x faster")
    
    print("\nRECOMMENDATION:")
    
    if 'sqlite' in results and 'disk' in results:
        sqlite_get = results['sqlite']['avg_get']
        disk_get = results['disk']['avg_get']
        
        if sqlite_get < disk_get:
            print("✓ SQLite cache is recommended for best performance.")
        else:
            print("✓ Disk cache is recommended for best performance.")
            
    print("  - Global hash registry provides significant speedup for file hashing")
    print("  - Consider using 'memory' for single runs where persistence is not needed.")
    print("  - 'null' cache is useful for forcing re-analysis, but offers no speed advantage.")



if __name__ == "__main__":
    main()