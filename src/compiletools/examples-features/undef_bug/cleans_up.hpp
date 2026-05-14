#ifndef CLEANS_UP_HPP
#define CLEANS_UP_HPP

// Include the header that defines TEMP_BUFFER_SIZE
#include "defines_macro.hpp"

// Use the macro for our purposes
inline int calculate_chunk_size() {
    return TEMP_BUFFER_SIZE / 2;
}

// Clean up the namespace - undefine the temporary macro
// This is good C++ hygiene to avoid polluting the global namespace
#undef TEMP_BUFFER_SIZE

#endif // CLEANS_UP_HPP
