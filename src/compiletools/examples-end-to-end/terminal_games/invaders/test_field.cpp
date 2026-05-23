// test_field.cpp -- headless unit test for the pure Space Invaders simulation.
#include "unit_test.hpp"

import invaders.field;

int main() {
    using namespace invaders;

    // A bullet aligned with an invader clears exactly that invader.
    {
        Field f = initial(40, 20);
        const int before = remaining(f);
        // Aim at invader (row 2, col 0): place a rising bullet one row below it.
        f.bullet_x = inv_screen_x(f, 0);
        f.bullet_y = inv_screen_y(f, 2) + 1;
        f = step(f, Action::None);  // bullet rises into the invader
        UT_REQUIRE(remaining(f) == before - 1);
        UT_REQUIRE(f.bullet_y == NO_BULLET);
    }

    // Player clamps at both bounds.
    {
        Field f = initial(10, 20);
        f.player_x = 0;
        f = step(f, Action::Left);
        UT_REQUIRE(f.player_x == 0);
        f.player_x = 9;
        f = step(f, Action::Right);
        UT_REQUIRE(f.player_x == 9);
    }

    // Firing arms a bullet only when none is in flight.
    {
        Field f = initial(40, 20);
        f = step(f, Action::Fire);
        UT_REQUIRE(f.bullet_y != NO_BULLET);
        const int bx = f.bullet_x;
        f = step(f, Action::Fire);  // ignored: bullet already flying
        UT_REQUIRE(f.bullet_x == bx);
    }

    // The formation reverses direction and drops a row at the right edge.
    {
        Field f = initial(14, 20);  // narrow so the edge is hit quickly
        const int dir0 = f.march_dir;
        const int y0 = f.offset_y;
        bool dropped = false;
        for (int i = 0; i < 200 && !dropped; ++i) {
            f = step(f, Action::None);
            if (f.offset_y > y0) dropped = true;
        }
        UT_REQUIRE(dropped);
        UT_REQUIRE(f.march_dir == -dir0);
    }

    // Clearing the last invader is a Win.
    {
        Field f = initial(40, 20);
        for (auto&& a : f.alive) a = false;
        f.alive[0] = true;                 // one invader left, at (0,0)
        f.bullet_x = inv_screen_x(f, 0);
        f.bullet_y = inv_screen_y(f, 0) + 1;
        f = step(f, Action::None);
        UT_REQUIRE(classify(f) == Verdict::Won);
    }

    // The formation reaching the bottom row is a Loss.
    {
        Field f = initial(40, 6);
        f.offset_y = 4;                    // bottom invader row at screen y=6 >= height-1
        UT_REQUIRE(classify(f) == Verdict::Lost);
    }

    return 0;
}
