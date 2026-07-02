// stepper.cpp -- the ONLY translation unit that includes the upstream sudoku
// headers. Two upstream properties force that confinement: grid.hpp defines a
// NON-INLINE operator<<(ostream&, const Grid&) at namespace scope (a second
// includer is an ODR link error), and the 2010-era headers use istringstream /
// back_insert_iterator / endl / cout / copy / vector unqualified, relying on
// the includer providing sudoku.cpp's using-declaration preamble -- replicated
// verbatim below, and it must stay BEFORE the upstream includes.
#include <iostream>
using std::cout;
using std::endl;
#include <iterator>
using std::back_insert_iterator;
using std::ostream_iterator;
#include <algorithm>
using std::copy;
#include <sstream>
using std::istringstream;
#include <vector>
using std::vector;

// The externals dir is on the include path (the fetch machinery widens
// INCLUDE with it), so the sudoku/src/ prefix names the fetched clone.
#include "sudoku/src/grid.hpp"
#include "sudoku/src/inconsistency.hpp"
#include "sudoku/src/uniqueperconstraintregion.hpp"
#include "sudoku/src/onlyspot.hpp"
#include "sudoku/src/lockedtuples.hpp"
#include "sudoku/src/hiddentuples.hpp"
#include "sudoku/src/intersectreject.hpp"
#include "sudoku/src/gridlock.hpp"
#include "sudoku/src/xyzwing.hpp"
#include "sudoku/src/singlevaluechains.hpp"
#include "sudoku/src/multivaluechains.hpp"

#include "stepper.hpp"

#include <stdexcept>

namespace sudoku_tui {
namespace {

using Sudoku::Cell;
using Sudoku::Constraint;
using Sudoku::ConstraintRegion;
using Sudoku::Grid;

const std::vector<Constraint::Type> kAllTypes = {Constraint::square, Constraint::row,
                                                 Constraint::column};

// Run one functor over the given constraint-region types, harvesting the
// out-params the upstream rules already fill (upstream's sudoku.cpp prints
// them; we return them). usesGrid distinguishes the two upstream call shapes.
template <typename Function>
bool sweep(Grid& grid, Function func, const std::vector<Constraint::Type>& types, Step& out) {
    std::set<Cell*> changed;
    std::set<Cell*> explanatory;
    std::string explanation;
    for (Constraint::Type type : types) {
        for (ConstraintRegion& cr : grid.get(type)) {
            if constexpr (Function::usesGrid) {
                func(cr, grid, changed, explanatory, explanation);
            } else {
                func(cr, changed, explanatory, explanation);
            }
        }
    }
    if (changed.empty()) return false;
    out.rule = func.name();
    out.explanation = explanation;
    for (Cell* cell : changed) out.changed.insert(cell->index());
    for (Cell* cell : explanatory) out.explanatory.insert(cell->index());
    return true;
}

// Priority cascade from upstream sudoku.cpp, re-expressed as "apply the first
// rule that makes progress". The per-rule region types mirror the arguments
// upstream's entry point passes (XYZWing squares-only, Gridlock rows+columns,
// chains rows-only -- these rules scan the whole grid from each seed region).
bool apply_first_rule(Grid& grid, Step& out) {
    if (sweep(grid, Sudoku::OnlySpot(), kAllTypes, out)) return true;
    if (sweep(grid, Sudoku::UniquePerConstraintRegion(), kAllTypes, out)) return true;
    if (sweep(grid, Sudoku::LockedTuples(), kAllTypes, out)) return true;
    if (sweep(grid, Sudoku::HiddenTuples(), kAllTypes, out)) return true;
    if (sweep(grid, Sudoku::XYZWing(), {Constraint::square}, out)) return true;
    if (sweep(grid, Sudoku::IntersectReject(), kAllTypes, out)) return true;
    if (sweep(grid, Sudoku::Gridlock(), {Constraint::row, Constraint::column}, out)) return true;
    if (sweep(grid, Sudoku::SingleValueChains(), {Constraint::row}, out)) return true;
    if (sweep(grid, Sudoku::MultiValueChains(), {Constraint::row}, out)) return true;
    return false;
}

// Duplicate-value check (testForZero=false: open cells are fine, two equal
// fixed values in one region are not).
bool has_contradiction(Grid& grid) {
    Step ignored;
    return sweep(grid, Sudoku::Inconsistency(false), kAllTypes, ignored);
}

bool is_solved(const Grid& grid) {
    for (const Cell& cell : grid.cells)
        if (cell.candidates().size() != 1) return false;
    return true;
}

}  // namespace

struct Stepper::Impl {
    Grid grid;
    Status status = Status::InProgress;
};

Stepper::Stepper(const std::string& puzzle81) : impl_(std::make_unique<Impl>()) {
    if (puzzle81.size() != GRID_CELLS)
        throw std::invalid_argument("puzzle must be exactly 81 characters");
    for (std::size_t i = 0; i != GRID_CELLS; ++i) {
        const char c = puzzle81[i];
        if (c >= '1' && c <= '9')
            impl_->grid.cells[i].initial(c);
        else if (c != '.' && c != '0')
            throw std::invalid_argument("puzzle characters must be 1-9, '.' or '0'");
    }
    if (has_contradiction(impl_->grid))
        impl_->status = Status::Inconsistent;
    else if (is_solved(impl_->grid))
        impl_->status = Status::Solved;
}

Stepper::~Stepper() = default;

Status Stepper::status() const { return impl_->status; }

std::optional<Step> Stepper::next_step() {
    if (impl_->status != Status::InProgress) return std::nullopt;

    Step step;
    if (!apply_first_rule(impl_->grid, step)) {
        impl_->status = Status::Stuck;
        return std::nullopt;
    }
    if (has_contradiction(impl_->grid))
        impl_->status = Status::Inconsistent;
    else if (is_solved(impl_->grid))
        impl_->status = Status::Solved;
    return step;
}

std::array<CellView, GRID_CELLS> Stepper::snapshot() const {
    std::array<CellView, GRID_CELLS> out;
    for (std::size_t i = 0; i != GRID_CELLS; ++i) {
        Cell& cell = impl_->grid.cells[i];  // Cell::value() is non-const upstream
        out[i].value = cell.value();
        out[i].candidates.clear();
        for (char candidate : cell.candidates()) out[i].candidates += candidate;
    }
    return out;
}

}  // namespace sudoku_tui
