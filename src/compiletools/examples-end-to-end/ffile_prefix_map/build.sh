#!/usr/bin/bash
# Demonstrates the workspace-relative compile paths feature (Round 3
# in CLAUDE.md). The -ffile-prefix-map auto-injection rewrites the
# gitroot prefix in DWARF / __FILE__ / .d paths to a stable target
# (default ".") so binaries built in different workspaces are byte-
# identical and share CAS entries across users.
set -e

# Default behaviour: -ffile-prefix-map=<gitroot>=. is auto-injected.
ct-cake --auto path_probe.cpp
EXE=$(ls bin/*/path_probe | head -n1)
echo
echo "Auto-injected -ffile-prefix-map=<gitroot>=. (default):"
"$EXE"

# Override the target prefix:
ct-cake --auto path_probe.cpp --ffile-prefix-map-target=/build
EXE=$(ls bin/*/path_probe | head -n1)
echo
echo "With --ffile-prefix-map-target=/build:"
"$EXE"

# Opt out per-slot by supplying any -f{file,debug,macro,canon}-prefix-map
# yourself in CXXFLAGS -- ct-cake then leaves that slot alone.
ct-cake --auto path_probe.cpp --append-CXXFLAGS='-ffile-prefix-map=/foo=/bar'
EXE=$(ls bin/*/path_probe | head -n1)
echo
echo "User-supplied -ffile-prefix-map (auto-injection skipped):"
"$EXE"

# Byte-identity check across two workspaces (manual exercise):
#
#   git clone <repo> /tmp/A && cd /tmp/A/src/compiletools/samples/ffile_prefix_map
#   ct-cake --auto path_probe.cpp
#   sha256sum bin/*/path_probe.o.* (or the cas-objdir entry)
#
#   git clone <repo> /tmp/B && cd /tmp/B/src/compiletools/samples/ffile_prefix_map
#   ct-cake --auto path_probe.cpp
#   sha256sum bin/*/path_probe.o.*
#
# Both sha256sums match because the absolute path no longer leaks into
# the object file's bytes.
