// arena_impl.cpp -- the pure Breakout simulation: implementation unit (module breakout.arena).
//
// Defines what arena.cppm declares. As a `module breakout.arena;` unit it
// implicitly imports the arena interface (and via `export import breakout.bricks;`
// the bricks sub-system is also in scope). ct-cake pulls this file into the link
// automatically for anything that imports breakout.arena.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

module breakout.arena;

namespace breakout {

Arena initial(int width, int height) {
    Arena a{width, height, width / 2 - PADDLE_WIDTH / 2, PADDLE_WIDTH,
            width / 2, height - 2, +1, -1, make_bricks(width)};
    return a;
}

Arena step(Arena a, PaddleDir dir) {
    if (dir == PaddleDir::Left  && a.paddle_x > 0)                    --a.paddle_x;
    if (dir == PaddleDir::Right && a.paddle_x + a.paddle_w < a.width) ++a.paddle_x;

    int nx = a.ball_x + a.vx;
    int ny = a.ball_y + a.vy;

    if (nx < 0 || nx >= a.width) { a.vx = -a.vx; nx = a.ball_x + a.vx; }  // side walls
    if (ny < 0)                  { a.vy = -a.vy; ny = a.ball_y + a.vy; }  // ceiling

    if (hit_brick(a.bricks, nx, ny)) { a.vy = -a.vy; ny = a.ball_y + a.vy; }  // brick reflects vertically

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

Verdict classify(const Arena& a) {
    if (bricks_left(a.bricks) == 0) return Verdict::Won;
    if (a.ball_y > paddle_row(a)) return Verdict::Lost;
    return Verdict::Playing;
}

}  // namespace breakout
