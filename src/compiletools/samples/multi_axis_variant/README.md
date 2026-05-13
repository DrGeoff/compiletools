# multi_axis_variant Sample

Demonstrates ct-cake's variant-axis composition. A variant is built
on-the-fly from one conf file per orthogonal concern:

* **toolchain** — `gcc`, `clang`, `icc`, `msvc`
* **language standard** — `cxx20`, `cxx23`, …
* **linker** — `ld`, `gold`, `mold`, `wild`
* **optimization** — `debug`, `release`, `releasewithdebinfo`
* **instrumentation** — `asan`, `ubsan`, `tsan`, `msan`, `coverage`,
  `lto`, `pgo-gen`, `pgo-use`
* **opinionated bundles** — `dev`, `ci`, `production`, `safety`,
  `perf`, `secure`

`--variant=gcc,debug,asan` is split, canonicalized to the dotted form
`gcc.debug.asan`, and resolved against `gcc.conf` + `debug.conf` +
`asan.conf` from the conf hierarchy. There is **no** per-combination
conf file required — the composition synthesizes them on demand.

## Run

```bash
./build.sh
```

Each invocation produces a separate `bin/<canonical-variant>/` and
shares CAS entries across builds whose toolchain/flags actually agree.

`axis_probe.cpp` prints the toolchain identity, the optimization mode
(via `NDEBUG`), the AddressSanitizer fingerprint, and the C++ language
standard, so the user can see which axes ended up baked in.

## `--variant=blank`

The `blank.conf` axis is intentionally empty. `--variant=blank` lets
the environment (`CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `LDFLAGS`) drive
the build with zero ct-supplied defaults:

```bash
CXX=g++ CXXFLAGS='-O2 -std=c++17' ct-cake --auto --variant=blank axis_probe.cpp
```
