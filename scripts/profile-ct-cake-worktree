#!/usr/bin/env python3
"""Detailed performance profiling for ct-cake using git worktrees and cProfile.

This script combines the safety of git worktrees with the detailed analysis
of cProfile to provide comprehensive performance comparison between branches.
"""
import argparse
import subprocess
import tempfile
import json
import pstats
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import sys
import os
import shutil

def profile_ct_cake_in_worktree(worktree_path: Path, sample_dir: str,
                               magic_mode: str = "direct", profile_file: Optional[str] = None) -> Tuple[Dict, bool]:
    """Profile ct-cake execution using subprocess for complete isolation."""
    
    success = True
    stats_data = {}
    
    try:
        sample_path = worktree_path / "src" / "compiletools" / "samples" / sample_dir
        if not sample_path.exists():
            print(f"    Sample directory not found: {sample_path}")
            return {}, False
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            
            # Create a temporary profile file
            if not profile_file:
                profile_file = str(tmppath / "profile.prof")
            
            # Set up environment for the worktree
            env = os.environ.copy()
            env['PYTHONPATH'] = str(worktree_path / "src")
            
            # PERFORMANCE OPTIMIZATION: Detect functional compiler once and set CXX
            # This eliminates expensive compiler detection (60%+ performance regression)
            if 'CXX' not in env:
                functional_compiler = None
                try:
                    # Temporarily add worktree to Python path and detect compiler
                    original_path = sys.path[:]
                    sys.path.insert(0, str(worktree_path / "src"))
                    
                    import compiletools.apptools
                    if hasattr(compiletools.apptools, 'get_functional_cxx_compiler'):
                        functional_compiler = compiletools.apptools.get_functional_cxx_compiler()
                    else:
                        # v5.1.0 doesn't have get_functional_cxx_compiler, use g++ default
                        functional_compiler = 'g++'
                        
                except Exception:
                    # Fallback to g++ if any error occurs
                    functional_compiler = 'g++'
                finally:
                    # Restore original Python path
                    sys.path[:] = original_path
                
                # Set CXX to avoid expensive detection during profiling
                if functional_compiler:
                    env['CXX'] = functional_compiler
            
            # Find ct-cake executable in worktree's venv
            ct_cake_path = worktree_path / ".venv" / "bin" / "ct-cake"
            if not ct_cake_path.exists():
                print(f"    ct-cake not found in worktree venv: {ct_cake_path}")
                return {}, False
            
            # Prepare ct-cake command with profiling
            cmd = [
                "python", "-m", "cProfile", "-o", profile_file,
                str(ct_cake_path),
                "--auto",
                "--magic", magic_mode,
                "--makefilename", str(tmppath / "Makefile"),
                "--objdir", str(tmppath / "obj"),
                "--bindir", str(tmppath / "bin"),
            ]
            
            
            # Run ct-cake with profiling in subprocess
            result = subprocess.run(
                cmd,
                cwd=sample_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=60  # 60 second timeout
            )
            
            if result.returncode != 0:
                print(f"    ct-cake failed with return code {result.returncode}")
                if result.stderr:
                    print(f"    stderr: {result.stderr[:200]}...")
                success = False
            else:
                # Load and analyze the profile
                if Path(profile_file).exists():
                    ps = pstats.Stats(profile_file)
                    ps.sort_stats('cumulative')
                    stats_data = extract_performance_metrics(ps)
                    
                else:
                    print(f"    Profile file not created: {profile_file}")
                    success = False
                
    except subprocess.TimeoutExpired:
        print("    Timeout after 60 seconds")
        success = False
    except Exception as e:
        print(f"    Error profiling {sample_dir}: {e}")
        success = False
        
    return stats_data, success

