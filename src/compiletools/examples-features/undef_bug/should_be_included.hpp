#ifndef SHOULD_NOT_SEE_MACRO_HPP
#define SHOULD_NOT_SEE_MACRO_HPP

// This header should ONLY be included if TEMP_BUFFER_SIZE is NOT defined
// It represents functionality that should be excluded when the macro exists

//#PKG-CONFIG=leaked-macro-pkg

inline void alternative_implementation() {
    // This is the alternative code path that should be used
    // when TEMP_BUFFER_SIZE has been cleaned up
}

#endif // SHOULD_NOT_SEE_MACRO_HPP
