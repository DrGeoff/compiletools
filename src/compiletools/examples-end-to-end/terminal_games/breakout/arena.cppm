// arena.cppm -- the pure Breakout simulation (named module breakout.arena).
//
// I/O-free and deterministic: the ball moves on an integer grid with unit
// velocity, so reflections are exact and testable. Both breakout.cpp and
// test_arena.cpp import this module.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <vector>

export module breakout.arena;

export namespace breakout {

inline constexpr int BRICK_ROWS = 3;
inline constexpr int PADDLE_WIDTH = 6;

enum class PaddleDir { None, Left, Right };
enum class Verdict { Playing, Won, Lost };

struct Arena {
    int width;
    int height;
    int paddle_x;             // left edge of the paddle
    int paddle_w;
    int ball_x;
    int ball_y;
    int vx;                   // -1 or +1
    int vy;                   // -1 or +1
    int brick_cols;
    std::vector<bool> bricks; // BRICK_ROWS*brick_cols, row-major; bricks occupy rows 1..BRICK_ROWS
};

inline int paddle_row(const Arena& a) { return a.height - 1; }

inline bool brick_alive(const Arena& a, int r, int c) { return a.bricks[r * a.brick_cols + c]; }
inline void kill_brick(Arena& a, int r, int c) { a.bricks[r * a.brick_cols + c] = false; }

inline int bricks_left(const Arena& a) {
    int n = 0;
    for (bool b : a.bricks) n += b ? 1 : 0;
    return n;
}

// Brick screen row (bricks start at screen row 1) and column->x mapping.
inline int brick_screen_y(int r) { return r + 1; }

inline Arena initial(int width, int height) {
    Arena a{width, height, width / 2 - PADDLE_WIDTH / 2, PADDLE_WIDTH,
            width / 2, height - 2, +1, -1, width, {}};
    a.bricks.assign(BRICK_ROWS * a.brick_cols, true);
    return a;
}

// If a living brick occupies (x, y), remove it and report the hit.
inline bool hit_brick(Arena& a, int x, int y) {
    for (int r = 0; r < BRICK_ROWS; ++r)
        if (brick_screen_y(r) == y && x >= 0 && x < a.brick_cols && brick_alive(a, r, x)) {
            kill_brick(a, r, x);
            return true;
        }
    return false;
}

inline Arena step(Arena a, PaddleDir dir) {
    if (dir == PaddleDir::Left  && a.paddle_x > 0)                    --a.paddle_x;
    if (dir == PaddleDir::Right && a.paddle_x + a.paddle_w < a.width) ++a.paddle_x;

    int nx = a.ball_x + a.vx;
    int ny = a.ball_y + a.vy;

    if (nx < 0 || nx >= a.width) { a.vx = -a.vx; nx = a.ball_x + a.vx; }  // side walls
    if (ny < 0)                  { a.vy = -a.vy; ny = a.ball_y + a.vy; }  // ceiling

    if (hit_brick(a, nx, ny)) { a.vy = -a.vy; ny = a.ball_y + a.vy; }     // brick reflects vertically

    // Paddle: ball arriving at the paddle row within the paddle span bounces up.
    if (ny >= paddle_row(a)) {
        if (nx >= a.paddle_x && nx < a.paddle_x + a.paddle_w) {
            a.vy = -a.vy;
            ny = a.ball_y + a.vy;
        }
        // else: missed -> ball falls past; classify() will read ny below paddle.
    }

    a.ball_x = nx;
    a.ball_y = ny;
    return a;
}

inline Verdict classify(const Arena& a) {
    if (bricks_left(a) == 0) return Verdict::Won;
    if (a.ball_y > paddle_row(a)) return Verdict::Lost;
    return Verdict::Playing;
}

}  // namespace breakout
