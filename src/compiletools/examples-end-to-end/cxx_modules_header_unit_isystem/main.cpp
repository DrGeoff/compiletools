// ct-exemarker
// Regression guard: a header unit reached only via a non-default search path
// (here, -isystem extlib/include) must precompile correctly. Pre-fix, the
// gcc cas-pcmdir mapper-resolution probe ran g++ -M without the user's
// include flags, failed to resolve extlib/Exception.h, left the mapper
// without an entry for the canonical path, and the precompile fell through
// to the global-mapper path -- where gcc then reported the import as an
// "unknown compiled module interface".
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
