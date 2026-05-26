// ct-exemarker
// Transitive header unit. main.cpp's ONLY module interaction is the
// `import <vector>;` header unit reached through vecutil.h's #include --
// main.cpp itself never writes `import`. After preprocessing the import
// is part of this TU, so the consumer compile must still resolve the
// header unit (gcc: -fmodules-ts + mapper entry; clang: -fmodules +
// -fmodule-file=<vector>=<bmi>). Gating purely on a TU's OWN
// header-unit imports leaves this consumer without those flags.
#include "vecutil.h"
#include <cstdio>

int main() {
    std::printf("sum=%d product=%d\n", sum_first_n(5), product_first_n(5));
    return 0;
}
