#!/usr/bin/bash
# Build the version banner with explicit project name / version macros.
#
# Pass --project-name and --project-version directly:
ct-cake --auto \
    --project-name=demo_app \
    --project-version=1.2.3 \
    "$@"

./bin/*/version_banner

# Or use the *-cmd variants to derive the values from a command:
#
# ct-cake --auto \
#     --project-name-cmd='basename "$(pwd)"' \
#     --project-version-cmd='git describe --always --dirty' \
#     "$@"

# Precedence check: when both --project-version and --project-version-cmd
# are supplied, the explicit value must win and the cmd must not be the
# one that shows up in the binary's output (build_inputs.py's
# _project_macro_value: `if not value and cmd:`). Grep the binary's own
# stdout for the value-derived string so a precedence inversion -- the
# cmd's "9.9.9" winning instead -- fails this script via its exit code.
ct-cake --auto \
    --project-name=demo_app \
    --project-version=1.2.3 \
    --project-version-cmd='echo 9.9.9' \
    "$@"

./bin/*/version_banner | grep -q '^version=1.2.3$' || {
    echo "FAIL: --project-version did not win over --project-version-cmd" >&2
    exit 1
}
