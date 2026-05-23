// field.cppm -- the pure Space Invaders simulation (named module invaders.field).
//
// I/O-free and deterministic. A block of invaders marches left/right and drops a
// row at the edges; a single player bullet rises and clears invaders. No enemy
// fire (YAGNI). Both invaders.cpp and test_field.cpp import this module.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <vector>

export module invaders.field;

export namespace invaders {

inline constexpr int INV_ROWS = 3;
inline constexpr int INV_COLS = 6;
inline constexpr int MARCH_EVERY = 4;   // ticks between formation steps
inline constexpr int NO_BULLET = -1;

enum class Action { None, Left, Right, Fire };
enum class Verdict { Playing, Won, Lost };

struct Field {
    int width;
    int height;
    int player_x;
    int offset_x;             // formation top-left column
    int offset_y;             // formation top-left row
    int march_dir;            // +1 right, -1 left
    int bullet_x;             // NO_BULLET when no bullet in flight
    int bullet_y;
    int ticks;
    std::vector<bool> alive;  // INV_ROWS*INV_COLS, row-major
};

inline bool inv_alive(const Field& f, int r, int c) { return f.alive[r * INV_COLS + c]; }
inline void kill_inv(Field& f, int r, int c) { f.alive[r * INV_COLS + c] = false; }

inline int remaining(const Field& f) {
    int n = 0;
    for (bool a : f.alive) n += a ? 1 : 0;
    return n;
}

// Absolute screen position of invader (r,c) given the current formation offset.
inline int inv_screen_x(const Field& f, int c) { return f.offset_x + c * 2; }
inline int inv_screen_y(const Field& f, int r) { return f.offset_y + r; }

inline Field initial(int width, int height) {
    Field f{width, height, width / 2, 1, 1, +1, NO_BULLET, NO_BULLET, 0,
            std::vector<bool>(INV_ROWS * INV_COLS, true)};
    return f;
}

// Lowest occupied screen row of any living invader (for the Lost test).
inline int formation_bottom(const Field& f) {
    int bottom = -1;
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) {
                const int y = inv_screen_y(f, r);
                if (y > bottom) bottom = y;
            }
    return bottom;
}

// Rightmost / leftmost living-invader screen columns (for edge detection).
inline int formation_right(const Field& f) {
    int right = 0;
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) { const int x = inv_screen_x(f, c); if (x > right) right = x; }
    return right;
}
inline int formation_left(const Field& f) {
    int left = f.width;
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c)) { const int x = inv_screen_x(f, c); if (x < left) left = x; }
    return left;
}

inline void advance_bullet(Field& f) {
    if (f.bullet_y == NO_BULLET) return;
    --f.bullet_y;
    if (f.bullet_y < 0) { f.bullet_x = f.bullet_y = NO_BULLET; return; }
    for (int r = 0; r < INV_ROWS; ++r)
        for (int c = 0; c < INV_COLS; ++c)
            if (inv_alive(f, r, c) &&
                inv_screen_x(f, c) == f.bullet_x && inv_screen_y(f, r) == f.bullet_y) {
                kill_inv(f, r, c);
                f.bullet_x = f.bullet_y = NO_BULLET;
                return;
            }
}

inline void march(Field& f) {
    if ((formation_right(f) + f.march_dir >= f.width) ||
        (formation_left(f) + f.march_dir < 0)) {
        f.march_dir = -f.march_dir;  // reverse and drop
        ++f.offset_y;
    } else {
        f.offset_x += f.march_dir;
    }
}

inline Field step(Field f, Action action) {
    if (action == Action::Left  && f.player_x > 0)             --f.player_x;
    if (action == Action::Right && f.player_x < f.width - 1)   ++f.player_x;
    if (action == Action::Fire  && f.bullet_y == NO_BULLET) {
        f.bullet_x = f.player_x;
        f.bullet_y = f.height - 2;  // just above the player row
    }
    advance_bullet(f);
    if (++f.ticks % MARCH_EVERY == 0) march(f);
    return f;
}

inline Verdict classify(const Field& f) {
    if (remaining(f) == 0) return Verdict::Won;
    if (formation_bottom(f) >= f.height - 1) return Verdict::Lost;
    return Verdict::Playing;
}

}  // namespace invaders
