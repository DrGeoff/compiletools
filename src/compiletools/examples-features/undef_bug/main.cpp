// Fixture root for the #undef macro-state cache invariant. The bug it pins is
// FIXED; this file is the oracle that keeps it fixed. See README.md for the
// mechanism and for which tests drive this sample.
//
// Correct dependency chain:
// main.cpp
//   -> uses_conditional.hpp
//        -> cleans_up.hpp
//             -> defines_macro.hpp (defines TEMP_BUFFER_SIZE)
//             -> #undef TEMP_BUFFER_SIZE
//        -> should_be_included.hpp (via #ifndef TEMP_BUFFER_SIZE)
//

// g++ is the independent oracle for that chain:
// <execute> g++ -MM main.cpp </execute>
/* <output>
main.o: main.cpp uses_conditional.hpp cleans_up.hpp defines_macro.hpp \
 should_be_included.hpp
</output>
*/

// The regression shape, should the invariant break again:
// - after processing cleans_up.hpp, TEMP_BUFFER_SIZE is still in macro state
// - #ifndef TEMP_BUFFER_SIZE therefore evaluates FALSE
// - should_be_included.hpp is not included
// - PKG-CONFIG=leaked-macro-pkg is not extracted
//
// Correct: 4 headers (uses_conditional, cleans_up, defines_macro, should_be_included)
// Regressed: 3 headers (missing should_be_included)

#include "uses_conditional.hpp"

int main() {
    process_data();
    return 0;
}
