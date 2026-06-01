// ct-exemarker
// export-template form2 consumer (whole-namespace export variant)
import myproj.util.rounding;

#include <cstdio>

int main() {
    int up = myproj::roundUp<int>(7, 4);
    int down = myproj::roundDown<int>(7, 4);
    std::printf("app0 up=%d down=%d\n", up, down);
    return 0;
}
