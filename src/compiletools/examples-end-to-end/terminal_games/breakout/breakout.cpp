// ct-exemarker
//
// breakout.cpp -- the interactive Breakout. Imports the pure simulation and
// includes the terminal facade; no PCH here.
//
// All textual #includes precede the module import: under gcc -fmodules-ts a
// header included *after* an import can be mis-attached to the imported
// module's purview (symptoms: std::string "does not name a type",
// term::write_frame "not a member"). Includes-first keeps them in the global
// module.
#include "terminal.h"

#include <print>
#include <format>
#include <string>

import breakout.arena;

namespace {

using breakout::Arena;
using breakout::PaddleDir;
using breakout::Verdict;

std::string render(const Arena& a) {
    std::string out = "\x1b[H";
    for (int y = 0; y < a.height; ++y) {
        std::string line(a.width, ' ');
        for (int r = 0; r < breakout::BRICK_ROWS; ++r)
            if (breakout::brick_screen_y(r) == y)
                for (int c = 0; c < a.brick_cols; ++c)
                    if (breakout::brick_alive(a, r, c) && c < a.width) line[c] = '#';
        if (a.ball_y == y && a.ball_x >= 0 && a.ball_x < a.width) line[a.ball_x] = 'O';
        if (y == breakout::paddle_row(a))
            for (int i = 0; i < a.paddle_w && a.paddle_x + i < a.width; ++i) line[a.paddle_x + i] = '=';
        out += line + "\x1b[K\n";
    }
    out += std::format("BRICKS {}   {}\x1b[K", breakout::bricks_left(a),
                       breakout::classify(a) == Verdict::Won  ? "*** YOU WIN ***"
                     : breakout::classify(a) == Verdict::Lost ? "*** BALL LOST ***" : "");
    return out;
}

bool splash() {
    const std::string screen =
        "\x1b[2J\x1b[H\n"
        "        ====  B R E A K O U T  ====\n\n"
        "  Bounce the ball (O) off your paddle (=) to smash every brick (#).\n"
        "  Don't let the ball fall past the paddle.\n\n"
        "  KEYS    A / D    move paddle      Q   quit\n\n"
        "        ----  press any key to begin  ----\n";
    term::write_frame(screen);
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') return false;
        if (key != '\0') return true;
        term::sleep_ms(20);
    }
}

PaddleDir key_to_dir(char key) {
    switch (key) {
        case 'a': case 'h': return PaddleDir::Left;
        case 'd': case 'l': return PaddleDir::Right;
        default:  return PaddleDir::None;
    }
}

int play_interactive() {
    term::RawMode raw;
    if (!splash()) return 0;
    Arena a = breakout::initial(term::cols() > 30 ? 30 : term::cols(),
                                term::rows() > 14 ? 14 : term::rows() - 1);
    term::clear();
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') break;
        a = breakout::step(a, key_to_dir(key));
        term::write_frame(render(a));
        if (breakout::classify(a) != Verdict::Playing) break;
        term::sleep_ms(90);
    }
    term::write_frame("\n");
    return 0;
}

// Non-TTY: capped auto-demo with a paddle that tracks the ball, so it terminates.
int run_demo() {
    Arena a = breakout::initial(30, 14);
    Verdict v = Verdict::Playing;
    for (int tick = 0; tick < 20000 && v == Verdict::Playing; ++tick) {
        const PaddleDir d = a.ball_x < a.paddle_x ? PaddleDir::Left
                          : a.ball_x > a.paddle_x + a.paddle_w - 1 ? PaddleDir::Right
                          : PaddleDir::None;
        a = breakout::step(a, d);
        v = breakout::classify(a);
    }
    std::println("BREAKOUT demo: {} bricks, {}", breakout::bricks_left(a),
                 v == Verdict::Won ? "win" : v == Verdict::Lost ? "ball lost" : "timeout");
    return 0;
}

}  // namespace

int main() {
    return term::stdin_is_tty() ? play_interactive() : run_demo();
}