def extract_performance_metrics(ps: pstats.Stats) -> Dict:
    """Extract key performance metrics from pstats."""
    
    # Get total stats
    total_calls = ps.total_calls
    total_time = ps.total_tt
    
    # Get top functions by directly accessing the stats data
    top_functions = []
    
    # Get the raw stats data and sort by cumulative time
    stats_data = []
    for func, (cc, nc, tt, ct, callers) in ps.stats.items():
        if ct > 0.001:  # Only include functions with significant time
            # Format function name
            filename, lineno, funcname = func
            if filename.startswith('/'):
                # Get just the filename, not full path
                filename = filename.split('/')[-1]
            func_name = f"{filename}:{lineno}({funcname})"
            
            stats_data.append({
                'ncalls': f"{cc}/{nc}" if cc != nc else str(nc),
                'tottime': tt,
                'cumtime': ct,
                'function': func_name
            })
    
    # Sort by cumulative time
    stats_data.sort(key=lambda x: x['cumtime'], reverse=True)
    top_functions = stats_data[:25]  # Top 25 functions
    
    # Get compiletools-specific functions
    compiletools_functions = {}
    io_functions = {}
    cache_functions = {}
    
    for func_data in stats_data:
        func_name = func_data['function']
        
        if 'compiletools' in func_name:
            # Extract just the compiletools part
            if 'compiletools/' in func_name:
                key = func_name.split('compiletools/')[-1]
            else:
                key = func_name
            compiletools_functions[key] = {
                'ncalls': func_data['ncalls'],
                'tottime': func_data['tottime'],
                'cumtime': func_data['cumtime']
            }
        elif '_io' in func_name or 'io.' in func_name:
            io_functions[func_name] = {
                'ncalls': func_data['ncalls'],
                'tottime': func_data['tottime'],
                'cumtime': func_data['cumtime']
            }
        elif 'cache' in func_name.lower():
            cache_functions[func_name] = {
                'ncalls': func_data['ncalls'],
                'tottime': func_data['tottime'],
                'cumtime': func_data['cumtime']
            }
    
    return {
        'total_calls': total_calls,
        'total_time': total_time,
        'top_functions': top_functions,
        'compiletools_functions': compiletools_functions,
        'io_functions': io_functions,
        'cache_functions': cache_functions
    }

def get_test_samples() -> List[str]:
    """Get list of sample directory names for testing."""
    return [
        "simple",           # Basic compilation
        "lotsofmagic",      # Heavy magic flag processing  
        "numbers",          # Multiple files
        "factory",          # Complex dependencies
        "cppflags_macros",  # Heavy preprocessing
    ]

def get_test_configurations() -> List[Dict[str, str]]:
    """Get list of test configurations for magic modes."""
    configurations = []

    magic_modes = ["direct", "cpp"]

    for magic_mode in magic_modes:
        configurations.append({
            "magic_mode": magic_mode,
            "name": magic_mode
        })

    return configurations

def cleanup_all_worktrees():
    """Clean up any existing worktrees that might be left over."""
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], 
                          capture_output=True, text=True)
    
    if result.returncode == 0:
        lines = result.stdout.strip().split('\n')
        worktrees_to_remove = []
        
        for line in lines:
            if line.startswith('worktree '):
                worktree_path = line[9:]  # Remove 'worktree ' prefix
                # Check if this is a temporary worktree (contains 'worktree_' in path)
                if 'worktree_' in worktree_path and '/tmp/' in worktree_path:
                    worktrees_to_remove.append(worktree_path)
        
        for wt_path in worktrees_to_remove:
            print(f"  Cleaning up leftover worktree: {wt_path}")
            subprocess.run(["git", "worktree", "remove", "--force", wt_path], 
                          capture_output=True)

def setup_worktree(branch_name: str, base_path: Path) -> Path:
    """Create a git worktree for the specified branch."""
    worktree_path = base_path / f"worktree_{branch_name}"
    
    # Remove existing worktree if it exists locally
    if worktree_path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], 
                      capture_output=True)
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
    
    # Also check if git thinks this worktree exists elsewhere and remove it
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], 
                          capture_output=True, text=True)
    if result.returncode == 0:
        lines = result.stdout.strip().split('\n')
        for i, line in enumerate(lines):
            if line.startswith('worktree ') and f'worktree_{branch_name}' in line:
                wt_path = line[9:]  # Remove 'worktree ' prefix
                print(f"  Removing existing worktree: {wt_path}")
                subprocess.run(["git", "worktree", "remove", "--force", wt_path], 
                              capture_output=True)
    
    # Check if branch is currently checked out in main repo
    result = subprocess.run(["git", "branch", "--show-current"], 
                          capture_output=True, text=True)
    current_branch = result.stdout.strip()
    
    if current_branch == branch_name:
        # If we're on the target branch, create worktree from HEAD
        # This creates a detached HEAD worktree pointing to the same commit
        result = subprocess.run([
            "git", "worktree", "add", "--detach", str(worktree_path), "HEAD"
        ], capture_output=True, text=True)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree for current branch {branch_name}: {result.stderr}")
    else:
        # Create new worktree normally
        result = subprocess.run([
            "git", "worktree", "add", str(worktree_path), branch_name
        ], capture_output=True, text=True)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree for {branch_name}: {result.stderr}")
    
    return worktree_path

