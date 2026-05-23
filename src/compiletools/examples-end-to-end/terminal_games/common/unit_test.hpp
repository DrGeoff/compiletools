// unit_test.hpp — minimal testmarker header.
//
// The *filename* is what matters: ct.conf ships `testmarkers = unit_test.hpp`,
// so any TU that includes this header is classified as a test by ct-cake's
// auto-discovery (built, run, and required to exit 0). The body is just a tiny
// assertion macro — a real project would drop in gtest/doctest/Catch2 here.
#pragma once

#include <cstdio>
#include <cstdlib>

#define UT_REQUIRE(cond)                                                   \
    do {                                                                   \
        if (!(cond)) {                                                     \
            std::fprintf(stderr, "UT_REQUIRE failed: %s at %s:%d\n",       \
                         #cond, __FILE__, __LINE__);                       \
            std::exit(1);                                                  \
        }                                                                  \
    } while (0)
