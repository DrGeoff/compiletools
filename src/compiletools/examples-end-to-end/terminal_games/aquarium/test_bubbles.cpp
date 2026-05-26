// test_bubbles.cpp -- headless unit test for aquarium.bubbles in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf).
#include <cstdint>

#include "unit_test.hpp"

import aquarium.bubbles;
import aquarium.water;

int main() {
    using namespace aqua;

    // spawn_bubble lands in the open water.
    {
        std::uint64_t seed = 3;
        for (int i = 0; i < 100; ++i) {
            Bubble b = spawn_bubble(40, 20, seed);
            UT_REQUIRE(b.x >= 0 && b.x < 40);
            UT_REQUIRE(b.y >= water_top() && b.y <= water_bottom(20));
        }
    }

    // advance rises one cell at full speed.
    {
        std::uint64_t seed = 5;
        Bubble b{};
        b.x = 3; b.y = 10; b.speed = SPEED_SCALE; b.accum = 0;
        advance_bubble(b, 40, 20, seed);
        UT_REQUIRE(b.y == 9);
    }

    // Reaching the surface respawns the bubble onto the floor-adjacent row.
    {
        std::uint64_t seed = 5;
        Bubble b{};
        b.x = 3; b.y = SURFACE_ROW + 1; b.speed = SPEED_SCALE; b.accum = 0;
        advance_bubble(b, 40, 20, seed);
        UT_REQUIRE(b.y == water_bottom(20));
    }

    // Bounds hold across many advances.
    {
        std::uint64_t seed = 77;
        Bubble b = spawn_bubble(60, 24, seed);
        for (int i = 0; i < 500; ++i) {
            advance_bubble(b, 60, 24, seed);
            UT_REQUIRE(b.x >= 0 && b.x < 60);
            UT_REQUIRE(b.y > SURFACE_ROW && b.y < floor_row(24));
        }
    }

    return 0;
}
