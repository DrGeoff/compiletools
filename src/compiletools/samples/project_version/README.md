# project_version Sample

Demonstrates ct-cake's opt-in `--project-version` / `--project-name`
hooks, which inject `-DCT_PROJECT_VERSION="<value>"` and
`-DCT_PROJECT_NAME="<value>"` macros into CPPFLAGS, CFLAGS, and
CXXFLAGS for the duration of a build.

## Run

```bash
./build.sh
```

Expected output:

```
name=demo_app
version=1.2.3
```

Without the `--project-name` / `--project-version` flags, the macros
remain undefined and the source falls back to `(unset; ...)` strings.

## Why opt-in?

Once a `-D` macro is on the command line, every TU that *textually*
mentions the macro identifier (including in comments and string
literals) gets a per-value cache entry — see the "Macro Scope Filter"
section of `README.ct-cake.rst`. Auto-injecting these macros for every
build would defeat object-cache reuse for any TU that names them in
documentation. The opt-in design keeps the cache clean by default.
