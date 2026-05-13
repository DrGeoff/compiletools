# cli_features Sample

A tour of ct-cake CLI options that don't map cleanly to any single
C++ idiom. The sample contains three trivial executables (`alpha`,
`beta`, `gamma`) whose only purpose is to be cheap rebuild targets.

## Run

```bash
./build.sh
```

The script walks through 11 numbered steps, one per feature:

| Step | Feature | Flags |
|---|---|---|
| 1 | Auto build everything | `--auto` |
| 2 | Rename a single output | `-o` / `--output` |
| 3 | Subset of auto | `--disable-tests`, `--disable-exes` |
| 4 | CI-style narrowed rebuild | `--build-only-changed` |
| 5 | clangd / clang-tidy database | `--compilation-database` |
| 6 | Per-build timing report | `--timing` |
| 7 | Stable per-invocation log dir | `--diagnostics-dir` |
| 8 | Mtime fallback for the touch workflow | `--use-mtime=True` |
| 9 | Custom CAS paths | `--cas-{obj,pch,pcm,exe}dir` |
| 10 | Clean build artefacts | `--clean` |
| 11 | Clean artefacts + this build's CAS slice | `--realclean` |

Each step is independent — feel free to copy individual lines into
your own scripts.
