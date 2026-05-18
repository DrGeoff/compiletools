# project_version Sample

> **DEPRECATED.** This example demonstrates `--project-version` /
> `--project-name` (and the `-cmd` variants), all of which are
> deprecated. The flags inject `-DCT_PROJECT_VERSION="..."` /
> `-DCT_PROJECT_NAME="..."` into CXXFLAGS, which defeats
> object-cache reuse for every TU whose transitive headers
> textually mention the macro name (including in comments — see the
> "Macro Scope Filter" section of `README.ct-cake.rst`). Running
> this example now prints a deprecation warning to stderr.
>
> **Use `examples-end-to-end/appinfo` instead.** That example shows
> the preferred pattern: a stable header plus a
> `--prebuild-script`-generated implementation file. PCH and every
> consumer `.o` stay cached across version bumps; only the
> generated `.cpp`'s `.o` invalidates.

Demonstrates ct-cake's opt-in (deprecated) `--project-version` /
`--project-name` hooks, which inject `-DCT_PROJECT_VERSION="<value>"`
and `-DCT_PROJECT_NAME="<value>"` macros into CPPFLAGS, CFLAGS, and
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

(plus a deprecation warning on stderr).

Without the `--project-name` / `--project-version` flags, the macros
remain undefined and the source falls back to `(unset; ...)` strings.

## Why was this deprecated?

Once a `-D` macro is on the command line, every TU that *textually*
mentions the macro identifier (including in comments and string
literals) gets a per-value cache entry — see the "Macro Scope Filter"
section of `README.ct-cake.rst`. The `--prebuild-script` +
generated-implementation-file pattern shown in
`examples-end-to-end/appinfo` avoids that trap entirely:

* no cmdline `-D`, so the scope filter has nothing to match against;
* the version value lives in a generated `.cpp`, not in the include
  graph, so it cannot invalidate any consumer's `dep_hash`;
* the stable `appinfo.hpp` is PCH-safe across version bumps.
