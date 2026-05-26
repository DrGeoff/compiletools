// ct-exemarker
//
// aquarium.cpp -- the ASCII aquarium. A controls-free artwork: it imports the
// pure simulation (import aquarium.tank) and renders it through the terminal
// facade (#include "terminal.h"). The only key it reads is 'q' to quit.
//
// CAS: this TU's object lives in cas-objdir and the linked exe in cas-exedir.
//
// NOTE: the textual #includes must precede `import aquarium.tank;`. Under gcc
// -fmodules-ts, importing the module first pulls its global-module-fragment std
// headers into the global module, which then clashes with the textual
// re-inclusion of the same std headers reached via terminal.h -- e.g.
// "redefinition of std::__is_constant_evaluated" and a std::array cascade.
// Headers-then-import keeps the global module consistent (same rule as the four games).
#include "terminal.h"

#include <print>
#include <format>
#include <string>
#include <string_view>
#include <vector>

import aquarium.tank;

namespace {

// Presentation table: glyph (both orientations) + 256-colour index per species.
// The pure module only ever produces an index in [0, SPECIES_COUNT); the look
// lives here, off the simulation. ASCII only, so one glyph char == one column.
struct Species {
    std::string_view right;
    std::string_view left;
    int color;
};
constexpr Species SPECIES[aqua::SPECIES_COUNT] = {
    {"><>",      "<><",      226},  // 0 minnow,    bright yellow
    {"><(((o>",  "<o)))><",  208},  // 1 angelfish, orange
    {">))>",     "<((<",      51},  // 2 reef fish, cyan
    {"(o>",      "<o)",      201},  // 3 puffer,    pink
    {">+++o>",   "<o+++<",    46},  // 4 darter,    green
};

constexpr int WATER_BG   = 17;   // deep blue
constexpr int SURFACE_FG = 45;   // bright cyan ripples
constexpr int BUBBLE_FG  = 159;  // pale cyan
constexpr int WEED_FG    = 35;   // sea green
constexpr int SAND_FG    = 179;  // tan
constexpr int DECOR_FG   = 173;  // bronze

std::string render(const aqua::Tank& t) {
    struct Cell { char ch; int fg; };
    const int w = t.width;
    const int h = t.height;
    std::vector<Cell> grid(w * h, Cell{' ', -1});
    auto at = [&](int x, int y) -> Cell& { return grid[y * w + x]; };
    auto put = [&](int x, int y, char ch, int fg) {
        if (x >= 0 && x < w && y >= 0 && y < h) at(x, y) = Cell{ch, fg};
    };

    const int floor_y = aqua::floor_row(t.height);

    // Water surface: a gentle ripple that drifts with the tick.
    for (int x = 0; x < w; ++x) {
        const char ch = ((x + static_cast<int>(t.tick) / 3) % 8 == 0) ? '~' : '-';
        put(x, aqua::SURFACE_ROW, ch, SURFACE_FG);
    }

    // Sandy floor: a static granular pattern.
    for (int x = 0; x < w; ++x)
        put(x, floor_y, ".,_."[(x * 7 + 3) & 3], SAND_FG);

    // A small treasure chest resting on the sand (only when there is room).
    if (w >= 12 && h >= 6) {
        static constexpr std::string_view CHEST[2] = {" __ ", "[##]"};
        const int base_x = w * 3 / 4;
        for (int row = 0; row < 2; ++row)
            for (int col = 0; col < 4; ++col)
                put(base_x + col, floor_y - 2 + row, CHEST[row][col], DECOR_FG);
    }

    // Seaweed: each plant sways column-wise as a pure function of the tick.
    for (const aqua::Weed& weed : t.weed)
        for (int seg = 0; seg < weed.height; ++seg) {
            const int y = floor_y - 1 - seg;
            if (y <= aqua::SURFACE_ROW) break;
            put(weed.x + aqua::seaweed_offset(t.tick, seg), y,
                (seg & 1) ? '(' : ')', WEED_FG);
        }

    // Fish in front of the scenery.
    for (const aqua::Fish& f : t.fish) {
        const std::string_view glyph =
            (f.dir > 0) ? SPECIES[f.species].right : SPECIES[f.species].left;
        for (int i = 0; i < static_cast<int>(glyph.size()); ++i)
            put(f.x + i, f.y, glyph[i], SPECIES[f.species].color);
    }

    // Bubbles in the foreground.
    for (const aqua::Bubble& b : t.bubbles)
        put(b.x, b.y, 'o', BUBBLE_FG);

    // Serialise: home the cursor, set one water background for the whole frame,
    // then emit each cell, switching foreground colour only when it changes.
    std::string out = std::format("\x1b[H\x1b[48;5;{}m", WATER_BG);
    for (int y = 0; y < h; ++y) {
        int cur = -2;
        for (int x = 0; x < w; ++x) {
            const Cell& c = at(x, y);
            const int fg = (c.ch == ' ') ? -1 : c.fg;
            if (fg != cur) {
                out += (fg < 0) ? std::string("\x1b[39m")
                                : std::format("\x1b[38;5;{}m", fg);
                cur = fg;
            }
            out += c.ch;
        }
        out += "\x1b[K";          // clear to EOL in the current (water) background
        if (y != h - 1) out += "\n";
    }
    out += "\x1b[0m";
    return out;
}

// Title splash. Returns false if the viewer quits before diving in.
bool splash() {
    const std::string screen =
        "\x1b[2J\x1b[H\n"
        "        \x1b[38;5;51m====  A S C I I   A Q U A R I U M  ====\x1b[0m\n\n"
        "  A calm tank of drifting fish, rising bubbles and swaying\n"
        "  seaweed. There are no controls -- just watch the fish.\n\n"
        "  KEYS    \x1b[38;5;226mQ\x1b[0m   quit\n\n"
        "        ----  press any key to dive in  ----\n";
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
    // The artwork fills the whole terminal (unlike the games, which clamp to a
    // fixed play area): size the tank to the live window.
    aqua::Tank tank = aqua::initial(term::cols(), term::rows(), 0xC0FFEE);
    term::clear();
    for (;;) {
        if (term::read_key() == 'q') break;
        tank = aqua::step(tank);
        term::write_frame(render(tank));
        term::sleep_ms(90);
    }
    term::write_frame("\x1b[0m\n");
    return 0;
}

// Non-TTY: a deterministic capped run so pipes/CI never hang. Prints one line.
int run_demo() {
    aqua::Tank tank = aqua::initial(60, 20, 0xABCDEF);
    for (int tick = 0; tick < 500; ++tick)
        tank = aqua::step(tank);
    std::println("AQUARIUM demo: {} fish, {} bubbles, {} weeds after {} ticks",
                 tank.fish.size(), tank.bubbles.size(), tank.weed.size(), tank.tick);
    return 0;
}

}  // namespace

int main() {
    return term::stdin_is_tty() ? play_interactive() : run_demo();
}
