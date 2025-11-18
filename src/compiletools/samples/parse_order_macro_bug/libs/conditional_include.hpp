#ifndef CONDITIONAL_INCLUDE_HPP
#define CONDITIONAL_INCLUDE_HPP

// This file conditionally includes hash_utility.hpp based on HASH_MAP_NAME
// If HASH_MAP_NAME is not defined when this is parsed, hash_utility.hpp won't be in deps

#ifdef HASH_MAP_NAME
#include "hash_utility.hpp"
#endif

#endif
