# PCH-bypass bug fixture

Fixture for `test_pch_bypass_bug.py`.

## The bug

When the precompiled-header source lives next to its consumer (the
realistic case for a private per-TU PCH — a header sitting in the same
directory as the `.cpp` that includes it), ct-cake builds the
`.gch` into `<cas-pchdir>/<hash>/` as designed but the consumer compile
never reads it.

Root cause: `build_backend.py` (around the `pch_include_flags` site)
emits only `-I <cas-pchdir>/<hash>`. GCC's `#include "header.h"`
resolution searches the source-file directory FIRST, finds the source
copy of the header, and then looks for a `.gch` only beside that
resolved path — never reaching the cache dir. The cached `.gch` is
dead bytes on disk and the TU compiles from source.

## File layout

```
pch_bypass_bug/
├── pch.h         heavy-ish stdlib bundle (the PCH source)
└── consumer.cpp  has `// ct-exemarker` and `//#PCH=pch.h`,
                  quoted #include "pch.h"
```

The two files are deliberately co-located in the same directory — that
is the structural condition that triggers the bug.

## Detection technique

The matrix in `test_examples_end_to_end_cross_backend.py` cannot catch
this: a from-source compile is a correct fallback, so the build still
succeeds (`returncode == 0`).

`test_pch_bypass_bug.py` appends `-H` to `CXXFLAGS`. GCC's `-H` prints
`! <path>` for every PCH it loads and `x <path>` for every PCH it
rejects. When the bug is present the consumer compile produces zero
`! …pch.h.gch` lines despite the cache being populated. When the bug
is fixed at least one such line must reference the cached `.gch` under
`<cas-pchdir>`.

## Fix sketch

Three fix options exist:

1. Stage a copy/symlink of the PCH header into `<cas-pchdir>/<hash>`
   alongside the `.gch`, then switch the emitted flag from `-I` to
   `-iquote` (or `-isystem` for angle-bracket consumers) so the cache
   resolution wins over the source-dir copy.
2. Emit `-include <cas-pchdir>/<hash>/<header>` explicitly.
3. Detect at rule-emit time that the header coexists with the consumer
   source dir and either symlink the `.gch` into the source dir or
   emit a build-time warning.

Option 2 is what landed in `build_backend.py`. The test in this
directory pins down the observable contract — "the cached `.gch` must
actually be read by the consumer compile" — without prescribing which
fix lands.
