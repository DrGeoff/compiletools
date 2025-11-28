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
        raise RuntimeError("Git executable not found. Make sure git is installed and in PATH.")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"Unexpected error running git command '{cmd}': {e}")

def get_index_hashes() -> Dict[Path, str]:
    """
    Return blob hashes for all tracked files from git index:
    { path: blob_sha }
    Uses --stage without --debug to avoid opening all files.
    Only includes files that actually exist on disk.
    """
    cmd = "git ls-files --stage"
    output = run_git(cmd)

    # Get git root once outside the loop
    git_root = find_git_root()

    hashes = {}
    for line in output.splitlines():
        # Parse "<mode> <blob_sha> <stage> <path>"
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue

        mode, blob_sha, stage, path_str = parts
        # Since we run git commands from git root, paths are relative to git root
        abs_path_str = os.path.join(git_root, path_str)

        # Skip files that have been deleted but not committed
        if not wrappedos.isfile(abs_path_str):
            continue

        hashes[Path(wrappedos.realpath(abs_path_str))] = blob_sha

    return hashes

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
    Given a list of paths, return { path: blob_sha } using batched git calls.
    Converts absolute paths to relative paths (relative to git root) for git compatibility.
    Batches calls to avoid "Too many open files" errors from git hash-object.
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

    # Dynamically determine batch size based on system fd limit
    # git hash-object opens all files simultaneously, so we need to stay under the limit
    import resource
    try:
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Use 95% of soft limit, leaving some headroom for Python, git, and other processes
        batch_size = int(soft_limit * 0.95)
    except Exception:
        # Fallback if we can't get the limit (non-Unix systems)
        batch_size = 1000

    result = {}

    for i in range(0, len(relative_paths), batch_size):
        batch_rel_paths = relative_paths[i:i+batch_size]
        batch_path_mapping = path_mapping[i:i+batch_size]

        input_data = "\n".join(batch_rel_paths) + "\n"
        output = run_git("git hash-object --stdin-paths", input_data=input_data)
        shas = output.splitlines()
        result.update(dict(zip(batch_path_mapping, shas)))

    return result

def get_current_blob_hashes() -> Dict[Path, str]:
    """
    Get the blob hash for every tracked file as it exists now.
    Simply uses index hashes directly - git status will detect real changes.
    This is much faster and avoids file descriptor exhaustion.
    """
    return get_index_hashes()

def get_modified_but_unstaged_files() -> list[Path]:
    """
    Get list of tracked files that have been modified but not staged.
    Uses git diff-files which only reports changed files without opening them all.
    Filters out deleted files since they can't be hashed.
    """
    cmd = "git diff-files --name-only"
    output = run_git(cmd)
    if not output:
        return []

    git_root = find_git_root()
    # Only return files that actually exist (exclude deleted files)
    result = []
    for line in output.splitlines():
        abs_path_str = os.path.join(git_root, line)
        # Use cached isfile check to avoid repeated filesystem operations
        if wrappedos.isfile(abs_path_str):
            result.append(Path(wrappedos.realpath(abs_path_str)))
    return result

def get_complete_working_directory_hashes() -> Dict[Path, str]:
    """
    Get blob hashes for ALL files in the working directory:
    - Tracked files (from git index, with updates for modified-but-unstaged files)
    - Untracked files (excluding ignored files)

    Returns complete working directory content fingerprint.
    Efficient approach: use index hashes, then only re-hash modified unstaged files.
    """
    import gc

    # Get hashes for all tracked files from index (no file opening)
    tracked_hashes = get_index_hashes()

    # Identify modified but unstaged files and re-hash only those
    modified_files = get_modified_but_unstaged_files()
    if modified_files:
        modified_hashes = batch_hash_objects(modified_files)
        tracked_hashes.update(modified_hashes)

    # Get all untracked files and hash them in batches
    untracked_files = get_untracked_files()
    if untracked_files:
        untracked_hashes = batch_hash_objects(untracked_files)
    else:
        untracked_hashes = {}

    # Clean up before returning
    del untracked_files
    del modified_files
    gc.collect()

    # Combine results: tracked (with modified updates) + untracked
    return {**tracked_hashes, **untracked_hashes}

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
