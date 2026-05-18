#!/usr/bin/bash
# Build env_printer and immediately run it inside a known environment,
# via a --postbuild-script hook.
set -eu

ct-cake --auto \
    --postbuild-script='./run_with_env.sh' \
    "$@"