def install_package_in_worktree(worktree_path: Path) -> bool:
    """Install the package in the worktree using uv."""
    print(f"  Installing package in {worktree_path.name}...")
    
    # Create virtual environment using uv
    venv_path = worktree_path / ".venv"
    result = subprocess.run([
        "uv", "venv", str(venv_path)
    ], capture_output=True, text=True, cwd=worktree_path, env=os.environ)
    
    if result.returncode != 0:
        print(f"    Failed to create venv: {result.stderr}")
        return False
    
    # Install package in editable mode using uv pip
    # Inherit the full environment including module-loaded compilers
    result = subprocess.run([
        "uv", "pip", "install", "-e", "."
    ], capture_output=True, text=True, cwd=worktree_path, env={
        **os.environ,
        "VIRTUAL_ENV": str(venv_path)
    })
    
    if result.returncode != 0:
        print(f"    Failed to install package: {result.stderr}")
        return False
    
    return True

def profile_branch_worktree(branch_name: str, worktree_path: Path, test_samples: List[str],
                           configurations: List[Dict[str, str]], save_profiles: bool = False,
                           profile_dir: Optional[Path] = None) -> Dict:
    """Profile ct-cake performance using a worktree."""
    print(f"\nProfiling branch: {branch_name} (worktree: {worktree_path.name})")

    results = {}

    for config in configurations:
        config_name = config["name"]
        magic_mode = config["magic_mode"]

        print(f"  Profiling configuration: {config_name}")
        config_results = {}

        for sample_dir in test_samples:
            print(f"    {sample_dir}", end="... ")

            profile_file = None
            if save_profiles and profile_dir:
                profile_file = str(profile_dir / f"{branch_name}_{config_name}_{sample_dir}.prof")

            stats_data, success = profile_ct_cake_in_worktree(
                worktree_path, sample_dir, magic_mode, profile_file
            )
            
            if success:
                config_results[sample_dir] = stats_data
                print(f"{stats_data['total_time']:.3f}s ({stats_data['total_calls']} calls)")
            else:
                config_results[sample_dir] = None
                print("FAILED")
        
        results[config_name] = config_results
    
    return results

def cleanup_worktrees(worktree_paths: List[Path]):
    """Clean up git worktrees."""
    for worktree_path in worktree_paths:
        if worktree_path.exists():
            print(f"Cleaning up worktree: {worktree_path}")
            subprocess.run(["git", "worktree", "remove", str(worktree_path)], 
                          capture_output=True)

