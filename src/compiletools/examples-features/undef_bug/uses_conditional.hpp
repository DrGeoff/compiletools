#ifndef USES_CONDITIONAL_HPP
#define USES_CONDITIONAL_HPP

// Include the header that cleans up TEMP_BUFFER_SIZE
#include "cleans_up.hpp"

// BUG (fixed): with the pre-fix preprocessing_cache, TEMP_BUFFER_SIZE was
// still defined here even though cleans_up.hpp did #undef TEMP_BUFFER_SIZE
//
// That meant:
// - The #ifndef incorrectly evaluated to FALSE
// - should_be_included.hpp was NOT included
// - PKG-CONFIG=leaked-macro-pkg was NOT extracted
// - The build was WRONG

#ifndef TEMP_BUFFER_SIZE
    // This should be included because cleans_up.hpp undefined the macro
    #include "should_be_included.hpp"
#endif

inline void process_data() {
    // This function expects alternative_implementation() to be available
    alternative_implementation();
}

#endif // USES_CONDITIONAL_HPP
