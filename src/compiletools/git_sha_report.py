import subprocess
import shlex
import os
from pathlib import Path
from typing import Dict, Tuple
from compiletools import wrappedos
from compiletools.git_utils import find_git_root

def run_git(cmd: str, input_data: str = None) -> str:
    """Run a git command from the repository root, optionally with stdin, and return stdout."""
    try:
        git_root = find_git_root()
    except Exception as e:
        raise RuntimeError(f"Failed to find git repository root: {e}")
    
    try:
        result = subprocess.run(
            shlex.split(cmd),
            input=input_data,
            capture_output=True,
            text=True,
            check=False,  # Handle errors manually for better messages
            cwd=git_root
        )
        
        if result.returncode != 0:
            error_msg = f"Git command failed: {cmd}\n"
            error_msg += f"Working directory: {git_root}\n"
            error_msg += f"Return code: {result.returncode}\n"
            if input_data:
                error_msg += f"Input data: {input_data[:500]}{'...' if len(input_data) > 500 else ''}\n"
            if result.stderr:
                error_msg += f"Error output: {result.stderr}"
            raise RuntimeError(error_msg)
        
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError(f"Git executable not found. Make sure git is installed and in PATH.")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"Unexpected error running git command '{cmd}': {e}")

def get_index_metadata() -> Dict[Path, Tuple[str, int, int]]:
    """
    Return index metadata for all tracked files:
    { path: (blob_sha, size, mtime) }
    """
    cmd = "git ls-files --stage --debug"
    output = run_git(cmd)

    metadata = {}
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Parse "<mode> <blob_sha> <stage> <path>"
        parts = line.split(None, 3)
        if len(parts) != 4:
            i += 1
            continue
            
        mode, blob_sha, stage, path_str = parts
        # Since we run git commands from git root, paths are relative to git root
        git_root = find_git_root()
        abs_path_str = os.path.join(git_root, path_str)
        path = Path(wrappedos.realpath(abs_path_str))
        i += 1

        size = None
        mtime = None
        # Read debug info lines (indented with spaces)
        while i < len(lines) and lines[i].startswith("  "):
            debug_line = lines[i].strip()
            if debug_line.startswith("size:"):
                size = int(debug_line.split()[1])
            elif debug_line.startswith("mtime:"):
                # Handle format "mtime: seconds:nanoseconds" by taking only seconds
                mtime_str = debug_line.split()[1]
                mtime = int(mtime_str.split(':')[0])
            i += 1

        if size is not None and mtime is not None:
            metadata[path] = (blob_sha, size, mtime)
    return metadata

def get_file_stat(path: Path) -> Tuple[int, int]:
    """Return (size, mtime_seconds) for a file on disk."""
    st = path.stat()
    return st.st_size, st.st_mtime_ns // 1_000_000_000

def get_untracked_files() -> list[Path]:
    """
    Get all untracked files in the working directory.
    Returns files that are not tracked by git and not ignored.
    """
    cmd = "git ls-files --others --exclude-standard"
    output = run_git(cmd)
    if not output:
        return []
    # Since we run git commands from git root, paths are relative to git root
    git_root = find_git_root()
    return [Path(wrappedos.realpath(os.path.join(git_root, line))) for line in output.splitlines()]

def batch_hash_objects(paths) -> Dict[Path, str]:
    """
    Given a list of paths, return { path: blob_sha } using one git call.
    Converts absolute paths to relative paths (relative to git root) for git compatibility.
    """
    if not paths:
        return {}
    
    git_root = find_git_root()
    # Convert absolute paths to relative paths for git hash-object
    # git hash-object --stdin-paths expects paths relative to cwd (which is git_root in run_git)
    relative_paths = []
    path_mapping = []  # Track which original path maps to which relative path
    
    for p in paths:
        abs_path = Path(p).resolve()
        
        # Skip directories - git hash-object only works on files
        if abs_path.is_dir():
            continue
            
        try:
            rel_path = abs_path.relative_to(git_root)
            relative_paths.append(str(rel_path))
            path_mapping.append(p)
        except ValueError:
            # Path is outside git root, skip it
            continue
    
    if not relative_paths:
        return {}
    
    input_data = "\n".join(relative_paths) + "\n"
    output = run_git("git hash-object --stdin-paths", input_data=input_data)
    shas = output.splitlines()
    return dict(zip(path_mapping, shas))

def get_current_blob_hashes() -> Dict[Path, str]:
    """
    Get the blob hash for every tracked file as it exists now.
    Uses index metadata for unchanged files and re-hashes changed ones.
    """
    index_metadata = get_index_metadata()
    unchanged = {}
    changed_paths = []

    for path, (blob_sha_index, size_index, mtime_index) in index_metadata.items():
        try:
            size_fs, mtime_fs = get_file_stat(path)
        except FileNotFoundError:
            # If the file is missing, we could skip or mark as None
            continue

        if size_fs == size_index and mtime_fs == mtime_index:
            unchanged[path] = blob_sha_index
        else:
            changed_paths.append(path)

    # Batch-hash changed files
    changed_hashes = batch_hash_objects(changed_paths)

    # Merge results
    return {**unchanged, **changed_hashes}

def get_complete_working_directory_hashes() -> Dict[Path, str]:
    """
    Get blob hashes for ALL files in the working directory:
    - Tracked files (using efficient index metadata when possible)
    - Untracked files (excluding ignored files)
    
    Returns complete working directory content fingerprint.
    Uses only ONE batch_hash_objects call for optimal performance.
    """
    # Get index metadata for tracked files
    index_metadata = get_index_metadata()
    unchanged_tracked = {}
    changed_tracked_paths = []

    # Process tracked files, separating unchanged from changed
    for path, (blob_sha_index, size_index, mtime_index) in index_metadata.items():
        try:
            size_fs, mtime_fs = get_file_stat(path)
        except FileNotFoundError:
            # If the file is missing, skip it
            continue

        if size_fs == size_index and mtime_fs == mtime_index:
            # File unchanged, use cached hash from index
            unchanged_tracked[path] = blob_sha_index
        else:
            # File changed, needs to be hashed
            changed_tracked_paths.append(path)

    # Get all untracked files
    untracked_files = get_untracked_files()
    
    # SINGLE batch hash call for all files that need hashing
    all_files_to_hash = changed_tracked_paths + untracked_files
    if all_files_to_hash:
        new_hashes = batch_hash_objects(all_files_to_hash)
    else:
        new_hashes = {}
    
    # Combine results: unchanged tracked + newly hashed tracked + untracked
    return {**unchanged_tracked, **new_hashes}

def main():
    """Main entry point for ct-git-sha-report command."""
    import sys
    
    # Check for command line arguments
    include_untracked = "--all" in sys.argv or "--untracked" in sys.argv
    
    if include_untracked:
        print("# Complete working directory fingerprint (tracked + untracked files)")
        blob_map = get_complete_working_directory_hashes()
    else:
        print("# Tracked files only (use --all or --untracked to include untracked files)")
        blob_map = get_current_blob_hashes()
    
    for path, sha in sorted(blob_map.items()):
        print(f"{sha}  {path}")
        print(f"{sha}  {path}")
