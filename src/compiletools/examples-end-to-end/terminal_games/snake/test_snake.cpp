// test_snake.cpp -- headless unit test for the pure Snake simulation.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf).
//
// <cstdint> is included explicitly (and before the import) because this TU uses
// std::uint64_t directly: the module keeps <cstdint> in its global-module-
// fragment, so the name is not re-exported to importers. Headers before import
// also avoids the gcc -fmodules-ts global-module clash (see snake.cpp).
#include <cstdint>

#include "unit_test.hpp"

import snake.world;

int main() {
    using namespace snake;

    // Moving right advances the head's x by one and keeps the length.
    {
        World w = initial(20, 10, 1);
        const int len0 = static_cast<int>(w.body.size());
        const Cell head0 = w.body.front();
        w = step(w, Direction::Right);
        UT_REQUIRE(w.body.front().x == head0.x + 1);
        UT_REQUIRE(w.body.front().y == head0.y);
        UT_REQUIRE(static_cast<int>(w.body.size()) == len0);
        UT_REQUIRE(classify(w) == Verdict::Playing);
    }

    // A 180-degree reversal key is ignored (snake keeps moving right).
    {
        UT_REQUIRE(turn(Direction::Right, 'a') == Direction::Right);
        UT_REQUIRE(turn(Direction::Right, 'w') == Direction::Up);
        UT_REQUIRE(turn(Direction::Up, 's') == Direction::Up);
    }

    // Eating food grows the snake by one and moves food off the body.
    {
        World w = initial(20, 10, 7);
        // Put food directly in front of the head so the next step eats it.
        w.food = step_head(w.body.front(), w.dir);
        const int len0 = static_cast<int>(w.body.size());
        w = step(w, w.dir);
        UT_REQUIRE(static_cast<int>(w.body.size()) == len0 + 1);
        UT_REQUIRE(score(w) == len0 + 1 - START_LENGTH);
        for (const Cell& b : w.body) UT_REQUIRE(!(b == w.food));
    }

    // Driving into the wall kills the snake.
    {
        World w = initial(5, 5, 3);
        for (int i = 0; i < 5; ++i) w = step(w, Direction::Right);
        UT_REQUIRE(classify(w) == Verdict::Dead);
    }

    // Driving back into the body kills the snake. A length-3 snake is too short
    // to self-collide on a U-turn (its tail vacates the cell first -- just like
    // real Snake), so first grow it by eating three times, then loop the longer
    // body back onto itself.
    {
        World w = initial(20, 10, 9);
        for (int i = 0; i < 3; ++i) {
            w.food = step_head(w.body.front(), w.dir);  // food dead ahead
            w = step(w, w.dir);                          // eat -> grow by one
        }
        // Body is now 6 long heading right; a square U-turn re-enters it.
        w = step(w, Direction::Up);
        w = step(w, Direction::Left);
        w = step(w, Direction::Down);  // head returns onto an occupied cell
        UT_REQUIRE(classify(w) == Verdict::Dead);
    }

    // Same seed yields the same food sequence (determinism).
    {
        World a = initial(20, 10, 42);
        World b = initial(20, 10, 42);
        UT_REQUIRE(a.food == b.food);
        std::uint64_t sa = a.seed, sb = b.seed;
        UT_REQUIRE(next_rand(sa) == next_rand(sb));
    }

    return 0;
}
