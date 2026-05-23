# relative_cas_dir_bug — a relative `--cas-pchdir` breaks the PCH rule from a subdir

This example pins a **latent build-backend bug**. It is *not* built by the
cross-backend matrix; it is consumed by `test_relative_cas_dir_bug.py`, which
reproduces the failure and its workaround.

## The files

- `heavy.h` — a few standard headers; the precompiled-header payload.
- `widget.cpp` — a trivial program that declares `//#PCH=heavy.h` and
  `#include "heavy.h"`, so ct-cake builds `heavy.h` into a `.gch` in
  `cas-pchdir` and consumes it.

## The bug

For cross-user PCH **byte-identity**, ct-cake's PCH precompile rule
(`build_backend.py`, `_create_pch_rules`) passes the PCH source *relative to the
gitroot* and runs the compiler under `cwd = anchor_root`:

```sh
flock <cas-pchdir>/.../pch.h.gch.lock sh -c \
  'cd <gitroot> && g++ ... -x c++-header <gitroot-relative source> \
       -o <cas-pchdir>/.../heavy.h.gch.compiletools.tmp \
   && mv -f <...>.tmp <...>.gch'
```

The **source** path is correctly made gitroot-relative, but the **output**
paths (`-o`, the `mv` targets) are emitted *relative to the invocation cwd*, not
the gitroot. So when **both** of these hold:

1. `--cas-pchdir` is a **relative** path, and
2. ct-cake is invoked from a **subdirectory** of the gitroot,

`make` creates the cache directory relative to its own cwd (the subdir), while
`cd <gitroot> && g++ -o <relpath>` resolves the same relative path against the
*gitroot* — a different, nonexistent directory. The compile dies with:

```
fatal error: cannot create precompiled header <relpath>/heavy.h.gch.compiletools.tmp:
No such file or directory
```

The C++20 module / BMI precompile rule (`build_backend.py`, around the
`pcm_rule_cwd = anchor_root` assignment) uses the identical anchor-relative
pattern, so a relative `--cas-pcmdir` from a subdir shares the same flaw.

## Not triggered by

- **Default cas dirs** — they resolve to absolute, gitroot-anchored paths.
- **Absolute `--cas-*dir`** — the rule's `-o` is absolute, so the `cd <gitroot>`
  is harmless. *(This is the workaround.)*
- **Invoking from the gitroot itself** — the invocation cwd and the rule cwd
  coincide, so the relative path resolves the same either way.

## Workaround

Pass **absolute** `--cas-*dir` paths, or invoke ct-cake from the gitroot.

## Fix sketch (not applied here)

Absolutize the PCH/PCM rule's `-o` and `mv` output paths (and the `flock`
sidecar path) against the invocation cwd before the rule is wrapped with
`cd <anchor_root> &&`. The source path must stay gitroot-relative for
byte-identity; only the cache-output paths need absolutizing.
