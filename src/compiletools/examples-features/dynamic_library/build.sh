#!/usr/bin/bash
# Build a dynamic (shared) library and link an executable against it.
#
# This mirrors examples-features/library/build.sh but uses --dynamic instead of
# --static, producing libgreeter.so in mylib/bin/.
set -e

# Build the shared library inside the mylib subdirectory.
pushd mylib >/dev/null
ct-cake --dynamic greeter.cpp "$@"
popd

# Now build the executable, which links against the shared library
# via the //#LDFLAGS=-Lmylib/bin -lgreeter annotation in main.cpp.
ct-cake --auto "$@"

# Run with LD_LIBRARY_PATH so the loader can find libgreeter.so at
# runtime — there's no rpath baked in by default.
LD_LIBRARY_PATH="$(pwd)/mylib/bin" ./bin/*/main
