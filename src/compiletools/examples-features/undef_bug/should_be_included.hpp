#ifndef SHOULD_NOT_SEE_MACRO_HPP
#define SHOULD_NOT_SEE_MACRO_HPP

// The guard name DELIBERATELY does not derive from the filename: include
// guards work by macro define state, exactly as the preprocessor evaluates
// them, and a detector that guessed the guard from the filename would pass
// on a conventional name while being wrong. Do not "fix" it to
// SHOULD_BE_INCLUDED_HPP. It also documents the condition this header
// depends on: it must not see TEMP_BUFFER_SIZE.
//
// This header should ONLY be included if TEMP_BUFFER_SIZE is NOT defined
// It represents functionality that should be excluded when the macro exists

//#PKG-CONFIG=leaked-macro-pkg

inline void alternative_implementation() {
    // This is the alternative code path that should be used
    // when TEMP_BUFFER_SIZE has been cleaned up
}

#endif // SHOULD_NOT_SEE_MACRO_HPP
