// ct-exemarker
//
// sudoku_tui.cpp -- an alternate TUI for github.com/DrGeoff/sudoku. The
// upstream project is a batch CLI that prints its human-style deductions to
// stdout; this program renders the same engine as an interactive step-through:
// a pencil-mark grid where each keypress applies one deduction, highlighting
// the changed cells and showing the rule's own explanation.
//
// The engine arrives via the //#GIT= declaration in stepper.hpp -- ct-cake
// clones the external, widens the include path, and discovers the upstream
// constraintregion.cpp implied source with no file list anywhere.
//
// Terminal conventions match terminal_games: splash screen, q to quit, and a
// deterministic auto-demo when stdin is not a TTY so pipes and CI never hang.
#include "stepper.hpp"
#include "terminal.h"

#include <algorithm>
#include <cstdio>
#include <format>
#include <fstream>
#include <map>
#include <optional>
#include <string>

namespace {

using sudoku_tui::CellView;
using sudoku_tui::Status;
using sudoku_tui::Step;
using sudoku_tui::Stepper;

constexpr std::string_view GREEN = "\x1b[32m";   // cells a step changed
constexpr std::string_view YELLOW = "\x1b[33m";  // cells that explain the step
constexpr std::string_view RESET = "\x1b[0m";
constexpr std::string_view CLEAR_EOL = "\x1b[K";

// The full pencil-mark rendering is 9 grid rows x 3 candidate bands + 4 rules
// + status lines; below this terminal height fall back to the compact form.
constexpr int FULL_RENDER_MIN_ROWS = 38;

std::string_view cell_colour(std::size_t idx, const std::optional<Step>& last) {
    if (!last) return "";
    if (last->changed.contains(idx)) return GREEN;
    if (last->explanatory.contains(idx)) return YELLOW;
    return "";
}

// One grid row of the pencil-mark rendering is three text lines ("bands"):
// band 0 shows candidates 1-3, band 1 shows 4-6, band 2 shows 7-9. A fixed
// cell instead shows [N] centred on the middle band.
std::string render_band(const std::array<CellView, sudoku_tui::GRID_CELLS>& snap,
                        const std::optional<Step>& last, std::size_t row, int band) {
    std::string out;
    for (std::size_t col = 0; col != 9; ++col) {
        out += (col % 3 == 0) ? "| " : "  ";
        const std::size_t idx = row * 9 + col;
        const CellView& cell = snap[idx];
        const std::string_view colour = cell_colour(idx, last);
        out += colour;
        if (cell.value != '0') {
            if (band == 1) {
                out += '[';
                out += cell.value;
                out += ']';
            } else {
                out += "   ";
            }
        } else {
            for (int k = 0; k != 3; ++k) {
                const char digit = static_cast<char>('1' + band * 3 + k);
                out += cell.candidates.find(digit) != std::string::npos ? digit : '.';
            }
        }
        if (!colour.empty()) out += RESET;
    }
    out += " |";
    return out;
}

std::string status_banner(Status status) {
    switch (status) {
        case Status::Solved:       return "*** SOLVED ***";
        case Status::Stuck:        return "*** STUCK -- beyond these techniques ***";
        case Status::Inconsistent: return "*** INCONSISTENT -- the grid contradicts itself ***";
        case Status::InProgress:   return "";
    }
    return "";
}

std::string render(const Stepper& stepper, const std::optional<Step>& last, int step_count) {
    const auto snap = stepper.snapshot();
    const std::string rule_line =
        last ? std::format("step {}: {}", step_count, last->rule) : "press any key for the first deduction";
    // Upstream explanations are multi-line (one line per elimination); the
    // status line shows the first and counts the rest.
    std::string explanation;
    if (last) {
        const std::size_t nl = last->explanation.find('\n');
        explanation = last->explanation.substr(0, nl);
        if (nl != std::string::npos && nl + 1 < last->explanation.size()) {
            const auto extra = static_cast<int>(
                std::ranges::count(last->explanation.begin() + static_cast<long>(nl) + 1,
                                   last->explanation.end(), '\n'));
            if (extra > 0) explanation += std::format("  (+{} more)", extra);
        }
    }

    std::string out = "\x1b[H";
    auto line = [&out](const std::string& text) {
        out += text;
        out += CLEAR_EOL;
        out += '\n';
    };

    line("  SUDOKU TUI -- engine fetched via //#GIT= from github.com/DrGeoff/sudoku");
    if (term::rows() >= FULL_RENDER_MIN_ROWS) {
        const std::string rule(2 + 9 * 4 + 2, '-');
        for (std::size_t row = 0; row != 9; ++row) {
            if (row % 3 == 0) line("  " + rule);
            for (int band = 0; band != 3; ++band) line("  " + render_band(snap, last, row, band));
        }
        line("  " + rule);
    } else {
        // Compact fallback for short terminals: one line per row, values only.
        for (std::size_t row = 0; row != 9; ++row) {
            std::string text = "  ";
            for (std::size_t col = 0; col != 9; ++col) {
                if (col % 3 == 0 && col != 0) text += "| ";
                const std::size_t idx = row * 9 + col;
                const std::string_view colour = cell_colour(idx, last);
                text += colour;
                text += snap[idx].value == '0' ? '.' : snap[idx].value;
                if (!colour.empty()) text += RESET;
                text += ' ';
            }
            line(text);
        }
    }
    line("");
    line("  " + rule_line);
    line("  " + explanation);
    const std::string banner = status_banner(stepper.status());
    line(banner.empty() ? "  any key = next deduction, q = quit" : "  " + banner + "   (any key exits)");
    return out;
}

char wait_for_key() {
    for (;;) {
        const char key = term::read_key();
        if (key != '\0') return key;
        term::sleep_ms(20);
    }
}

bool splash() {
    term::write_frame(std::format(
        "\x1b[2J\x1b[H\n"
        "        ====  S U D O K U   T U I  ====\n\n"
        "  An alternate front-end for the human-style solver at\n"
        "  github.com/DrGeoff/sudoku -- fetched at build time by ct-cake\n"
        "  from the //#GIT= declaration in stepper.hpp.\n\n"
        "  Watch the engine solve the way a person does: each keypress\n"
        "  applies ONE deduction. {}Changed cells{} are green, the {}cells that\n"
        "  justify the deduction{} are yellow, and the engine's own\n"
        "  explanation appears under the grid.\n\n"
        "  KEYS    any key    next deduction\n"
        "          Q          quit\n\n"
        "        ----  press any key to begin  ----\n",
        GREEN, RESET, YELLOW, RESET));
    return wait_for_key() != 'q';
}

int play_interactive(const std::string& puzzle) {
    term::RawMode raw;
    if (!splash()) return 0;

    Stepper stepper{puzzle};
    std::optional<Step> last;
    int step_count = 0;
    term::clear();
    for (;;) {
        term::write_frame(render(stepper, last, step_count));
        const char key = wait_for_key();
        if (key == 'q' || stepper.status() != Status::InProgress) break;
        if (auto step = stepper.next_step()) {
            last = std::move(step);
            ++step_count;
        }
    }
    term::write_frame("\n");
    return 0;
}

// Used when stdin is not a TTY: run the whole cascade headlessly and print
// one deterministic summary line, so the binary always terminates in
// pipes/CI. The e2e test asserts the "solved in " prefix for the default
// puzzle.
int run_demo(const std::string& puzzle) {
    Stepper stepper{puzzle};
    std::map<std::string, int> histogram;  // rule -> count; iterates sorted
    int steps = 0;
    while (auto step = stepper.next_step()) {
        ++steps;
        ++histogram[step->rule];
    }

    std::string rules;
    for (const auto& [rule, count] : histogram)
        rules += std::format("{}{} x{}", rules.empty() ? "" : ", ", rule, count);

    switch (stepper.status()) {
        case Status::Solved:
            std::printf("solved in %d steps: %s\n", steps, rules.c_str());
            return 0;
        case Status::Stuck:
            std::printf("stuck after %d steps: %s\n", steps, rules.c_str());
            return 0;
        case Status::Inconsistent:
            std::printf("inconsistent after %d steps\n", steps);
            return 0;
        case Status::InProgress:
            break;
    }
    std::printf("demo did not terminate\n");
    return 1;
}

// argv[1] names a puzzle file: the first 81 puzzle characters ('1'-'9', '.',
// '0') found in it, all other bytes ignored -- accepts one-line norvig format
// and simple grids alike.
std::optional<std::string> load_puzzle_file(const char* path) {
    std::ifstream in{path};
    if (!in) return std::nullopt;
    std::string puzzle;
    char c;
    while (puzzle.size() < sudoku_tui::GRID_CELLS && in.get(c))
        if ((c >= '0' && c <= '9') || c == '.') puzzle += c;
    if (puzzle.size() != sudoku_tui::GRID_CELLS) return std::nullopt;
    return puzzle;
}

}  // namespace

int main(int argc, char* argv[]) {
    std::string puzzle = sudoku_tui::DEFAULT_PUZZLE;
    if (argc > 1) {
        const auto loaded = load_puzzle_file(argv[1]);
        if (!loaded) {
            std::fprintf(stderr, "sudoku_tui: %s: expected a file containing 81 puzzle "
                                 "characters (1-9, '.' or '0')\n", argv[1]);
            return 1;
        }
        puzzle = *loaded;
    }
    return term::stdin_is_tty() ? play_interactive(puzzle) : run_demo(puzzle);
}
