// Bare testmarker header. Including this in a TU is the convention
// ct-cake's auto-discovery uses to classify the TU as a *test*
// (instead of an *executable*). The default ct.conf sets
//
//     testmarkers = unit_test.hpp
//
// so any file that pulls this header in transitively is added to the
// test target list, built into bin/<variant>/, and executed as part of
// `ct-cake --auto`'s runtests phase.
//
// The header itself can be empty — only its filename matters for
// detection. A real project would supply a tiny assertion macro here
// or pull in a real framework (gtest/doctest/Catch2); kept minimal
// so the sample stays self-contained.
#pragma once

#include <cstdio>
#include <cstdlib>

#define UT_REQUIRE(cond)                                              \
    do {                                                              \
        if (!(cond)) {                                                \
            std::fprintf(stderr, "UT_REQUIRE failed: %s at %s:%d\n",  \
                         #cond, __FILE__, __LINE__);                  \
            std::exit(1);                                             \
        }                                                             \
    } while (0)
