// Main file that demonstrates the #undef bug
//
// Expected dependency chain:
// main.cpp
//   -> uses_conditional.hpp
//        -> cleans_up.hpp
//             -> defines_macro.hpp (defines TEMP_BUFFER_SIZE)
//             -> #undef TEMP_BUFFER_SIZE
//        -> should_be_included.hpp (via #ifndef TEMP_BUFFER_SIZE)
//

// g++ shows the files that should be found
// <execute> g++ -MM main.cpp </execute>
/* <output>
main.o: main.cpp uses_conditional.hpp cleans_up.hpp defines_macro.hpp \
 should_be_included.hpp
</output>
*/

// BUG: With preprocessing_cache bug:
// - After processing cleans_up.hpp, TEMP_BUFFER_SIZE is still in macro state
// - #ifndef TEMP_BUFFER_SIZE evaluates to FALSE
// - should_not_see_macro.hpp is NOT included
// - PKG-CONFIG=leaked-macro-pkg is NOT extracted
//
// Expected: 3 headers (uses_conditional, cleans_up, defines_macro, should_not_see_macro)
// Buggy: 3 headers (missing should_not_see_macro)

#include "uses_conditional.hpp"

int main() {
    process_data();
    return 0;
}
