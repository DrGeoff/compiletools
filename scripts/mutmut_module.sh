#!/usr/bin/env bash
#
# mutmut_module.sh -- per-module mutation testing driver for compiletools.
#
# Full-suite-per-mutant is not viable here (the suite is ~3.5 min).  Instead we
# scope each mutation run to ONE source module and run ONLY that module's
# focused test file as the per-mutant test command.  A focused test file runs
# in a fraction of a second, so a few hundred mutants finish in a couple of
# minutes rather than days.
#
# mutmut 3.x reads its config from ./pyproject.toml ([tool.mutmut]) or, if that
# section is absent, ./setup.cfg ([mutmut]).  There is no CLI override for the
# per-module knobs (source scope + test selection), so this driver writes a
# throw-away setup.cfg for the requested module, runs mutmut, prints the
# survivors + machine-readable stats, then removes the setup.cfg again.  The
# repo intentionally keeps [tool.mutmut] OUT of pyproject.toml so this
# per-module setup.cfg is what mutmut picks up.
#
# Usage:
#   scripts/mutmut_module.sh <module> [test_file]
#
#   <module>     Module under src/compiletools, with or without .py
#                (e.g. "utils", "utils.py", or "src/compiletools/utils.py").
#   [test_file]  Optional focused test file.  Defaults to
#                src/compiletools/test_<module>.py.
#
# Environment:
#   MUTMUT_FLOOR   If set to a percentage (e.g. 65), the script exits non-zero
#                  when the module's mutation score falls below it.  Unset =>
#                  report only, always exit 0 (unless mutmut itself errors).
#   MUTMUT_KEEP    If set to 1, leave the generated setup.cfg in place on exit
#                  (useful for `uv run mutmut show <mutant>` / `mutmut browse`).
#
# Score definition:
#   detected = killed + timeout          (timeout == behaviour change caught)
#   assessed = killed + timeout + survived   (mutants that had test coverage)
#   score    = 100 * detected / assessed
#   Mutants with NO covering test ("no_tests") are a coverage gap, not an
#   assertion gap, so they are excluded from the score (but still reported).
#   A module that yields zero assessed mutants scores N/A and always passes.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <module> [test_file]" >&2
    exit 2
fi

# Normalise the module argument to a bare name (strip dir + .py).
raw="$1"
module="$(basename "$raw")"
module="${module%.py}"

source_path="src/compiletools/${module}.py"
test_file="${2:-src/compiletools/test_${module}.py}"

if [[ ! -f "$source_path" ]]; then
    echo "error: source module not found: $source_path" >&2
    exit 2
fi
if [[ ! -f "$test_file" ]]; then
    echo "error: focused test file not found: $test_file" >&2
    echo "       pass one explicitly as the second argument if it is named differently." >&2
    exit 2
fi

# uv resyncs the project env on `uv run`, which drops the mutation extra unless
# it is requested.  Make sure mutmut is present before we start.
uv sync --extra dev --extra mutation >/dev/null 2>&1 || true

SETUP_CFG="$REPO_ROOT/setup.cfg"
PRE_EXISTING_SETUP_CFG=0
[[ -f "$SETUP_CFG" ]] && PRE_EXISTING_SETUP_CFG=1

cleanup() {
    if [[ "${MUTMUT_KEEP:-0}" != "1" && "$PRE_EXISTING_SETUP_CFG" == "0" ]]; then
        rm -f "$SETUP_CFG"
    fi
}
trap cleanup EXIT

if [[ "$PRE_EXISTING_SETUP_CFG" == "1" ]]; then
    echo "error: refusing to overwrite an existing setup.cfg; move it aside first." >&2
    exit 2
fi

# Write the per-module mutmut config.
#
#  source_paths                        -- copy the WHOLE package into mutants/
#                                         so every intra-package import resolves;
#  only_mutate                         -- but mutate just this one module;
#  pytest_add_cli_args_test_selection  -- run only this module's focused tests.
cat > "$SETUP_CFG" <<EOF
[mutmut]
source_paths=src/compiletools
only_mutate=$source_path
pytest_add_cli_args_test_selection=$test_file
EOF

echo "=============================================================="
echo " mutmut per-module run"
echo "   module     : $source_path"
echo "   test file  : $test_file"
echo "   floor      : ${MUTMUT_FLOOR:-<none> (report only)}"
echo "=============================================================="

# Fresh tree each run so stale mutants can't skew the stats.
rm -rf "$REPO_ROOT/mutants"

# Run the mutation campaign.  mutmut exits non-zero when survivors exist; that
# is expected data, not a driver failure, so don't let it trip `set -e`.
set +e
uv run mutmut run
set -e

echo
echo "----- surviving mutants (the weak-test signal) -----"
uv run mutmut results || true

echo
echo "----- machine-readable stats -----"
uv run mutmut export-cicd-stats || true
stats_json="$REPO_ROOT/mutants/mutmut-cicd-stats.json"
if [[ ! -f "$stats_json" ]]; then
    # mutmut writes no stats file when a module yields zero mutants (e.g. a
    # pure dataclass with no mutable literals/operators). Synthesise an empty
    # record so the score step treats it as N/A and passes.
    echo '{"killed":0,"survived":0,"total":0,"no_tests":0,"timeout":0}' > "$stats_json"
    echo "(no mutants generated for this module -- empty stats synthesised)"
fi
cat "$stats_json"
echo

# Compute the score and enforce the floor (if any) in Python for robustness.
uv run python - "$stats_json" "${MUTMUT_FLOOR:-}" "$module" <<'PY'
import json
import sys

stats_path, floor_raw, module = sys.argv[1], sys.argv[2], sys.argv[3]
with open(stats_path) as f:
    s = json.load(f)

killed = s.get("killed", 0)
survived = s.get("survived", 0)
timeout = s.get("timeout", 0)
no_tests = s.get("no_tests", 0)
total = s.get("total", 0)

detected = killed + timeout
assessed = killed + timeout + survived

print()
print(f"module        : {module}")
print(f"total mutants : {total}")
print(f"killed        : {killed}")
print(f"timeout       : {timeout}  (counted as detected)")
print(f"survived      : {survived}  (weak-test signal)")
print(f"no_tests      : {no_tests}  (coverage gap; excluded from score)")

if assessed == 0:
    print("mutation score: N/A (no assessed mutants) -- PASS")
    sys.exit(0)

score = 100.0 * detected / assessed
print(f"mutation score: {score:.1f}%  ({detected}/{assessed} detected)")

if not floor_raw:
    sys.exit(0)

floor = float(floor_raw)
if score + 1e-9 < floor:
    print(f"FAIL: score {score:.1f}% is below floor {floor:.1f}%")
    sys.exit(1)
print(f"PASS: score {score:.1f}% >= floor {floor:.1f}%")
sys.exit(0)
PY
