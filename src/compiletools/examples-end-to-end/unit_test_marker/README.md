# unit_test_marker Sample

Demonstrates ct-cake's automatic *test* classification via the
`testmarkers` configuration setting.

The default `ct.conf` ships with:

```
testmarkers = unit_test.hpp
```

Any TU that transitively includes a file named `unit_test.hpp` is
classified as a unit test (rather than as an executable). On
`ct-cake --auto`, tests are:

1. Compiled into `bin/<variant>/<test_name>`
2. Executed after the build completes
3. Required to exit `0` — non-zero exit is a build failure

## File map

| File | Classification | Why |
|---|---|---|
| `main.cpp` | exe | Has `main(` (exemarker), no `unit_test.hpp` |
| `test_widget.cpp` | test | Has `main(`, *and* `#include "unit_test.hpp"` |
| `widget.{hpp,cpp}` | impl | Pulled in by both |
| `unit_test.hpp` | testmarker header | Filename matches `testmarkers` |

## Run

```bash
ct-cake --auto
```

Expected: `bin/<variant>/main` and `bin/<variant>/test_widget` are
built, `test_widget` is executed and prints nothing (UT_REQUIRE passes).

To skip the test phase: `ct-cake --auto --disable-tests`.
To skip the exe phase:  `ct-cake --auto --disable-exes`.
