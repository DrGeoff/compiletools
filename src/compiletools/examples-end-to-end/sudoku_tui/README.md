# sudoku_tui — the `//#GIT=` external-fetch showcase

A terminal step-through UI for the sudoku solver that lives in a **different
git repository** — [github.com/DrGeoff/sudoku](https://github.com/DrGeoff/sudoku),
a batch CLI that solves puzzles the way a person does and prints each
deduction with an explanation. This example is an alternate front-end for
that engine: a pencil-mark grid where each keypress applies one human-style
deduction, with the changed cells highlighted and the engine's own
explanation underneath.

The point is the first line of `stepper.hpp`:

```cpp
//#GIT=https://github.com/DrGeoff/sudoku.git@master
```

That single magic comment is the whole multi-repo story. `ct-cake` scans the
build targets **and their transitive headers** for `//#GIT=` declarations,
then:

1. **clones** the external to `<externals-dir>/sudoku` at the declared ref
   (this example's `ct.conf` pins `externals-dir = /tmp/ct-sudoku-tui-externals`
   so the clone never lands inside the compiletools checkout — the *default*
   is a sibling of your gitroot, the multi-repo workspace layout);
2. **widens the include path** with the externals dir, so
   `#include "sudoku/src/grid.hpp"` resolves into the clone (and
   self-documents where the code comes from);
3. **walks the external's headers** like any others — hunter's
   implied-source rule sees `constraintregion.hpp` and compiles the upstream
   `constraintregion.cpp` sitting next to it. No file list anywhere.

The other files are deliberately boring — plain headers, no modules, no PCH —
so the fetch lesson stays front and center:

| File | Role |
|---|---|
| `stepper.hpp` | the `//#GIT=` declaration + a pimpl'd, terminal-free stepper API |
| `stepper.cpp` | the ONLY TU that includes the upstream headers (upstream's `grid.hpp` has a non-inline `operator<<`; a second includer would be an ODR link error) |
| `sudoku_tui.cpp` | the executable: splash → pencil-mark grid → one deduction per keypress |
| `terminal.{h,cpp}` | trimmed copy of `terminal_games`' POSIX-terminal facade |
| `test_stepper.cpp` | headless test, auto-run with every build (`testmarkers = unit_test.hpp`) — it compiles against the *external's* headers, proving dep-scanning works across the repo boundary |

## Run it

```bash
cd src/compiletools/examples-end-to-end/sudoku_tui
ct-cake
bin/*/sudoku_tui          # any key = next deduction, q = quit
```

The first build clones the external; rebuilds reuse the present clone as-is
(pull explicitly with `ct-cake --update`). When stdin is not a terminal the
program runs the whole cascade headlessly and prints one deterministic line —
so it never hangs a pipe or CI:

```bash
printf '' | bin/*/sudoku_tui
# solved in 29 steps: Hidden Tuples x1, Locked Tuples x1, Only Spot x20, Unique Per Constraint Region x7
```

Bring your own puzzle as 81 characters (`1`–`9` for givens, `.` or `0` for
blanks; all other bytes ignored):

```bash
bin/*/sudoku_tui my_puzzle.txt
```

## Escape hatches

- **Already have a clone?** `CT_GIT_PATH_SUDOKU=~/code/sudoku ct-cake` (or
  `--git-path sudoku=~/code/sudoku`) uses it instead of cloning.
- **Offline?** Once the clone exists, `ct-cake --no-fetch` builds without
  touching the network. The first build with no network and no override
  fails with a `FetchError` naming the external and URL.
- **Different clone location?** `--externals-dir DIR` / `CT_EXTERNALS_DIR`
  override the ct.conf setting.

## Testing note

This example is *deliberately excluded* from the hermetic cross-backend
matrix (its whole point is a real network fetch); its end-to-end coverage is
`test_e2e_sudoku_tui.py`, which drives the full clone → resolve → widen →
build → run pipeline against the real GitHub URL and skips when offline.
