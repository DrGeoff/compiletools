// arena.cppm -- the pure Breakout simulation: interface unit (module breakout.arena).
//
// Re-exports breakout.bricks and declares the Arena aggregate (which composes a
// Bricks sub-struct) together with the simulation entry points. The definitions
// live in arena_impl.cpp. A consumer's single `import breakout.arena;` brings in
// both the arena and the bricks sub-system.
//
// I/O-free and deterministic: the ball moves on an integer grid with unit
// velocity, so reflections are exact and testable.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

export module breakout.arena;

export import breakout.bricks;

export namespace breakout {

inline constexpr int PADDLE_WIDTH = 6;

enum class PaddleDir { None, Left, Right };
enum class Verdict { Playing, Won, Lost };

struct Arena {
    int width;
    int height;
    int paddle_x;   // left edge of the paddle
    int paddle_w;
    int ball_x;
    int ball_y;
    int vx;         // -1 or +1
    int vy;         // -1 or +1
    Bricks bricks;
};

inline int paddle_row(const Arena& a) { return a.height - 1; }

Arena initial(int width, int height);
Arena step(Arena a, PaddleDir dir);
Verdict classify(const Arena& a);

}  // namespace breakout
