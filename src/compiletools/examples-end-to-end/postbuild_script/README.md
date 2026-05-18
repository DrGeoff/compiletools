# postbuild_script Sample

Demonstrates ct-cake's `--postbuild-script` hook, which runs a
user-supplied script after a successful build but before executables
are copied to the top-level `bin/`. This example uses the hook to
launch the freshly-built binary inside a known environment.

## Run

```bash
./build.sh
```

Expected output (after the ct-cake banner):

```
DEMO_ENV_VAR=hello-from-postbuild
```

## What's happening

* `env_printer.cpp` is a one-line program that prints
  `DEMO_ENV_VAR=<getenv-value-or-(unset)>` to stdout.
* `run_with_env.sh` is the post-build hook. It exports
  `DEMO_ENV_VAR=hello-from-postbuild`, then `exec`s the binary at
  `bin/*/env_printer`.
* `build.sh` invokes `ct-cake --auto --postbuild-script=./run_with_env.sh`,
  so the hook runs automatically as part of the build.

If you run `env_printer` directly without the hook, it prints
`DEMO_ENV_VAR=(unset)` (or whatever value is in your shell's environment).

## When to use this pattern

A post-build script that sets up a known environment and runs the
binary is useful for:

* Smoke-tests baked into the build (this example).
* Generating launcher scripts that pin `LD_LIBRARY_PATH`, `LD_PRELOAD`,
  or interpreter selection for downstream users.
* Packaging steps (tarball assembly, checksum manifests, signing).
* Deployment shims that publish artifacts to a release directory.

Non-zero exit from the post-build script fails the whole ct-cake
invocation, so this works as a build-time gate.
