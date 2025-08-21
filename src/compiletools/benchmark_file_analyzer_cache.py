#!/usr/bin/env python3
"""Benchmark to determine potential savings from FileAnalyzer caching.

This script measures:
1. Time to analyze files without cache
2. Time to compute hash of file content 
3. Simulated cache lookup time
4. Potential time savings from caching
"""

import argparse
import hashlib
import os
import pickle
import time
from typing import List, Tuple

from compiletools.file_analyzer import create_file_analyzer, FileAnalysisResult
from compiletools.testhelper import samplesdir


def hash_file_content(filepath: str) -> str:
    """Compute hash of file content for cache key."""
    with open(filepath, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


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
    serialize_times = []
    deserialize_times = []
    serialized_data = None
    
    for _ in range(repetitions):
        # Measure serialization
        start = time.perf_counter()
        serialized_data = pickle.dumps(result)
        end = time.perf_counter()
        serialize_times.append(end - start)
        
        # Measure deserialization
        start = time.perf_counter()
        pickle.loads(serialized_data)
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
            if filename.endswith(('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp')):
                filepath = os.path.join(root, filename)
                if os.path.getsize(filepath) > 0:  # Skip empty files
                    files.append(filepath)
                    if len(files) >= max_files:
                        return files
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark FileAnalyzer cache performance",
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
    args = parser.parse_args()
    
    # Use provided directory or default to samples
    test_dir = args.test_directory if args.test_directory else samplesdir()
    
    print("FileAnalyzer Cache Performance Benchmark")
    print("=" * 60)
    
    print(f"\nSearching for test files in {test_dir}...")
    test_files = find_test_files(test_dir)
    
    if not test_files:
        print("No C/C++ files found for testing")
        return
    
    print(f"Found {len(test_files)} test files")
    
    # Benchmark results
    total_analysis_time = 0
    total_hash_time = 0
    total_serialize_time = 0
    total_deserialize_time = 0
    total_cache_hit_time = 0
    total_size = 0
    file_count = 0
    
    print("\nAnalyzing files...")
    print("-" * 60)
    
    for filepath in test_files[:args.max_files]:  # Test specified number of files
        file_size = os.path.getsize(filepath)
        print(f"\nFile: {os.path.basename(filepath)} ({file_size:,} bytes)")
        
        # Measure analysis time
        analysis_time, result = measure_analysis_time(filepath, 5)
        print(f"  Analysis time: {analysis_time*1000:.3f} ms")
        
        # Measure hash computation time
        hash_time = measure_hash_time(filepath, 5)
        print(f"  Hash time: {hash_time*1000:.3f} ms")
        
        # Measure serialization time
        serialize_time, deserialize_time, serialized_size = measure_serialization_time(result, 5)
        print(f"  Serialize time: {serialize_time*1000:.3f} ms")
        print(f"  Deserialize time: {deserialize_time*1000:.3f} ms")
        print(f"  Serialized size: {serialized_size:,} bytes")
        
        # Simulate cache hit time (hash + deserialize)
        cache_hit_time = hash_time + deserialize_time
        print(f"  Cache hit time: {cache_hit_time*1000:.3f} ms")
        
        # Calculate savings
        savings = analysis_time - cache_hit_time
        savings_percent = (savings / analysis_time) * 100 if analysis_time > 0 else 0
        print(f"  Potential savings: {savings*1000:.3f} ms ({savings_percent:.1f}%)")
        
        # Accumulate totals
        total_analysis_time += analysis_time
        total_hash_time += hash_time
        total_serialize_time += serialize_time
        total_deserialize_time += deserialize_time
        total_cache_hit_time += cache_hit_time
        total_size += serialized_size
        file_count += 1
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if file_count > 0:
        avg_analysis = (total_analysis_time / file_count) * 1000
        avg_cache_hit = (total_cache_hit_time / file_count) * 1000
        avg_savings = avg_analysis - avg_cache_hit
        avg_savings_percent = (avg_savings / avg_analysis) * 100 if avg_analysis > 0 else 0
        
        print(f"\nFiles analyzed: {file_count}")
        print(f"Average analysis time: {avg_analysis:.3f} ms")
        print(f"Average cache hit time: {avg_cache_hit:.3f} ms")
        print(f"Average savings per file: {avg_savings:.3f} ms ({avg_savings_percent:.1f}%)")
        print(f"Average serialized size: {total_size/file_count:,.0f} bytes")
        
        print(f"\nTotal time for {file_count} files:")
        print(f"  Without cache: {total_analysis_time:.3f} seconds")
        print(f"  With cache (all hits): {total_cache_hit_time:.3f} seconds")
        print(f"  Time saved: {total_analysis_time - total_cache_hit_time:.3f} seconds")
        
        # Estimate for larger build
        estimated_files = 100
        estimated_no_cache = avg_analysis * estimated_files / 1000
        estimated_with_cache = avg_cache_hit * estimated_files / 1000
        print(f"\nEstimated for {estimated_files} files:")
        print(f"  Without cache: {estimated_no_cache:.2f} seconds")
        print(f"  With cache: {estimated_with_cache:.2f} seconds")
        print(f"  Time saved: {estimated_no_cache - estimated_with_cache:.2f} seconds")
        
        # Cache hit rate simulation
        print("\nCache effectiveness depends on hit rate:")
        for hit_rate in [0.5, 0.7, 0.9, 0.95]:
            effective_time = (hit_rate * avg_cache_hit + (1-hit_rate) * avg_analysis) * estimated_files / 1000
            saved = estimated_no_cache - effective_time
            print(f"  {hit_rate*100:.0f}% hit rate: {saved:.2f} seconds saved")
    
    print("\n" + "=" * 60)
    print("RECOMMENDATION")
    print("=" * 60)
    
    if file_count > 0 and avg_savings_percent > 50:
        print("\n✓ Caching is HIGHLY RECOMMENDED")
        print(f"  - Average savings of {avg_savings_percent:.1f}% per file")
        print("  - Significant time savings for repeated builds")
    elif file_count > 0 and avg_savings_percent > 20:
        print("\n✓ Caching is RECOMMENDED")
        print(f"  - Average savings of {avg_savings_percent:.1f}% per file")
        print("  - Moderate time savings for repeated builds")
    else:
        print("\n⚠ Caching may not provide significant benefits")
        print(f"  - Only {avg_savings_percent:.1f}% average savings per file")
        print("  - Consider the complexity vs benefit trade-off")


if __name__ == "__main__":
    main()