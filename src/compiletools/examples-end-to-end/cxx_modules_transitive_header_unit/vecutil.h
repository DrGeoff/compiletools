#pragma once
// Library-style wrapper header. It owns the header-unit import
// (`import <vector>;`), so a consumer gains vector-backed facilities
// purely by `#include`-ing this header -- the consumer never writes
// `import` itself. Its ONLY module interaction is this header unit,
// and it arrives transitively (through the #include), not directly.
import <vector>;

int sum_first_n(int n);
int product_first_n(int n);
