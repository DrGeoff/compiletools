// bricks_impl.cpp -- the Breakout brick grid: implementation unit (module breakout.bricks).
//
// Defines what bricks.cppm declares. As a `module breakout.bricks;` unit it
// implicitly imports the bricks interface (so Bricks/BRICK_ROWS are in scope).
// ct-cake pulls this file into the link automatically for anything that imports
// breakout.bricks.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <vector>

module breakout.bricks;

namespace breakout {
namespace {

void kill_brick(Bricks& b, int r, int c) { b.grid[r * b.cols + c] = false; }

}  // namespace

Bricks make_bricks(int cols) {
    Bricks b{cols, {}};
    b.grid.assign(BRICK_ROWS * cols, true);
    return b;
}

int bricks_left(const Bricks& b) {
    int n = 0;
    for (bool x : b.grid) n += x ? 1 : 0;
    return n;
}

bool hit_brick(Bricks& b, int x, int y) {
    for (int r = 0; r < BRICK_ROWS; ++r)
        if (brick_screen_y(r) == y && x >= 0 && x < b.cols && brick_alive(b, r, x)) {
            kill_brick(b, r, x);
            return true;
        }
    return false;
}

}  // namespace breakout
