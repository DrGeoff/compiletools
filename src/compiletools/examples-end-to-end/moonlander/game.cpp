// ct-exemarker
//
// game.cpp — the interactive Moon Lander. The only seam that touches both the
// pure simulation (import lander.physics) and the terminal facade (#include
// "terminal.h"): read a key, step the physics, render an ASCII frame from the
// state, write it. It deliberately uses neither a PCH nor heavy system headers,
// so PCH and `import` never mix in one TU.
//
// CAS: this TU's object is cached in cas-objdir and the linked executable in
// cas-exedir.
import lander.physics;

#include "terminal.h"

#include <cstdio>
#include <format>
#include <string>

namespace {

using lander::LanderState;
using lander::Verdict;

constexpr int LEFT_MARGIN = 14;

const char* verdict_banner(Verdict v) {
    switch (v) {
        case Verdict::Landed:  return "*** THE EAGLE HAS LANDED ***";
        case Verdict::Crashed: return "*** CRASHED ***";
        case Verdict::Flying:  return "";
    }
    return "";
}

// Render the whole scene as one string with embedded newlines. Each line ends
// with "\x1b[K" (clear-to-end-of-line) so a shrinking HUD leaves no artifacts.
std::string render(LanderState s, Verdict v, bool thrusting, int rows) {
    const int sky = rows > 4 ? rows - 2 : 3;  // reserve ground + HUD rows
    double f = s.altitude / lander::START_ALTITUDE;
    if (f < 0.0) f = 0.0;
    if (f > 1.0) f = 1.0;
    const int lander_row = static_cast<int>((1.0 - f) * (sky - 1) + 0.5);
    const std::string margin(LEFT_MARGIN, ' ');

    std::string out = "\x1b[H";
    for (int r = 0; r < sky; ++r) {
        if (r == lander_row)
            out += margin + "/\\";
        else if (thrusting && r == lander_row + 1)
            out += margin + "vv";
        out += "\x1b[K\n";
    }
    out += std::string(LEFT_MARGIN + 8, '=') + "\x1b[K\n";  // landing pad
    out += std::format("ALT {:6.1f} m   VEL {:7.2f} m/s   FUEL {:5.1f}   {}\x1b[K",
                       s.altitude, s.velocity, s.fuel,
                       v == Verdict::Flying ? (thrusting ? "THRUST" : "")
                                            : verdict_banner(v));
    return out;
}

// The keys that fire the thruster. Named so the splash instructions and the
// game loop share one definition and can never describe different keys.
bool is_thrust_key(char key) { return key == ' ' || key == 'w' || key == 'k'; }

// Title + instructions, shown before play. Returns false if the player quits
// from the splash, true to launch. The numbers come straight from the
// simulation constants, so the instructions can never drift from the physics.
bool splash() {
    const std::string screen = std::format(
        "\x1b[2J\x1b[H\n"
        "        ====  M O O N   L A N D E R  ====\n\n"
        "  Pilot the lunar module down to the surface. Fire the\n"
        "  thruster to slow your descent -- but mind the fuel, because\n"
        "  once the tank runs dry you are at the mercy of gravity.\n\n"
        "  GOAL    touch down at {:.1f} m/s or slower to land safely;\n"
        "          come in any faster and you crash.\n\n"
        "  START   altitude {:.0f} m     fuel {:.0f} units\n\n"
        "  KEYS    SPACE / W / K    fire thruster\n"
        "          Q                quit\n\n"
        "        ----  press any key to begin  ----\n",
        lander::SAFE_LANDING_SPEED, lander::START_ALTITUDE, lander::START_FUEL);
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

    LanderState s = lander::initial();
    term::clear();

    for (;;) {
        const Verdict v = lander::classify(s);
        const char key = term::read_key();
        if (key == 'q') break;
        const bool thrusting = v == Verdict::Flying && is_thrust_key(key);

        term::write_frame(render(s, v, thrusting, term::rows()));
        if (v != Verdict::Flying) break;

        s = lander::step(s, thrusting, lander::TICK_SECONDS);
        term::sleep_ms(80);
    }

    term::write_frame("\n");
    return 0;
}

// Used only when stdin is not a TTY, so the binary stays runnable (and always
// terminates) in pipes/CI. Thrust decision is a pure function of the state.
bool autopilot(LanderState s) {
    return s.velocity < -3.0 || (s.altitude < 25.0 && s.velocity < -1.0);
}

int run_demo() {
    LanderState s = lander::initial();
    for (int tick = 0; tick < 10'000; ++tick) {
        const Verdict v = lander::classify(s);
        if (v != Verdict::Flying) {
            std::printf("%s  (altitude %.1f m, velocity %.2f m/s, fuel %.1f)\n",
                        verdict_banner(v), s.altitude, s.velocity, s.fuel);
            return 0;
        }
        s = lander::step(s, autopilot(s), lander::TICK_SECONDS);
    }
    std::printf("demo did not terminate\n");
    return 1;
}

}  // namespace

int main() {
    return term::stdin_is_tty() ? play_interactive() : run_demo();
}
