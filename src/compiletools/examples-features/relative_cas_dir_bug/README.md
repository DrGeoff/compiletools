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

Pass **absolute** `--cas-*dir` paths, or invoke ct-cake from the gitroot. The
realistic shared-CAS deployment — a team's cache on an NFS/SMB mount, off the
source tree — already uses absolute paths (e.g. `--cas-pchdir=/mnt/ct-cache/pch`)
and is therefore unaffected. `test_relative_cas_dir_bug.py` covers that off-tree
case alongside the relative bug and the under-gitroot workaround.

## Fix sketch (not applied here)

The clean fix is at the single resolution chokepoint: in
`apptools.resolve_cas_directory_arguments`, resolve a *relative* cas dir against
the **gitroot** — `os.path.join(find_git_root(), value)`, reusing the same
`git_root` the resolver already computes and that the build's `anchor_root`
uses. `os.path.join` passes absolute values through unchanged, so absolute and
default cas dirs are untouched; only relative ones are anchored.

Gitroot-anchoring (rather than cwd-anchoring, e.g. `os.path.abspath`) is the
*correct* form: `apptools.canonicalize_path_for_cache_key` is a textual
string-prefix operation, so cross-user byte-identity depends on the cas-dir
string sharing the exact `anchor_root` prefix. Gitroot-anchoring guarantees that
by construction; cwd-anchoring only achieves it when `getcwd()` and
`git rev-parse --show-toplevel` agree textually, which fails under symlinked /
NFS-automounted checkouts. The lone behavioural delta is that a relative cas dir
now means "relative to the gitroot" (matching the default) regardless of the
invocation cwd. The source path must still stay gitroot-relative for
byte-identity; only the cache *location* is being anchored.
