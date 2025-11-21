#ifndef USES_CONDITIONAL_HPP
#define USES_CONDITIONAL_HPP

// Include the header that cleans up TEMP_BUFFER_SIZE
#include "cleans_up.hpp"

// BUG: With the preprocessing_cache bug, TEMP_BUFFER_SIZE is still defined here
// even though cleans_up.hpp did #undef TEMP_BUFFER_SIZE
//
// This means:
// - The #ifndef will incorrectly evaluate to FALSE
// - should_not_see_macro.hpp will NOT be included
// - PKG-CONFIG=leaked-macro-pkg will NOT be extracted
// - The build will be WRONG

#ifndef TEMP_BUFFER_SIZE
    // This should be included because cleans_up.hpp undefined the macro
    #include "should_be_included.hpp"
#endif

inline void process_data() {
    // This function expects alternative_implementation() to be available
    alternative_implementation();
}

#endif // USES_CONDITIONAL_HPP
