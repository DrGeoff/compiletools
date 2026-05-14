# dynamic_library Sample

Mirror of `examples-features/library/` but exercising `ct-cake --dynamic`
instead of `--static`. The library lives in `mylib/`, the consumer
in the top-level `main.cpp`.

## Layout

```
dynamic_library/
├── build.sh              -- two-step build: lib first, then exe
├── main.cpp              -- exe that forward-declares greeting()
└── mylib/
    ├── greeter.cpp       -- the lib
    └── greeter.hpp
```

## Build

```bash
./build.sh
```

The script:

1. `cd mylib && ct-cake --dynamic greeter.cpp` — produces
   `mylib/bin/<variant>/libgreeter.so` (the cas-exedir entry is
   linked into place by the `symlink` rule).
2. `ct-cake --auto` from the top — compiles `main.cpp`, links it
   against `-Lmylib/bin -lgreeter` per the magic flags.
3. Runs the executable with `LD_LIBRARY_PATH` set so the dynamic
   loader can find `libgreeter.so` at startup.

## Why a forward declaration?

`main.cpp` deliberately does *not* `#include "mylib/greeter.hpp"`.
If it did, ct-cake's header spider would pull `greeter.cpp` into the
exe's own compile set and statically link `greeting()`, so the
shared library would never be exercised.
