// test_arena.cpp -- headless unit test for the pure Breakout simulation.
#include "unit_test.hpp"

import breakout.arena;

int main() {
    using namespace breakout;

    // Side wall reflects vx.
    {
        Arena a = initial(20, 12);
        // Clear bricks so they don't interfere with the wall test.
        for (auto&& b : a.bricks) b = false;  // std::vector<bool> yields a proxy, not bool&
        a.ball_x = a.width - 1; a.ball_y = 6; a.vx = +1; a.vy = -1;
        a = step(a, PaddleDir::None);
        UT_REQUIRE(a.vx == -1);
    }

    // Ceiling reflects vy.
    {
        Arena a = initial(20, 12);
        for (auto&& b : a.bricks) b = false;  // std::vector<bool> yields a proxy, not bool&
        a.ball_x = 10; a.ball_y = 0; a.vx = +1; a.vy = -1;
        a = step(a, PaddleDir::None);
        UT_REQUIRE(a.vy == +1);
    }

    // Paddle reflects vy.
    {
        Arena a = initial(20, 12);
        for (auto&& b : a.bricks) b = false;  // std::vector<bool> yields a proxy, not bool&
        a.bricks[0] = true;  // keep one brick at screen (0,1) so classify() isn't Won;
                             // it is far from the ball's paddle-row path (rows 10/11/9)
        a.paddle_x = 5; a.ball_x = 7; a.ball_y = paddle_row(a) - 1; a.vx = +1; a.vy = +1;
        a = step(a, PaddleDir::None);
        UT_REQUIRE(a.vy == -1);
        UT_REQUIRE(classify(a) == Verdict::Playing);
    }

    // A brick in the ball's path is removed and vy reflects.
    {
        Arena a = initial(20, 12);
        const int before = bricks_left(a);
        // Place ball just below brick row 0 (screen y=1) moving up into it.
        a.ball_x = 4; a.ball_y = brick_screen_y(0) + 1; a.vx = 0; a.vy = -1;
        a = step(a, PaddleDir::None);
        UT_REQUIRE(bricks_left(a) == before - 1);
        UT_REQUIRE(a.vy == +1);
    }

    // Missing the ball (it goes past the paddle row) is a Loss.
    {
        Arena a = initial(20, 12);
        for (auto&& b : a.bricks) b = false;  // std::vector<bool> yields a proxy, not bool&
        a.bricks[0] = true;  // keep one brick so the only way to end is the miss
        a.paddle_x = 0; a.paddle_w = 2;
        a.ball_x = 18; a.ball_y = paddle_row(a) - 1; a.vx = 0; a.vy = +1;
        a = step(a, PaddleDir::None);   // moves onto paddle row, no catch
        a = step(a, PaddleDir::None);   // falls below
        UT_REQUIRE(classify(a) == Verdict::Lost);
    }

    // Clearing the last brick is a Win.
    {
        Arena a = initial(20, 12);
        for (auto&& b : a.bricks) b = false;  // std::vector<bool> yields a proxy, not bool&
        a.bricks[0] = true;             // single brick at (row 0, col 0), screen (0,1)
        a.ball_x = 0; a.ball_y = brick_screen_y(0) + 1; a.vx = 0; a.vy = -1;
        a = step(a, PaddleDir::None);
        UT_REQUIRE(classify(a) == Verdict::Won);
    }

    return 0;
}
