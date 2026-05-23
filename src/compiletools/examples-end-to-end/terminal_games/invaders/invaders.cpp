// ct-exemarker
//
// invaders.cpp -- the interactive Space Invaders. Imports the pure simulation
// and includes the terminal facade; no PCH here.
#include "terminal.h"

#include <cstdio>
#include <format>
#include <string>

import invaders.field;

namespace {

using invaders::Action;
using invaders::Field;
using invaders::Verdict;

std::string render(const Field& f) {
    std::string out = "\x1b[H";
    for (int y = 0; y < f.height; ++y) {
        std::string line(f.width, ' ');
        for (int r = 0; r < invaders::INV_ROWS; ++r)
            for (int c = 0; c < invaders::INV_COLS; ++c)
                if (invaders::inv_alive(f, r, c) && invaders::inv_screen_y(f, r) == y) {
                    const int x = invaders::inv_screen_x(f, c);
                    if (x >= 0 && x < f.width) line[x] = 'W';
                }
        if (f.bullet_y == y && f.bullet_x >= 0 && f.bullet_x < f.width) line[f.bullet_x] = '|';
        if (y == f.height - 1 && f.player_x >= 0 && f.player_x < f.width) line[f.player_x] = 'A';
        out += line + "\x1b[K\n";
    }
    out += std::format("INVADERS {}   {}\x1b[K", invaders::remaining(f),
                       invaders::classify(f) == Verdict::Won  ? "*** YOU WIN ***"
                     : invaders::classify(f) == Verdict::Lost ? "*** INVADED ***" : "");
    return out;
}

bool splash() {
    const std::string screen =
        "\x1b[2J\x1b[H\n"
        "        ====  S P A C E   I N V A D E R S  ====\n\n"
        "  Clear the formation before it reaches the ground.\n\n"
        "  KEYS    A / D    move      SPACE   fire\n"
        "          Q        quit\n\n"
        "        ----  press any key to begin  ----\n";
    term::write_frame(screen);
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') return false;
        if (key != '\0') return true;
        term::sleep_ms(20);
    }
}

Action key_to_action(char key) {
    switch (key) {
        case 'a': case 'h': return Action::Left;
        case 'd': case 'l': return Action::Right;
        case ' ': return Action::Fire;
        default:  return Action::None;
    }
}

int play_interactive() {
    term::RawMode raw;
    if (!splash()) return 0;
    Field f = invaders::initial(term::cols() > 30 ? 30 : term::cols(),
                                term::rows() > 14 ? 14 : term::rows() - 1);
    term::clear();
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') break;
        f = invaders::step(f, key_to_action(key));
        term::write_frame(render(f));
        if (invaders::classify(f) != Verdict::Playing) break;
        term::sleep_ms(90);
    }
    term::write_frame("\n");
    return 0;
}

// Non-TTY: capped auto-demo (player holds fire) so CI never hangs.
int run_demo() {
    Field f = invaders::initial(30, 14);
    Verdict v = Verdict::Playing;
    for (int tick = 0; tick < 5000 && v == Verdict::Playing; ++tick) {
        f = invaders::step(f, Action::Fire);
        v = invaders::classify(f);
    }
    std::printf("INVADERS demo: %d left, %s\n", invaders::remaining(f),
                v == Verdict::Won ? "win" : v == Verdict::Lost ? "invaded" : "timeout");
    return 0;
}

}  // namespace

int main() {
    return term::stdin_is_tty() ? play_interactive() : run_demo();
}