def compare_profiles(baseline: Dict, current: Dict, baseline_name: str, current_name: str, verbose: bool = False) -> None:
    """Compare and display profiling results."""
    print("\n" + "="*100)
    print("DETAILED PERFORMANCE PROFILING COMPARISON (using git worktrees)")
    print("="*100)
    
    for config_name in baseline.keys():
        if config_name not in current:
            continue
            
        print(f"\nConfiguration: {config_name.upper()}")
        print("=" * 95)
        
        # Overall timing comparison
        baseline_time_header = f"{baseline_name} Time"
        current_time_header = f"{current_name} Time"
        baseline_calls_header = f"{baseline_name} Calls"
        current_calls_header = f"{current_name} Calls"
        
        print(f"{'Sample':<20} {baseline_time_header:<12} {current_time_header:<12} {baseline_calls_header:<15} {current_calls_header:<15} {'Status':<15}")
        print("-" * 95)
        
        total_baseline_time = 0
        total_current_time = 0
        
        for sample in baseline[config_name].keys():
            if sample not in current[config_name]:
                continue
                
            baseline_data = baseline[config_name][sample]
            current_data = current[config_name][sample]
            
            if baseline_data is None or current_data is None:
                status = "FAILED"
                baseline_str = "FAIL"
                current_str = "FAIL"
                baseline_calls = "FAIL"
                current_calls = "FAIL"
            else:
                baseline_time = baseline_data['total_time']
                current_time = current_data['total_time']
                baseline_calls = baseline_data['total_calls']
                current_calls = current_data['total_calls']
                
                total_baseline_time += baseline_time
                total_current_time += current_time
                
                time_change = ((current_time - baseline_time) / baseline_time) * 100 if baseline_time > 0 else 0
                ((current_calls - baseline_calls) / baseline_calls) * 100 if baseline_calls > 0 else 0
                
                if abs(time_change) < 2:
                    status = "~"
                elif time_change > 0:
                    status = f"SLOWER ({time_change:+.1f}%)"
                else:
                    status = f"FASTER ({time_change:+.1f}%)"
                    
                baseline_str = f"{baseline_time:.3f}s"
                current_str = f"{current_time:.3f}s"
                baseline_calls = f"{baseline_calls:,}"
                current_calls = f"{current_calls:,}"
            
            print(f"{sample:<20} {baseline_str:<12} {current_str:<12} {baseline_calls:<15} {current_calls:<15} {status:<15}")
        
        # Show overall summary
        if total_baseline_time > 0 and total_current_time > 0:
            overall_change = ((total_current_time - total_baseline_time) / total_baseline_time) * 100
            if abs(overall_change) < 2:
                overall_status = "~"
            elif overall_change > 0:
                overall_status = f"SLOWER ({overall_change:+.1f}%)"
            else:
                overall_status = f"FASTER ({overall_change:+.1f}%)"
            
            print("-" * 95)
            print(f"{'OVERALL':<20} {total_baseline_time:.3f}s{'':<4} {total_current_time:.3f}s{'':<4} {'':<15} {'':<15} {overall_status:<15}")
        
        # Show detailed hotspot analysis for a representative sample (only in verbose mode)
        if verbose and baseline[config_name] and current[config_name]:
            # Find the best sample for analysis (prefer lotsofmagic, then factory, then simple)
            analysis_sample = None
            for preferred in ['lotsofmagic', 'factory', 'simple']:
                if (preferred in baseline[config_name] and preferred in current[config_name] and
                    baseline[config_name][preferred] and current[config_name][preferred]):
                    analysis_sample = preferred
                    break
            
            if not analysis_sample:
                analysis_sample = next(iter(baseline[config_name].keys()))
            
            baseline_sample = baseline[config_name][analysis_sample]
            current_sample = current[config_name][analysis_sample]
            
            if baseline_sample and current_sample:
                print(f"\nDetailed Hotspot Analysis for '{analysis_sample}' sample:")
                print("=" * 84)
                analyze_hotspots(baseline_sample, current_sample, baseline_name, current_name)

