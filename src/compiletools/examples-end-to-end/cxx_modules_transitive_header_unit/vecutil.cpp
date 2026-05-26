// Implied source for vecutil.h (ct-cake discovers it because it is a
// sibling .cpp of the #include'd vecutil.h). This TU imports the
// `<vector>` header unit DIRECTLY, which is what gives a gcc build a
// module-mapper entry for the header unit. The interesting consumer is
// main.cpp, whose only header-unit interaction is transitive.
import <vector>;
#include "vecutil.h"

int sum_first_n(int n) {
    std::vector<int> v;
    for (int i = 1; i <= n; ++i) v.push_back(i);
    int s = 0;
    for (int x : v) s += x;
    return s;
}

int product_first_n(int n) {
    std::vector<int> v;
    for (int i = 1; i <= n; ++i) v.push_back(i);
    int p = 1;
    for (int x : v) p *= x;
    return p;
}
