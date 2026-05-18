#!/bin/sh
# Post-build hook: set DEMO_ENV_VAR and run the freshly-built binary
# in that known environment.
#
# ct-cake invokes this AFTER backend.execute("build") succeeds but
# BEFORE _copyexes publishes to the top-level bin/. With a default
# --bindir, the binary lives at bin/<variant>/env_printer; with an
# explicit --bindir=<path> override (CI test pattern) it lives at
# <path>/env_printer with no variant subdir. Probe both.
set -eu

export DEMO_ENV_VAR="hello-from-postbuild"

for candidate in bin/*/env_printer bin/env_printer; do
    if [ -x "$candidate" ]; then
        exec "$candidate"
    fi
done

echo "run_with_env.sh: env_printer binary not found under bin/" >&2
exit 1