def analyze_hotspots(baseline_data: Dict, current_data: Dict, baseline_name: str, current_name: str):
    """Analyze and compare function hotspots between versions."""
    
    print(f"Top Time-Consuming Functions ({baseline_name} vs {current_name}):")
    baseline_header = f"{baseline_name} Time"
    current_header = f"{current_name} Time"
    print(f"{'Function':<50} {baseline_header:<12} {current_header:<12} {'Change':<10}")
    print("-" * 84)
    
    # Debug: Check what we have
    baseline_funcs = baseline_data.get('top_functions', [])
    current_funcs_list = current_data.get('top_functions', [])
    
    if not baseline_funcs:
        print("No baseline functions found")
        return
        
    if not current_funcs_list:
        print("No current functions found")
        return
    
    # Create function lookup for current data
    current_funcs = {f['function']: f for f in current_funcs_list}
    
    shown_count = 0
    for baseline_func in baseline_funcs[:15]:  # Top 15
        func_name = baseline_func['function']
        baseline_time = baseline_func['cumtime']
        
        # Skip trivial functions
        if baseline_time < 0.001:
            continue
            
        # Truncate long function names
        display_name = func_name[:47] + "..." if len(func_name) > 50 else func_name
        
        if func_name in current_funcs:
            current_time = current_funcs[func_name]['cumtime']
            if baseline_time > 0:
                change_pct = ((current_time - baseline_time) / baseline_time) * 100
                change_str = f"{change_pct:+.1f}%"
            else:
                change_str = "N/A"
            current_str = f"{current_time:.3f}s"
        else:
            current_str = "MISSING"
            change_str = "N/A"
        
        baseline_str = f"{baseline_time:.3f}s"
        print(f"{display_name:<50} {baseline_str:<12} {current_str:<12} {change_str:<10}")
        shown_count += 1
        
        if shown_count >= 10:
            break
    
    if shown_count == 0:
        print("No significant functions found")
    
    # Show compiletools-specific analysis
    print("\nCompiletools Module Analysis:")
    baseline_header = f"{baseline_name} Time"
    current_header = f"{current_name} Time"
    print(f"{'Module/Function':<50} {baseline_header:<12} {current_header:<12} {'Change':<10}")
    print("-" * 84)
    
    baseline_ct = baseline_data.get('compiletools_functions', {})
    current_ct = current_data.get('compiletools_functions', {})
    
    # Get all compiletools functions from both versions
    all_ct_funcs = set(baseline_ct.keys()) | set(current_ct.keys())
    
    # Sort by baseline cumtime
    sorted_funcs = sorted(all_ct_funcs, 
                         key=lambda f: baseline_ct.get(f, {}).get('cumtime', 0), 
                         reverse=True)
    
    for func_name in sorted_funcs[:10]:  # Top 10 compiletools functions
        display_name = func_name[:47] + "..." if len(func_name) > 50 else func_name
        
        baseline_time = baseline_ct.get(func_name, {}).get('cumtime', 0)
        current_time = current_ct.get(func_name, {}).get('cumtime', 0)
        
        if func_name not in baseline_ct:
            baseline_str = "NEW"
            change_str = "NEW"
        elif func_name not in current_ct:
            baseline_str = f"{baseline_time:.3f}s"
            change_str = "REMOVED"
        else:
            baseline_str = f"{baseline_time:.3f}s"
            if baseline_time > 0:
                change_pct = ((current_time - baseline_time) / baseline_time) * 100
                change_str = f"{change_pct:+.1f}%"
            else:
                change_str = "N/A"
        
        current_str = f"{current_time:.3f}s" if func_name in current_ct else "REMOVED"
        
        print(f"{display_name:<50} {baseline_str:<12} {current_str:<12} {change_str:<10}")
    
    # Show I/O and cache analysis if available
    for category, title in [('io_functions', 'I/O Functions'), ('cache_functions', 'Cache Functions')]:
        baseline_cat = baseline_data.get(category, {})
        current_cat = current_data.get(category, {})
        
        if baseline_cat or current_cat:
            print(f"\n{title} Analysis:")
            baseline_header = f"{baseline_name} Time"
            current_header = f"{current_name} Time"
            print(f"{'Function':<50} {baseline_header:<12} {current_header:<12} {'Change':<10}")
            print("-" * 84)
            
            all_funcs = set(baseline_cat.keys()) | set(current_cat.keys())
            sorted_funcs = sorted(all_funcs, 
                                key=lambda f: baseline_cat.get(f, {}).get('cumtime', 0), 
                                reverse=True)
            
            for func_name in sorted_funcs[:5]:  # Top 5 for these categories
                display_name = func_name[:47] + "..." if len(func_name) > 50 else func_name
                
                baseline_time = baseline_cat.get(func_name, {}).get('cumtime', 0)
                current_time = current_cat.get(func_name, {}).get('cumtime', 0)
                
                if func_name not in baseline_cat:
                    baseline_str = "NEW"
                    change_str = "NEW"
                elif func_name not in current_cat:
                    baseline_str = f"{baseline_time:.3f}s"
                    change_str = "REMOVED"
                else:
                    baseline_str = f"{baseline_time:.3f}s"
                    if baseline_time > 0:
                        change_pct = ((current_time - baseline_time) / baseline_time) * 100
                        change_str = f"{change_pct:+.1f}%"
                    else:
                        change_str = "N/A"
                
                current_str = f"{current_time:.3f}s" if func_name in current_cat else "REMOVED"
                
                print(f"{display_name:<50} {baseline_str:<12} {current_str:<12} {change_str:<10}")

