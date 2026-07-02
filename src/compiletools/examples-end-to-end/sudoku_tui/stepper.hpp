//#GIT=https://github.com/DrGeoff/sudoku.git@master
//
// stepper.hpp -- a resumable, terminal-free deduction stepper over the
// human-style solver fetched from the //#GIT= external above. ct-cake scans
// build targets and their transitive headers for //#GIT= declarations; this
// header is included by both the executable and the test, so the external is
// fetched whichever target is built.
//
// The API is deliberately pimpl'd: the upstream headers define a non-inline
// operator<< at namespace scope and expect a particular using-declaration
// preamble, so stepper.cpp is the ONLY translation unit that includes them.
#pragma once

#include <array>
#include <cstddef>
#include <memory>
#include <optional>
#include <set>
#include <string>

namespace sudoku_tui {

inline constexpr std::size_t GRID_CELLS = 81;

// A puzzle the rule cascade fully solves (29 deterministic steps: Only Spot
// x20, Unique Per Constraint Region x7, Locked Tuples x1, Hidden Tuples x1)
// -- which keeps the auto-demo line and test_stepper deterministic.
inline constexpr char DEFAULT_PUZZLE[] =
    "4.....8.5.3..........7......2.....6.....8.4......1.......6.3.7"
    ".5..2.....1.4......";

// One applied deduction: which rule fired, the engine's own explanation
// text, and the affected cell indices (0..80, row-major).
struct Step {
    std::string rule;
    std::string explanation;
    std::set<std::size_t> changed;
    std::set<std::size_t> explanatory;
};

enum class Status { InProgress, Solved, Stuck, Inconsistent };

// A render-friendly view of one cell: value is '1'..'9' once fixed, '0'
// while open; candidates lists the remaining pencil marks (e.g. "379").
struct CellView {
    char value;
    std::string candidates;
};

class Stepper {
public:
    // puzzle81: exactly 81 chars, '1'-'9' for givens, '.' or '0' for blanks.
    // Throws std::invalid_argument otherwise.
    explicit Stepper(const std::string& puzzle81);
    ~Stepper();
    Stepper(const Stepper&) = delete;
    Stepper& operator=(const Stepper&) = delete;

    Status status() const;

    // Apply the highest-priority rule that makes progress. Returns the step,
    // or nullopt when the grid is already at a terminal state (which also
    // transitions status() to Solved / Stuck / Inconsistent).
    std::optional<Step> next_step();

    std::array<CellView, GRID_CELLS> snapshot() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace sudoku_tui
