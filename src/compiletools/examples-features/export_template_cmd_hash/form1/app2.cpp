// ct-exemarker
// export-template form1 consumer (per-declaration export variant)
import myproj.util.rounding;

#include <cstdio>

int main() {
    unsigned up = myproj::roundUp<unsigned>(15u, 6u);
    unsigned down = myproj::roundDown<unsigned>(15u, 6u);
    std::printf("app2 up=%u down=%u\n", up, down);
    return 0;
}