def get_current_branch():
    """Get the current git branch name."""
    try:
        result = subprocess.run(["git", "branch", "--show-current"],
                              capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "HEAD"  # Fallback for detached HEAD state

def main():
    current_branch = get_current_branch()

    parser = argparse.ArgumentParser(
        description="Profile ct-cake performance between branches using git worktrees and cProfile",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--baseline-branch",
        default="master",
        help="Baseline branch to compare against"
    )
    parser.add_argument(
        "--current-branch",
        default=current_branch,
        help=f"Current branch to test (default: {current_branch})"
    )
    parser.add_argument(
        "--magic-modes",
        nargs="+",
        default=["direct", "cpp"],
        choices=["direct", "cpp"],
        help="Magic processing modes to test"
    )
    parser.add_argument(
        "--save-profiles",
        action="store_true",
        help="Save individual .prof files for detailed analysis"
    )
    parser.add_argument(
        "--save-results",
        help="Save detailed results to JSON file"
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Keep worktrees after testing (for debugging)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed hotspot analysis and function breakdowns"
    )
    
    args = parser.parse_args()
    
    # Verify we're in a git repo
    result = subprocess.run(["git", "status"], capture_output=True)
    if result.returncode != 0:
        print("Error: Not in a git repository")
        return 1
    
    # Create temporary directory for worktrees
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        worktree_paths = []
        profile_dir = None
        
        if args.save_profiles:
            profile_dir = Path("profiles_worktree")
            profile_dir.mkdir(exist_ok=True)
        
        try:
            print("CT-CAKE DETAILED PERFORMANCE PROFILING (using git worktrees)")
            print(f"Baseline: {args.baseline_branch}")
            print(f"Current: {args.current_branch}")
            print("This approach is completely safe - your working directory won't be affected")
            if args.save_profiles:
                print("Saving individual profile files for detailed analysis")
            
            # Get test samples
            test_samples = get_test_samples()
            print(f"Test samples: {', '.join(test_samples)}")
            
            # Generate test configurations
            configurations = []
            for magic_mode in args.magic_modes:
                configurations.append({
                    "magic_mode": magic_mode,
                    "name": magic_mode
                })
            
            print(f"Test configurations: {', '.join([c['name'] for c in configurations])}")
            
            # Clean up any leftover worktrees first
            print("\nCleaning up any leftover worktrees...")
            cleanup_all_worktrees()
            
            # Set up worktrees
            print("Setting up git worktrees...")
            baseline_worktree = setup_worktree(args.baseline_branch, temp_path)
            current_worktree = setup_worktree(args.current_branch, temp_path)
            worktree_paths = [baseline_worktree, current_worktree]
            
            # Install packages in worktrees
            if not install_package_in_worktree(baseline_worktree):
                print(f"Failed to set up {args.baseline_branch} worktree")
                return 1
                
            if not install_package_in_worktree(current_worktree):
                print(f"Failed to set up {args.current_branch} worktree")
                return 1
            
            # Profile both branches
            baseline_results = profile_branch_worktree(
                args.baseline_branch, baseline_worktree, test_samples, 
                configurations, args.save_profiles, profile_dir
            )
            
            current_results = profile_branch_worktree(
                args.current_branch, current_worktree, test_samples,
                configurations, args.save_profiles, profile_dir
            )
            
            # Compare results
            compare_profiles(baseline_results, current_results, args.baseline_branch, args.current_branch, args.verbose)
            
            # Save results if requested
            if args.save_results:
                results = {
                    "baseline_branch": args.baseline_branch,
                    "current_branch": args.current_branch,
                    "baseline_results": baseline_results,
                    "current_results": current_results,
                    "test_config": {
                        "magic_modes": args.magic_modes,
                        "configurations": [c['name'] for c in configurations],
                        "test_samples": test_samples,
                        "save_profiles": args.save_profiles
                    }
                }
                
                with open(args.save_results, 'w') as f:
                    json.dump(results, f, indent=2, default=str)
                print(f"\nDetailed results saved to: {args.save_results}")
            
            if args.save_profiles and profile_dir:
                print(f"\nProfile files saved in: {profile_dir}/")
                print("Analyze with: python -m pstats <profile_file>")
        
        finally:
            # Clean up worktrees unless requested to keep them
            if not args.keep_worktrees:
                cleanup_worktrees(worktree_paths)
            else:
                print("\nWorktrees preserved:")
                for wt in worktree_paths:
                    if wt.exists():
                        print(f"  {wt}")
                print("Remember to clean them up later with: git worktree remove <path>")

if __name__ == "__main__":
    exit(main())