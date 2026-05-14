// Consumer TU that declares pch.h as its precompiled header.
//
//   `// ct-exemarker` — ct-cake builds this TU into an executable.
//   `//#PCH=pch.h`    — ct-cake compiles pch.h into a .gch under
//                       <cas-pchdir>/<hash>/pch.h.gch and adds
//                       `-I <cas-pchdir>/<hash>` to this TU's compile
//                       command.
//
// The include is QUOTED, so GCC's resolution order is:
//   1. directory of consumer.cpp                 ← contains pch.h, MATCH
//   2. -iquote dirs                              (none)
//   3. -I dirs                                   (cas-pchdir is here, never reached)
//   4. -isystem dirs                             (skipped)
//
// GCC stops at step 1, then looks for `<src_dir>/pch.h.gch` next to
// the resolved header — which doesn't exist. The cas-pchdir copy of
// the .gch is never opened. The TU compiles fully from source despite
// the cache being populated.

// ct-exemarker
//#PCH=pch.h
#include "pch.h"

int main() {
    std::vector<int> v{1, 2, 3};
    return static_cast<int>(v.size());
}
