#ifndef CONDITIONAL_HPP
#define CONDITIONAL_HPP

// First include base.hpp to get USE_HASH macro
#include "base.hpp"

// Then conditionally include dependency.hpp based on that macro
// BUG: When headerdeps runs with empty macro state (frozenset()),
// USE_HASH is not defined, so dependency.hpp is NOT included!
#ifdef USE_HASH
#include "dependency.hpp"
#endif

#endif
