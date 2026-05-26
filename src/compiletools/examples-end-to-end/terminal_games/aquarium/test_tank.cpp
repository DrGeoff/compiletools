// test_tank.cpp -- headless unit test for the pure ASCII-aquarium simulation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf).
//
// <cstdint> and <vector> are included explicitly and *before* the import: this
// TU names std::uint64_t directly and manipulates the Tank's vectors, and the
// module keeps those headers in its global-module-fragment (not re-exported to
// importers). Headers-before-import also avoids the gcc -fmodules-ts
// global-module clash (see aquarium.cpp).
#include <cstdint>
#include <vector>

#include "unit_test.hpp"

import aquarium.tank;

int main() {
    using namespace aqua;

    // Determinism: same seed -> identical Tank after many steps.
    {
        Tank a = initial(40, 20, 123);
        Tank b = initial(40, 20, 123);
        for (int i = 0; i < 200; ++i) { a = step(a); b = step(b); }
        UT_REQUIRE(a.tick == b.tick);
        UT_REQUIRE(a.fish == b.fish);
        UT_REQUIRE(a.bubbles == b.bubbles);
        UT_REQUIRE(a.weed == b.weed);
    }

    // Conservation: fish and bubble counts are constant across steps.
    {
        Tank t = initial(40, 20, 7);
        const auto nf = t.fish.size();
        const auto nb = t.bubbles.size();
        UT_REQUIRE(nf > 0);
        UT_REQUIRE(nb > 0);
        for (int i = 0; i < 500; ++i) t = step(t);
        UT_REQUIRE(t.fish.size() == nf);
        UT_REQUIRE(t.bubbles.size() == nb);
    }

    // A right-mover that leaves the right edge re-enters from the left.
    {
        Tank t = initial(40, 20, 1);
        t.fish.clear();
        Fish f{};
        f.x = t.width - 1; f.y = 5; f.dir = +1; f.species = 0; f.speed = SPEED_SCALE; f.accum = 0;
        t.fish.push_back(f);
        t = step(t);
        UT_REQUIRE(t.fish.size() == 1);
        UT_REQUIRE(t.fish[0].dir == +1);
        UT_REQUIRE(t.fish[0].x == 0);
    }

    // A left-mover that leaves the left edge re-enters from the right.
    {
        Tank t = initial(40, 20, 1);
        t.fish.clear();
        Fish f{};
        f.x = 0; f.y = 5; f.dir = -1; f.species = 0; f.speed = SPEED_SCALE; f.accum = 0;
        t.fish.push_back(f);
        t = step(t);
        UT_REQUIRE(t.fish[0].dir == -1);
        UT_REQUIRE(t.fish[0].x == t.width - 1);
    }

    // Bubbles rise (y decreases) and respawn in the water once they reach the surface.
    {
        Tank t = initial(40, 20, 5);
        t.bubbles.clear();
        Bubble b{};
        b.x = 3; b.y = 10; b.speed = SPEED_SCALE; b.accum = 0;
        t.bubbles.push_back(b);
        t = step(t);
        UT_REQUIRE(t.bubbles[0].y == 9);                 // rose by one cell

        t.bubbles[0].y = SURFACE_ROW + 1;
        t.bubbles[0].speed = SPEED_SCALE;
        t.bubbles[0].accum = 0;
        t = step(t);                                     // crosses the surface
        UT_REQUIRE(t.bubbles[0].y >= water_top(t));      // respawned in the water
        UT_REQUIRE(t.bubbles[0].y <= water_bottom(t));
    }

    // Everything stays in bounds and species are valid across many steps.
    {
        Tank t = initial(60, 24, 99);
        for (const Fish& f : t.fish)
            UT_REQUIRE(f.species >= 0 && f.species < SPECIES_COUNT);
        for (int i = 0; i < 300; ++i) {
            t = step(t);
            for (const Fish& f : t.fish) {
                UT_REQUIRE(f.x >= 0 && f.x < t.width);
                UT_REQUIRE(f.y > SURFACE_ROW && f.y < floor_row(t));
                UT_REQUIRE(f.species >= 0 && f.species < SPECIES_COUNT);
            }
            for (const Bubble& b : t.bubbles) {
                UT_REQUIRE(b.x >= 0 && b.x < t.width);
                UT_REQUIRE(b.y > SURFACE_ROW && b.y < floor_row(t));
            }
        }
    }

    // Seaweed sway is bounded in {-1,0,+1} and periodic (period 24) in the tick.
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
