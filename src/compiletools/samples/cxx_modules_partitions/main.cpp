// ct-exemarker
// Partition variant: math is split across math.cppm + math-basic.cppm +
// math-advanced.cppm. The primary interface unit re-exports the two
// interface partitions so a single `import math;` is enough here.
import math;

#include <cstdio>

int main() {
    std::printf("add(2,3)=%d mul(2,3)=%d\n", add(2, 3), mul(2, 3));
    return 0;
}
