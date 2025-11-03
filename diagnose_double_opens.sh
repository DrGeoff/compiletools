#!/bin/bash
# Diagnostic script for test_file_open_efficiency.py failures
# This script helps identify why git hash registry might be failing
#
# Usage: cd /path/to/compiletools && bash /tmp/diagnose_double_opens.sh

echo "======================================================================"
echo "Git Hash Registry Diagnostics for compiletools"
echo "======================================================================"
echo ""

# Try to find and activate venv
if [ -f ".venv/bin/activate" ]; then
    echo "Found .venv, activating..."
    source .venv/bin/activate
elif [ -f "$HOME/.venv312/bin/activate" ]; then
    echo "Found ~/.venv312, activating..."
    source "$HOME/.venv312/bin/activate"
else
    echo "No venv found, using system Python"
fi
echo ""

echo "1. File Descriptor Limits:"
echo "   Soft limit (ulimit -n): $(ulimit -n)"
echo "   Hard limit (ulimit -Hn): $(ulimit -Hn)"
echo ""

echo "2. Git Availability:"
if which git >/dev/null 2>&1; then
    echo "   ✓ Git found: $(git --version)"
else
    echo "   ✗ ERROR: git not in PATH"
fi
echo ""

echo "3. Git Repository Status:"
if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "   ✓ In git repo: $(git rev-parse --show-toplevel)"
else
    echo "   ✗ ERROR: Not in a git repository or .git is corrupted"
fi
echo ""

echo "4. Test Git Commands:"
echo "   Testing 'git ls-files --stage' (first 3 files):"
if git ls-files --stage 2>/dev/null | head -3; then
    echo "   ✓ Success"
else
    echo "   ✗ FAILED"
fi
echo ""

echo "   Testing 'git hash-object':"
echo "test" > /tmp/test_hash_$$.txt
if hash=$(git hash-object /tmp/test_hash_$$.txt 2>&1); then
    echo "   ✓ Success: $hash"
else
    echo "   ✗ FAILED: $hash"
fi
rm -f /tmp/test_hash_$$.txt
echo ""

echo "5. Python Environment:"
python3 --version
echo ""

echo "6. Python Can Import compiletools:"
if python3 -c "import sys; sys.path.insert(0, 'src'); import compiletools.global_hash_registry" 2>&1; then
    echo "   ✓ Import successful"
else
    echo "   ✗ Import failed (see error above)"
fi
echo ""

echo "7. Try Loading Hash Registry (CRITICAL TEST):"
python3 << 'PYEOF'
import sys
sys.path.insert(0, 'src')

try:
    import compiletools.global_hash_registry as ghr

    # Clear any cached state
    ghr.clear_global_registry()

    # Try loading with verbose output
    print("   Attempting to load hashes from git...")
    ghr.load_hashes(verbose=3)
    stats = ghr.get_registry_stats()
    print(f"   ✓ SUCCESS: Loaded {stats['total_files']} files from git")
    print(f"   Registry hits: {stats.get('registry_hits', 0)}")
    print(f"   Computed hashes: {stats.get('computed_hashes', 0)}")
except Exception as e:
    import traceback
    print(f"   ✗ FAILED: {type(e).__name__}: {e}")
    print("\n   Full traceback:")
    traceback.print_exc()
    print("\n   *** This means the git hash registry is not working! ***")
    print("   Files will be opened twice during builds (inefficient)")
PYEOF

echo ""
echo "======================================================================"
echo "Interpretation:"
echo "======================================================================"
echo ""
echo "If step 7 FAILED:"
echo "  - The git hash registry cannot load"
echo "  - This causes test_file_open_efficiency.py to fail"
echo "  - Files are opened 2x: once for hash computation, once for analysis"
echo ""
echo "If step 7 SUCCEEDED:"
echo "  - The git hash registry IS working"
echo "  - The test failure must have a different cause"
echo "  - Please report this along with Python version and OS details"
echo ""
echo "Common failure causes:"
echo "  1. Not in git repository root when running tests"
echo "  2. Git executable not in PATH"
echo "  3. .git directory corrupted"
echo "  4. File descriptor limits too low (< 1024)"
echo "  5. Python subprocess module issues with git"
echo "  6. Missing dependencies (stringzilla, etc.)"
echo ""
