# auto_pkg_config Sample (default-on `//#PKG-CONFIG` inference)

A translation unit with a bare `#include <autoinfer/api.h>` — no magic-flag
annotation anywhere. With `--auto-pkg-config` (default on) and a
`PKG_CONFIG_PATH` that can see the `examples-features/pkgs/*.pc` fixtures,
`pkgconfig_infer` maps the header to the `autoinfer` package (via the
`.pc` include-dir index) and treats the TU exactly as if it carried
`//#PKG-CONFIG=autoinfer`: `-I.../autoinfer-include` in CPPFLAGS/CXXFLAGS,
`-lautoinfer` in LDFLAGS.

`autoinfer/api.h` only *declares* `autoinfer_answer()`; the definition lives
in a real `libautoinfer.a` that the cross-backend matrix test compiles into
each cell's workspace and publishes through a generated `autoinfer.pc`
(`Libs: -L<ws>/lib -lautoinfer`) that shadows the fixture's
`Libs: -lautoinfer`. This is what makes the matrix cell a genuine link test:

* with `--auto-pkg-config` the link succeeds and the binary exits 0
  (42 == 42);
* with `--no-auto-pkg-config` no `-I`/`-l` exists, so the compile fails at
  the `#include` — proving inference is what made the positive cell work.

Inference never invents anything a user could not have written by hand:
explicit `//#PKG-CONFIG`, `-l`, or `-I` flags always win, std/compiler/
project headers are never candidates, and ambiguity resolves to nothing
(plus a diagnostic) rather than a guess.
