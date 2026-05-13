#!/usr/bin/bash
# Demonstrates ct-cake's variant-axis composition.
#
# Each invocation below resolves a different multi-axis variant; the
# canonical (sorted) form is what ends up in bin/<canonical>/ and in
# the cas-objdir/cas-pchdir/cas-pcmdir/cas-exedir paths.
#
#   --variant input        canonical name on disk
#   ---------------------- ----------------------
#   gcc,debug              gcc.debug
#   gcc,release            gcc.release
#   gcc,debug,asan         gcc.debug.asan
#   clang,release,lto      clang.release.lto
#   blank                  blank   (env-only build, see below)
#
# Comma, dot and whitespace are interchangeable separators —
# --variant=gcc.debug.asan and --variant="gcc debug asan" parse the
# same way.
set -e

run_variant() {
    local v="$1"
    echo
    echo "=========================================="
    echo "  --variant=$v"
    echo "=========================================="
    ct-cake --auto --variant="$v" axis_probe.cpp
    # bin/<variant>/ uses the canonicalized name.
    ./bin/*/axis_probe || true
}

run_variant gcc,debug
run_variant gcc,release
run_variant gcc,debug,asan
# Uncomment if clang is on PATH:
# run_variant clang,release,lto

# `blank` is the special axis that inherits everything from the
# environment — useful when the user wants CC/CXX/CFLAGS/CXXFLAGS to
# come from outside ct entirely:
#
# CXX=g++ CXXFLAGS='-O2 -std=c++17' ct-cake --auto --variant=blank axis_probe.cpp
