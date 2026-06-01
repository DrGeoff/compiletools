// ct-exemarker
// export-template form1 consumer (per-declaration export variant)
import myproj.util.rounding;

#include <cstdio>

int main() {
    long up = myproj::roundUp<long>(123, 8);
    long down = myproj::roundDown<long>(123, 8);
    std::printf("app1 up=%ld down=%ld\n", up, down);
    return 0;
}
