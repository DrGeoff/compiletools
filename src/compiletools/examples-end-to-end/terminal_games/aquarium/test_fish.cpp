// test_fish.cpp -- headless unit test for aquarium.fish in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). It imports
// only aquarium.fish (+ aquarium.water for SPEED_SCALE/geometry), so ct-cake
// discovers just that sub-graph -- proof the fish module stands on its own.
#include <cstdint>

#include "unit_test.hpp"

import aquarium.fish;
import aquarium.water;

int main() {
    using namespace aqua;

    // spawn_fish lands a fish in bounds with a valid species and direction.
    {
        std::uint64_t seed = 42;
        for (int i = 0; i < 100; ++i) {
            Fish f = spawn_fish(40, 20, seed);
            UT_REQUIRE(f.x >= 0 && f.x < 40);
            UT_REQUIRE(f.y > SURFACE_ROW && f.y < floor_row(20));
            UT_REQUIRE(f.species >= 0 && f.species < SPECIES_COUNT);
            UT_REQUIRE(f.dir == 1 || f.dir == -1);
        }
    }

    // A right-mover that leaves the right edge re-enters from the left.
    {
        std::uint64_t seed = 1;
        Fish f{};
        f.x = 39; f.y = 5; f.dir = +1; f.species = 0; f.speed = SPEED_SCALE; f.accum = 0;
        advance_fish(f, 40, 20, seed);
        UT_REQUIRE(f.dir == +1);
        UT_REQUIRE(f.x == 0);
    }

    // A left-mover that leaves the left edge re-enters from the right.
    {
        std::uint64_t seed = 1;
        Fish f{};
        f.x = 0; f.y = 5; f.dir = -1; f.species = 0; f.speed = SPEED_SCALE; f.accum = 0;
        advance_fish(f, 40, 20, seed);
        UT_REQUIRE(f.dir == -1);
        UT_REQUIRE(f.x == 39);
    }

    // Determinism: same seed -> identical fish after the same spawn/advance.
    {
        std::uint64_t sa = 7, sb = 7;
        Fish a = spawn_fish(40, 20, sa);
        Fish b = spawn_fish(40, 20, sb);
        for (int i = 0; i < 200; ++i) { advance_fish(a, 40, 20, sa); advance_fish(b, 40, 20, sb); }
        UT_REQUIRE(a == b);
    }

    // Bounds hold across many advances.
    {
        std::uint64_t seed = 99;
        Fish f = spawn_fish(60, 24, seed);
        for (int i = 0; i < 500; ++i) {
            advance_fish(f, 60, 24, seed);
            UT_REQUIRE(f.x >= 0 && f.x < 60);
            UT_REQUIRE(f.y > SURFACE_ROW && f.y < floor_row(24));
            UT_REQUIRE(f.species >= 0 && f.species < SPECIES_COUNT);
        }
    }

    return 0;
}
