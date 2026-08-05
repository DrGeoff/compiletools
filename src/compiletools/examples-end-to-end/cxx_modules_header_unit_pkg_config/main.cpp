// ct-exemarker
// Regression guard: a header unit reached only via a PKG-CONFIG-resolved
// include path must precompile correctly. Design pothole: the TU-consumer
// compile path expands PKG-CONFIG via magicflags._handle_pkg_config and
// gets the -isystem, but the header-unit precompile pre-pass builds its
// flag list separately from state.flags.cxx — any -isystem source folded
// into one path but not the other (here: per-source //#PKG-CONFIG=extlib
// magic flags) makes gcc error with
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
