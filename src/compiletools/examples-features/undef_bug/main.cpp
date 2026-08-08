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

// BUG (fixed; kept as the regression fixture): with the pre-fix
// preprocessing_cache:
// - After processing cleans_up.hpp, TEMP_BUFFER_SIZE was still in macro state
// - #ifndef TEMP_BUFFER_SIZE evaluated to FALSE
// - should_be_included.hpp was NOT included
// - PKG-CONFIG=leaked-macro-pkg was NOT extracted
//
// Expected: 4 headers (uses_conditional, cleans_up, defines_macro, should_be_included)
// Buggy: 3 headers (missing should_be_included)

#include "uses_conditional.hpp"

int main() {
    process_data();
    return 0;
}
