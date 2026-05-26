// test_rng.cpp -- headless unit test for snake.rng in isolation.
// Classified as a test via testmarkers=unit_test.hpp. Imports
// only snake.rng, so ct-cake discovers just that sub-graph -- proof the RNG
// leaf stands on its own (no World, no game) and lets ct-cake build + run this
// tiny test in parallel while the game still links.
#include <cstdint>

#include "unit_test.hpp"

import snake.rng;

int main() {
    using namespace snake;
    // Determinism: identical seeds yield identical sequences.
    std::uint64_t a = 12345, b = 12345;
    for (int i = 0; i < 100; ++i) UT_REQUIRE(next_rand(a) == next_rand(b));
    // The generator advances (two successive draws differ for a normal seed).
    std::uint64_t s = 1;
    const std::uint64_t r0 = next_rand(s);
    const std::uint64_t r1 = next_rand(s);
    UT_REQUIRE(r0 != r1);
    return 0;
}
