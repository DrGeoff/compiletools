// ct-exemarker
// Split-impl variant: math is declared in math.cppm but defined in math_impl.cpp.
import math;

#include <cstdio>

int main() {
    std::printf("add(2,3)=%d\n", add(2, 3));
    return 0;
}
