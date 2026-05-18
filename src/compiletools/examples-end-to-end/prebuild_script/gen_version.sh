#!/bin/sh
# Generate a header that bakes a version string in as a macro.
# Invoked by ct-cake's --prebuild-script hook; the path of the header
# to write is passed as $1.
#
# Falls back to a placeholder when not in a git checkout so the example
# is runnable from a plain copy of the source tree.
set -eu

out="$1"
mkdir -p "$(dirname "$out")"

if version=$(git describe --always --dirty 2>/dev/null); then
    :
else
    version="0.0.0-no-git"
fi

# Atomic write so a concurrent re-run never sees a half-written header.
tmp="${out}.tmp.$$"
printf '#define DEMO_PREBUILD_VERSION "%s"\n' "$version" > "$tmp"
mv -f "$tmp" "$out"
