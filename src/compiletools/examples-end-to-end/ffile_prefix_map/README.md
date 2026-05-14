# ffile_prefix_map Sample

Demonstrates ct-cake's workspace-relative compile paths feature
(see CLAUDE.md → "Workspace-Relative Compile Paths (Round 3)").

By default, ct-cake appends `-ffile-prefix-map=<gitroot>=<target>` to
`CXXFLAGS` and `CFLAGS` (target overridable via
`--ffile-prefix-map-target`, default `.`). The result:

* `__FILE__` expands to a path with the *target* prefix instead of
  the absolute gitroot path
* DWARF debug info, dependency files (`.d`), and `__BASE_FILE__`
  carry the same workspace-independent paths
* The CAS object key hashes the workspace-relative path too, so
  two clones at `/scratch/run-1/repo` and `/scratch/run-2/repo`
  produce byte-identical `.o` files and share cache hits

Linker symmetry is provided by sister functions
`canonicalize_for_command` / `canonicalize_path_for_command`, which
target-prefix RPATHs and `--version-script` paths inside the
gitroot.

## Run

```bash
./build.sh
```

Three demonstrations:

1. Default auto-injection: `__FILE__` is `./path_probe.cpp`
2. Override target: `--ffile-prefix-map-target=/build` →
   `/build/.../path_probe.cpp`
3. User opt-out: any user-supplied `-f{file,debug,macro,canon}-prefix-map`
   suppresses auto-injection on that slot

## Acceptance gates

The Round 3 byte-identity test
(`test_ffile_prefix_map.py`) parametrizes across all six backends ×
four CAS layers; `cas-objdir` and `cas-exedir` pass on every backend,
`cas-pchdir` and `cas-pcmdir` `xfail(strict=False)` because gcc embeds
source paths in PCH/BMI via an internal table that
`-ffile-prefix-map` doesn't reach.
