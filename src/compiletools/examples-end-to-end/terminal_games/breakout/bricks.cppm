// bricks.cppm -- the Breakout brick grid: interface unit (module breakout.bricks).
//
// Declares the Bricks sub-struct, its comparison, the row count, and the
// signatures of the brick operations. The definitions live in bricks_impl.cpp
// (a `module breakout.bricks;` implementation unit). A consumer's
// `import breakout.bricks;` brings in the whole bricks sub-system.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <vector>

export module breakout.bricks;

export namespace breakout {

inline constexpr int BRICK_ROWS = 3;

struct Bricks {
    int cols;
    std::vector<bool> grid;  // BRICK_ROWS*cols, row-major; bricks occupy screen rows 1..BRICK_ROWS
};
constexpr bool operator==(const Bricks& a, const Bricks& b) {
    return a.cols == b.cols && a.grid == b.grid;
}

inline bool brick_alive(const Bricks& b, int r, int c) { return b.grid[r * b.cols + c]; }
inline int brick_screen_y(int r) { return r + 1; }

Bricks make_bricks(int cols);
int bricks_left(const Bricks& b);
bool hit_brick(Bricks& b, int x, int y);

}  // namespace breakout
