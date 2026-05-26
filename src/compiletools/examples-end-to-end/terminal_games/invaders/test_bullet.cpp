// test_bullet.cpp -- headless unit test for invaders.bullet in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). It imports
// invaders.bullet AND invaders.formation explicitly (bullet does not re-export
// formation), so ct-cake discovers just the bullet+formation sub-graph and can
// build + run this test in parallel while the game still links.
#include "unit_test.hpp"

import invaders.bullet;
import invaders.formation;

int main() {
    using namespace invaders;

    // A bullet in flight rises by 1 per advance_bullet call.
    {
        Formation f = make_formation();
        Bullet b{5, 10};
        advance_bullet(b, f);
        UT_REQUIRE(b.y == 9);
    }

    // A bullet reaching y < 0 is cleared to NO_BULLET.
    {
        Formation f = make_formation();
        Bullet b{5, 0};  // one advance will take it to y=-1
        advance_bullet(b, f);
        UT_REQUIRE(b.y == NO_BULLET);
        UT_REQUIRE(b.x == NO_BULLET);
    }

    // A bullet aimed at a living invader's screen cell kills it and clears the bullet.
    {
        Formation f = make_formation();
        const int before = remaining(f);
        // Place the bullet one row below invader (row 0, col 1) so one advance lands on it.
        const int ix = inv_screen_x(f, 1);
        const int iy = inv_screen_y(f, 0);
        Bullet b{ix, iy + 1};
        advance_bullet(b, f);
        UT_REQUIRE(remaining(f) == before - 1);
        UT_REQUIRE(b.y == NO_BULLET);
        UT_REQUIRE(b.x == NO_BULLET);
    }

    // advance_bullet is a no-op when no bullet is in flight.
    {
        Formation f = make_formation();
        Bullet b{NO_BULLET, NO_BULLET};
        advance_bullet(b, f);
        UT_REQUIRE(b.y == NO_BULLET);
        UT_REQUIRE(remaining(f) == INV_ROWS * INV_COLS);
    }

    return 0;
}
