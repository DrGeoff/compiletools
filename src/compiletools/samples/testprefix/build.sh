#!/usr/bin/bash
# Demonstrates TESTPREFIX -- a command prefix prepended to every unit
# test invocation in the runtests phase.
#
# TESTPREFIX is set the same way any other compiletools setting is:
# environment variable, --variant=...conf, or in any ct.conf in the
# search path. The most common values are wrappers like:
#
#   timeout 30                    -- bound test runtime
#   valgrind --quiet --error-exitcode=1
#                                 -- memory-correctness gate
#   gdb --batch -ex run -ex bt    -- automatic post-mortem
#
# Below we use `timeout 5` because it's universally available and
# doesn't require any third-party tooling.
set -e

export TESTPREFIX="timeout 5"
ct-cake --auto "$@"

# Equivalently, pass --TESTPREFIX on the CLI:
#
#   ct-cake --auto --TESTPREFIX="timeout 5" "$@"
#
# Or set it in a per-project ct.conf:
#
#   echo 'TESTPREFIX=timeout 5' >> ct.conf.d/ct.conf
