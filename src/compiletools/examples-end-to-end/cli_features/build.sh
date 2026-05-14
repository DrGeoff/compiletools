#!/usr/bin/bash
# Tour of ct-cake CLI features that don't naturally belong to any
# single C++ idiom. Every command below is a one-liner -- the goal is
# to be a quick reference for what each flag actually does.
set -e

GITROOT=$(git rev-parse --show-toplevel)

step() { echo; echo "=========================================="; echo "  $*"; echo "=========================================="; }

step "1. Build everything (default)"
ct-cake --auto

step "2. Build a single target with a custom output name (-o / --output)"
# -o renames the produced binary; useful for scripted builds that need
# a stable name independent of the source filename.
ct-cake alpha.cpp -o alpha_renamed
ls bin/*/alpha_renamed

step "3. --disable-tests / --disable-exes (subset of --auto)"
ct-cake --auto --disable-tests   # build exes only
ct-cake --auto --disable-exes    # build tests only (no tests here, so no-op)

step "4. --build-only-changed: limit to files mentioned"
# Useful in CI: feed in `git diff --name-only master` so only the
# binaries that depend on the changed sources get rebuilt and re-tested.
ct-cake --build-only-changed "$(pwd)/beta.cpp"

step "5. --compilation-database: produce compile_commands.json"
# Generates compile_commands.<variant>.json at the gitroot and
# atomically retargets a sibling compile_commands.json symlink.
# Consumed by clangd, clang-tidy, vscode, etc.
ct-cake --auto --compilation-database
ls -la "$GITROOT"/compile_commands*.json

step "6. --timing: per-build timing report"
# Writes timing.json into the per-invocation diagnostics dir and
# prints a summary table. Inspect later with ct-timing-report.
ct-cake --auto --timing

step "7. --diagnostics-dir: route per-build artefacts to a stable path"
ct-cake --auto --timing --diagnostics-dir=/tmp/ct-diag-$$
ls /tmp/ct-diag-$$

step "8. --use-mtime=True: restore classical mtime semantics"
# CAS-only mode (the default --use-mtime=False) ignores mtime, so
# `touch alpha.cpp` does NOT trigger a rebuild. With --use-mtime=True
# the Make/Ninja backends honor mtime and a touch forces a rebuild.
touch alpha.cpp
ct-cake alpha.cpp --use-mtime=True

step "9. --cas-objdir / --cas-pchdir / --cas-pcmdir / --cas-exedir overrides"
# Point any of the four CAS layers at a custom path -- handy for
# pinning a CI cache outside the gitroot or sharing across hosts.
ct-cake --auto \
    --cas-objdir=/tmp/ct-cache-$$/obj \
    --cas-pchdir=/tmp/ct-cache-$$/pch \
    --cas-pcmdir=/tmp/ct-cache-$$/pcm \
    --cas-exedir=/tmp/ct-cache-$$/exe

step "10. --clean: remove build artifacts"
# Removes Makefile/Ninja files and bin/, but preserves the object CAS
# (other sub-projects in the same workspace still rely on it).
ct-cake --clean

step "11. --realclean: --clean PLUS this build's CAS objects + bindir"
# Selective: only this build's contributions are removed from the CAS;
# objects belonging to other projects stay.
ct-cake --realclean
