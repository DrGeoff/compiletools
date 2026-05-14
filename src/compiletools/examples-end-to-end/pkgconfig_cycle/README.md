# pkgconfig_cycle Sample (intentional build failure)

Demonstrates ct-cake's `LDFLAGSCycleError` when two translation units
assert opposite **hard** link-order constraints via multi-package
`//#PKG-CONFIG` annotations.

| File | Annotation | Hard ordering implied |
|---|---|---|
| `asserts_alpha_before_beta.cpp` | `//#PKG-CONFIG=cycle-alpha cycle-beta` | `-lcycle_alpha` before `-lcycle_beta` |
| `asserts_beta_before_alpha.cpp` | `//#PKG-CONFIG=cycle-beta cycle-alpha` | `-lcycle_beta` before `-lcycle_alpha` |

Hard edges never cancel (only soft single-package or plain `//#LDFLAGS`
constraints do), so the merge step in `utils.merge_ldflags_with_topo_sort`
raises:

```
Cyclic library dependency detected — link order cannot be determined.
  Cycle: -lcycle_alpha -> -lcycle_beta -> -lcycle_alpha
```

The diagnostic also names the contributing source files.

## To reproduce

```bash
PKG_CONFIG_PATH=$(pwd)/../pkgs ct-cake --auto
```

The build is expected to fail at the merge step, before any compile
command runs. To prove the cycle is what fails (rather than something
upstream), inspect just the magic flags:

```bash
PKG_CONFIG_PATH=$(pwd)/../pkgs ct-magicflags asserts_alpha_before_beta.cpp asserts_beta_before_alpha.cpp
```

## Fix

Either drop one TU's multi-package form (keep both packages but list
them in two single-pkg annotations, which makes the edge **soft** and
gets cancelled), or pick one consistent order and use it in both files.
