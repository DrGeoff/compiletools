// formation_impl.cpp -- the invader formation: implementation unit (module invaders.formation).
//
// Defines what formation.cppm declares. As a `module invaders.formation;` unit it
// implicitly imports the formation interface (so Formation/INV_ROWS/INV_COLS are in
// scope). ct-cake pulls this file into the link automatically for anything that
// imports invaders.formation.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <limits>
#include <vector>

module invaders.formation;

namespace invaders {
namespace {

void kill_inv(Formation& f, int r, int c) { f.alive[r * INV_COLS + c] = false; }

}  // namespace

Formation make_formation() {
    return Formation{1, 1, +1, std::vector<bool>(INV_ROWS * INV_COLS, true)};
}

int remaining(const Formation& f) {
    int n = 0;
    for (bool a : f.alive) n += a ? 1 : 0;
    return n;
}

int formation_bottom(const Formation& f) {
    int bottom = -1;
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) {
                const int y = inv_screen_y(f, r);
                if (y > bottom) bottom = y;
            }
    return bottom;
}

// Horizontal extents of the living formation. File-local (anonymous namespace):
// only march() below needs them, so they stay internal -- the same
// minimal-public-interface rule that keeps kill_inv above (and breakout's
// kill_brick) out of the module interface.
namespace {

int formation_right(const Formation& f) {
    int right = 0;
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) { const int x = inv_screen_x(f, c); if (x > right) right = x; }
    return right;
}

int formation_left(const Formation& f) {
    int left = std::numeric_limits<int>::max();  // sentinel when no invader is alive
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) { const int x = inv_screen_x(f, c); if (x < left) left = x; }
    return left;
}

}  // namespace

void march(Formation& f, int width) {
    if (remaining(f) == 0) return;  // no invaders left -> nothing to march (avoids the empty-formation sentinel overflow)
    if ((formation_right(f) + f.march_dir >= width) ||
        (formation_left(f) + f.march_dir < 0)) {
        f.march_dir = -f.march_dir;
        ++f.offset_y;
    } else {
        f.offset_x += f.march_dir;
    }
}

bool try_hit(Formation& f, int x, int y) {
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c) && inv_screen_x(f, c) == x && inv_screen_y(f, r) == y) {
                kill_inv(f, r, c);
                return true;
            }
    return false;
}

}  // namespace invaders
