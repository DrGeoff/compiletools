// ct-exemarker
// Regression guard: a header unit reached only via a PKG-CONFIG-resolved
// include path must precompile correctly. Pre-fix, the gcc cas-pcmdir
// header-unit precompile rule honored -isystem flags that came from
// ``append-CXXFLAGS = -isystem ...`` (commit ac300abe) but NOT the
// equivalent -isystem paths derived from per-source ``//#PKG-CONFIG=extlib``
// magic flags. The TU-consumer compile path expanded PKG-CONFIG via
// magicflags._handle_pkg_config and got the -isystem; the header-unit
// precompile pre-pass only walked args.flags.cxx and missed the
// PKG-CONFIG-derived flags, so gcc errored with
// ``cc1plus: fatal error: extlib/Exception.h: No such file or directory``.
//
//#PKG-CONFIG=extlib
import <extlib/Exception.h>;
#include <cstdio>

int main() {
    try {
        throw extlib::Exception("boom");
    } catch (const extlib::Exception& e) {
        std::printf("caught=%s\n", e.what());
    }
    return 0;
}
