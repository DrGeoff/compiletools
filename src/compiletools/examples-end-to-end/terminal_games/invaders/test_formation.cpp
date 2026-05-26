// test_formation.cpp -- headless unit test for invaders.formation in isolation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). It imports
// only invaders.formation, so ct-cake discovers just that sub-graph -- proof the
// formation leaf stands on its own (no Bullet, no Field) and lets ct-cake build +
// run this tiny test in parallel while the field module still links.
#include "unit_test.hpp"

import invaders.formation;

int main() {
    using namespace invaders;

    // A fresh formation has all invaders alive.
    {
        Formation f = make_formation();
        UT_REQUIRE(remaining(f) == INV_ROWS * INV_COLS);
    }

    // march advances offset_x by march_dir in the middle of the field.
    {
        Formation f = make_formation();
        const int x0 = f.offset_x;
        // The initial march_dir is +1, so offset_x should have moved +1.
        march(f, 100);   // very wide: no edge hit
        UT_REQUIRE(f.offset_x == x0 + 1);
    }

    // march reverses march_dir AND increments offset_y at the right edge.
    {
        Formation f = make_formation();
        // Force the formation right against the wall: rightmost living invader is at
        // offset_x + (INV_COLS-1)*2 = offset_x + 10. With width=12, offset_x=1 gives
        // right=11; march_dir=+1 would push it to 12 >= width, so it must reverse.
        f.offset_x = 1;
        f.march_dir = +1;
        const int y0 = f.offset_y;
        march(f, 12);
        UT_REQUIRE(f.march_dir == -1);
        UT_REQUIRE(f.offset_y == y0 + 1);
    }

    // try_hit at a living invader's screen cell returns true and drops remaining by 1.
    {
        Formation f = make_formation();
        const int before = remaining(f);
        const int x = inv_screen_x(f, 2);
        const int y = inv_screen_y(f, 1);
        UT_REQUIRE(try_hit(f, x, y) == true);
        UT_REQUIRE(remaining(f) == before - 1);
    }

    // try_hit at an already-dead or off-grid cell returns false.
    {
        Formation f = make_formation();
        const int x = inv_screen_x(f, 0);
        const int y = inv_screen_y(f, 0);
        try_hit(f, x, y);                          // kill it
        UT_REQUIRE(try_hit(f, x, y) == false);     // already dead
        UT_REQUIRE(try_hit(f, 999, 999) == false); // off-grid miss
    }

    // An emptied formation marches nowhere (guards the empty-formation sentinel).
    {
        Formation z = make_formation();
        for (int r = 0; r < INV_ROWS; ++r)
            for (int c = 0; c < INV_COLS; ++c)
                try_hit(z, inv_screen_x(z, c), inv_screen_y(z, r));
        UT_REQUIRE(remaining(z) == 0);
        const Formation before = z;
        march(z, 30);
        UT_REQUIRE(z == before);
    }

    return 0;
}
