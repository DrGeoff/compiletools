#!/bin/bash
# DEPRECATED: This script is deprecated in favor of ct-cleanup-locks
#
# Please use the new Python-based tool instead:
#   ct-cleanup-locks [--dry-run] [--objdir=/path/to/objects]
#
# The new tool:
# - Integrates with ct.conf configuration (respects lock timeouts, objdir settings)
# - Uses the same lock detection logic as the build system
# - Provides better error handling and reporting
# - Supports all the same features as this script
#
# This script will be removed in a future release.
# -----------------------------------------------------------------------------

# Script to check and clean up stale lockdirs on remote hosts
# Usage: ./cleanup_remote_locks.sh <shared_object_directory>

echo "WARNING: This script is deprecated. Use 'ct-cleanup-locks' instead." >&2
echo "         Run 'ct-cleanup-locks --help' for more information." >&2
echo "" >&2

set -e

if [ $# -lt 1 ]; then
    echo "Usage: $0 <shared_object_directory>" >&2
    echo "Example: $0 /shared/build" >&2
    exit 1
fi

SHARED_OBJECT_DIR="$1"
DRY_RUN=${DRY_RUN:-0}

if [ "$DRY_RUN" = "1" ]; then
    echo "DRY RUN MODE: No locks will be removed"
fi

echo "Scanning for lockdirs in: $SHARED_OBJECT_DIR"
echo ""

# Find all .lockdir directories
for lockdir in $(find "$SHARED_OBJECT_DIR" -type d -name "*.lockdir" 2>/dev/null); do
    pid_file="$lockdir/pid"

    if [ ! -f "$pid_file" ]; then
        echo "WARNING: Lock directory missing pid file: $lockdir"
        continue
    fi

    # Read lock info (format: hostname:pid)
    lock_info=$(cat "$pid_file" 2>/dev/null || echo "")

    if [ -z "$lock_info" ]; then
        echo "WARNING: Empty pid file: $pid_file"
        continue
    fi

    # Parse hostname and pid
    lock_host="${lock_info%%:*}"
    lock_pid="${lock_info##*:}"

    if [ -z "$lock_host" ] || [ -z "$lock_pid" ]; then
        echo "WARNING: Invalid lock info format in $pid_file: $lock_info"
        continue
    fi

    # Get lock age
    if [ "$(uname)" = "Linux" ]; then
        lock_mtime=$(stat -c %Y "$lockdir" 2>/dev/null || echo 0)
    else
        lock_mtime=$(stat -f %m "$lockdir" 2>/dev/null || echo 0)
    fi

    now=$(date +%s)
    lock_age_sec=$((now - lock_mtime))

    echo "Lock: $lockdir"
    echo "  Host: $lock_host"
    echo "  PID: $lock_pid"
    echo "  Age: ${lock_age_sec}s"

    # Check if process is still running
    current_host=$(uname -n)
    is_stale=0

    if [ "$lock_host" = "$current_host" ]; then
        # Local lock - use kill -0
        if ! kill -0 "$lock_pid" 2>/dev/null; then
            echo "  Status: STALE (local process not running)"
            is_stale=1
        else
            echo "  Status: ACTIVE (local process running)"
        fi
    else
        # Remote lock - SSH to check
        echo "  Checking remote host..."
        if ssh -o ConnectTimeout=5 -o BatchMode=yes "$lock_host" "kill -0 $lock_pid 2>/dev/null" 2>/dev/null; then
            echo "  Status: ACTIVE (remote process running)"
        else
            ssh_exit=$?
            if [ $ssh_exit -eq 255 ]; then
                echo "  Status: UNKNOWN (SSH connection failed)"
            else
                echo "  Status: STALE (remote process not running)"
                is_stale=1
            fi
        fi
    fi

    # Clean up stale locks
    if [ $is_stale -eq 1 ]; then
        if [ "$DRY_RUN" = "1" ]; then
            echo "  Action: Would remove lockdir"
        else
            if rm -rf "$lockdir" 2>/dev/null; then
                echo "  Action: REMOVED lockdir"
            else
                echo "  Action: FAILED to remove lockdir (check permissions)"
            fi
        fi
    fi

    echo ""
done

echo "Scan complete"
