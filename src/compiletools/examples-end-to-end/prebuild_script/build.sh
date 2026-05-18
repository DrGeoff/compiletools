#!/usr/bin/bash
# Build the version banner, generating "build/version.h" via a
# --prebuild-script hook before headerdeps walks the include graph.
set -eu

ct-cake --auto \
    --prebuild-script='./gen_version.sh build/version.h' \
    "$@"

./bin/*/version_banner
