// ct-exemarker
//
// snake.cpp -- the interactive Snake. The only seam that touches both the pure
// simulation (import snake.world) and the terminal facade (#include
// "terminal.h"). No PCH, no heavy headers here, so import and PCH never mix.
//
// CAS: this TU's object lives in cas-objdir and the linked exe in cas-exedir.
//
// NOTE: the textual #includes must precede `import snake.world;`. Under gcc
// -fmodules-ts, importing the module first pulls its global-module-fragment
// std headers (<cstdint>, <deque>) into the global module, which then collides
// with the textual re-inclusion of the same std headers reached via
// terminal.h (<string_view> -> <cstdint>), giving "redefinition of
// std::__is_constant_evaluated" and a std::array cascade. Headers-then-import
// keeps the global module consistent.
#include "terminal.h"

#include <cstdio>
#include <format>
#include <string>

import snake.world;

namespace {

using snake::Cell;
using snake::Direction;
using snake::Verdict;
using snake::World;

std::string render(const World& w) {
    std::string out = "\x1b[H";
    for (int y = 0; y < w.height; ++y) {
        for (int x = 0; x < w.width; ++x) {
            char ch = ' ';
            if (Cell{x, y} == w.food) ch = '*';
            for (const Cell& b : w.body)
                if (b == Cell{x, y}) { ch = (b == w.body.front()) ? '@' : 'o'; break; }
            out += ch;
        }
        out += "\x1b[K\n";
    }
    out += std::format("SCORE {}   {}\x1b[K",
                       snake::score(w),
                       w.alive ? "" : "*** GAME OVER ***");
    return out;
}

bool splash() {
    const std::string screen = std::format(
        "\x1b[2J\x1b[H\n"
        "        ====  S N A K E  ====\n\n"
        "  Steer the snake to eat the food (*). Each meal grows you by\n"
        "  one. Don't hit a wall or your own body.\n\n"
        "  START   length {}\n\n"
        "  KEYS    W/A/S/D or H/J/K/L   steer\n"
        "          Q                    quit\n\n"
        "        ----  press any key to begin  ----\n",
        snake::START_LENGTH);
    term::write_frame(screen);
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') return false;
        if (key != '\0') return true;
        term::sleep_ms(20);
    }
}

int play_interactive() {
    term::RawMode raw;
    if (!splash()) return 0;
    World w = snake::initial(term::cols() > 20 ? 20 : term::cols(),
                             term::rows() > 12 ? 12 : term::rows() - 2, 0x1234);
    term::clear();
    Direction dir = w.dir;
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') break;
        dir = snake::turn(dir, key);
        w = snake::step(w, dir);
        term::write_frame(render(w));
        if (snake::classify(w) == Verdict::Dead) break;
        term::sleep_ms(120);
    }
    term::write_frame("\n");
    return 0;
}

// Non-TTY: deterministic capped auto-demo so pipes/CI never hang.
int run_demo() {
    World w = snake::initial(20, 10, 0xABCD);
    for (int tick = 0; tick < 1000 && w.alive; ++tick)
        w = snake::step(w, w.dir);  // drives straight into a wall, then stops
    std::printf("SNAKE demo: score %d, %s\n",
                snake::score(w), w.alive ? "still playing" : "game over");
    return 0;
}

}  // namespace

int main() {
    return term::stdin_is_tty() ? play_interactive() : run_demo();
}
