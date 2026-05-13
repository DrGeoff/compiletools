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
