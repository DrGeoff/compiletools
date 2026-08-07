#ifndef SHOULD_BE_INCLUDED_HPP
#define SHOULD_BE_INCLUDED_HPP

// Reachable only when TEMP_BUFFER_SIZE is NOT defined: cleans_up.hpp #undef'd
// it, so uses_conditional.hpp's #ifndef guard must open. The magic flag below
// is the observable -- a header walk that resurrects the #undef'd macro never
// reaches this file, so the flag never lands on the compile line.

//#PKG-CONFIG=leaked-macro-pkg

inline void alternative_implementation() {
}

#endif // SHOULD_BE_INCLUDED_HPP
