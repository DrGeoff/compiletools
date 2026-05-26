// test_bricks.cpp -- headless unit test for breakout.bricks in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). It imports
// only breakout.bricks, so ct-cake discovers just that sub-graph -- proof the
// bricks leaf stands on its own (no Arena, no game) and lets ct-cake build + run
// this tiny test in parallel while the arena still links.
#include "unit_test.hpp"

import breakout.bricks;

int main() {
    using namespace breakout;

    // A fresh grid has all bricks alive.
    {
        Bricks b = make_bricks(10);
        UT_REQUIRE(bricks_left(b) == BRICK_ROWS * 10);
    }

    // hit_brick kills a living cell and reports the hit; the cell is then dead.
    {
        Bricks b = make_bricks(10);
        UT_REQUIRE(brick_alive(b, 0, 3));
        UT_REQUIRE(hit_brick(b, 3, brick_screen_y(0)) == true);
        UT_REQUIRE(!brick_alive(b, 0, 3));
        UT_REQUIRE(bricks_left(b) == BRICK_ROWS * 10 - 1);
    }

    // Second hit at the same (already dead) cell returns false.
    {
        Bricks b = make_bricks(10);
        hit_brick(b, 3, brick_screen_y(0));
        UT_REQUIRE(hit_brick(b, 3, brick_screen_y(0)) == false);
    }

    // Out-of-range x and wrong screen row both return false.
    {
        Bricks b = make_bricks(10);
        UT_REQUIRE(hit_brick(b, 999, brick_screen_y(0)) == false);
        UT_REQUIRE(hit_brick(b, 3, 0) == false);
    }

    return 0;
}
