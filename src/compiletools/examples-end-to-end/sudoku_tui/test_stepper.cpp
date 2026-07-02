// test_stepper.cpp -- headless test of the deduction stepper. Including
// unit_test.hpp classifies this TU as a test (ct.conf: testmarkers =
// unit_test.hpp), so ct-cake builds and RUNS it inline with every build --
// proving dep-scanning, implied-source discovery and the test cache all work
// across the fetched repo boundary.
#include "stepper.hpp"
#include "unit_test.hpp"

#include <map>
#include <stdexcept>
#include <string>

using sudoku_tui::CellView;
using sudoku_tui::Status;
using sudoku_tui::Stepper;

namespace {

// The default puzzle must run to Solved, with every step well-formed.
void test_default_puzzle_solves() {
    Stepper stepper{sudoku_tui::DEFAULT_PUZZLE};
    UT_REQUIRE(stepper.status() == Status::InProgress);

    int steps = 0;
    while (auto step = stepper.next_step()) {
        UT_REQUIRE(!step->rule.empty());
        UT_REQUIRE(!step->changed.empty());
        for (std::size_t idx : step->changed) UT_REQUIRE(idx < sudoku_tui::GRID_CELLS);
        UT_REQUIRE(++steps <= 400);  // no livelock
    }
    UT_REQUIRE(stepper.status() == Status::Solved);
    UT_REQUIRE(steps > 0);
}

// Independent validity check of the final grid: every row, column and box
// contains each digit exactly once (not trusting the engine's own checker).
void test_solution_is_valid_sudoku() {
    Stepper stepper{sudoku_tui::DEFAULT_PUZZLE};
    while (stepper.next_step()) {
    }
    UT_REQUIRE(stepper.status() == Status::Solved);

    const auto snap = stepper.snapshot();
    for (std::size_t unit = 0; unit != 9; ++unit) {
        std::map<char, int> row, col, box;
        for (std::size_t k = 0; k != 9; ++k) {
            ++row[snap[unit * 9 + k].value];
            ++col[snap[k * 9 + unit].value];
            const std::size_t r = (unit / 3) * 3 + k / 3;
            const std::size_t c = (unit % 3) * 3 + k % 3;
            ++box[snap[r * 9 + c].value];
        }
        for (char d = '1'; d <= '9'; ++d) {
            UT_REQUIRE(row[d] == 1);
            UT_REQUIRE(col[d] == 1);
            UT_REQUIRE(box[d] == 1);
        }
    }
}

void test_snapshot_reflects_givens() {
    Stepper stepper{sudoku_tui::DEFAULT_PUZZLE};
    const auto snap = stepper.snapshot();
    UT_REQUIRE(snap[0].value == '4');            // first given
    UT_REQUIRE(snap[1].value == '0');            // blank: still open
    UT_REQUIRE(snap[1].candidates.size() > 1);   // ... with pencil marks
}

void test_invalid_input_throws() {
    bool threw = false;
    try {
        Stepper bad{"12345"};  // wrong length
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    UT_REQUIRE(threw);

    threw = false;
    try {
        std::string junk(81, 'x');  // wrong alphabet
        Stepper bad{junk};
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    UT_REQUIRE(threw);
}

void test_contradictory_puzzle_reports_inconsistent() {
    std::string twin(81, '.');
    twin[0] = '5';
    twin[1] = '5';  // two 5s in row 0: contradiction from the start
    Stepper stepper{twin};
    UT_REQUIRE(stepper.status() == Status::Inconsistent);
    UT_REQUIRE(!stepper.next_step().has_value());
}

}  // namespace

int main() {
    test_default_puzzle_solves();
    test_solution_is_valid_sudoku();
    test_snapshot_reflects_givens();
    test_invalid_input_throws();
    test_contradictory_puzzle_reports_inconsistent();
    return 0;
}
