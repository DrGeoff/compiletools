#ifndef USES_CONDITIONAL_HPP
#define USES_CONDITIONAL_HPP

// cleans_up.hpp #undef's TEMP_BUFFER_SIZE, so the guard below must open.
#include "cleans_up.hpp"

// This #ifndef is the whole fixture: it is the only place a resurrected
// TEMP_BUFFER_SIZE becomes observable. A macro-state cache that carries the
// macro past the #undef closes the guard, drops should_be_included.hpp from
// the dependency graph, and silently loses its PKG-CONFIG magic flag.
#ifndef TEMP_BUFFER_SIZE
    #include "should_be_included.hpp"
#endif

// Compiling this fixture is a second, independent check: alternative_impl-
// ementation() only exists if the guard opened, so a real compiler rejects
// the wrong answer outright.
inline void process_data() {
    alternative_implementation();
}

#endif // USES_CONDITIONAL_HPP
