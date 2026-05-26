// test_seaweed.cpp -- headless unit test for aquarium.seaweed in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). Imports
// only aquarium.seaweed; ct-cake still links aquarium.water because
// seaweed_impl.cpp (the implementation unit) imports it.
#include <cstdint>

#include "unit_test.hpp"

import aquarium.seaweed;

int main() {
    using namespace aqua;

    // spawn_weed places a plant in bounds, within the documented height range.
    {
        std::uint64_t seed = 11;
        for (int i = 0; i < 100; ++i) {
            Weed w = spawn_weed(60, 24, seed);
            UT_REQUIRE(w.x >= 0 && w.x < 60);
            UT_REQUIRE(w.height >= MIN_WEED_HEIGHT && w.height <= MAX_WEED_HEIGHT);
        }
    }

    // Sway is bounded in {-1,0,+1} and periodic with period 24.
    {
        for (std::uint64_t tk = 0; tk < 50; ++tk)
            for (int r = 0; r < 10; ++r) {
                const int o = seaweed_offset(tk, r);
                UT_REQUIRE(o >= -1 && o <= 1);
                UT_REQUIRE(seaweed_offset(tk, r) == seaweed_offset(tk + 24, r));
            }
    }

    return 0;
}
