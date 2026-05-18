# prebuild_script Sample

Demonstrates ct-cake's `--prebuild-script` hook, which runs a
user-supplied script before the build graph is constructed. The script
emits `build/version.h` containing a `DEMO_PREBUILD_VERSION` macro;
`version_banner.cpp` `#include`s that header and prints the value.

## Run

```bash
./build.sh
```

Expected output (with a tagged git checkout):

```
version=v1.2.3
```

Or, in a non-git copy of the source tree:

```
version=0.0.0-no-git
```

## Why a generated header instead of `-DDEMO_PREBUILD_VERSION="..."`?

Once a `-D` macro is on the command line, every TU that *textually*
mentions the macro identifier (including in comments and string
literals in unrelated headers) gets a per-value cache entry — see the
"Macro Scope Filter" section of `README.ct-cake.rst`. Generating the
value into a header that exactly one TU includes keeps the cmdline
`-D` set clean: unrelated TUs that happen to mention
`DEMO_PREBUILD_VERSION` in prose retain full object-cache reuse.

This is the generalisation of the narrower `--project-version` /
`--project-name` convenience hooks (see the `project_version/`
example): use `--prebuild-script` for any code-gen step the build
needs to consume.

## Symmetric option

`--postbuild-script` runs after a successful build (before executables
are copied to `bin/`). Use it to emit launcher scripts, packaging
manifests, checksum files, etc. Non-zero exit fails the invocation.
