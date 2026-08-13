# READMACROS declaration site

Where `//#READMACROS=` is declared changes which branch a gate resolves to.

The two subdirectories are the same project twice, differing only in which
file carries the declaration:

- `declared_beside_gate/` — `gate.hpp` holds both the `#if` and the
  `//#READMACROS=` declaration. Resolves correctly (`-DPICKED_NEW`).
- `declared_in_definer/` — the declaration sits in `extver.hpp`, beside the
  macro body, one include away from the `#if`. The gate's operands never
  reach its evaluation, every clause of `EXTLIB_AT_LEAST` reads its version
  macros as 0, and the negated gate takes the arm the compiler does not take
  (`-DPICKED_OLD`). Nothing is printed at any verbosity.

Everything else is identical: same external header, same macro body, same
gate expression, same include chain. Equality on the last version component
(2.5.7 against a gate asking for 2.5.7) is deliberate: an operand that
silently reads as 0 fails every clause, which is the shape the assume-false
fallback produces.

The `-isystem extlib/include` magic flags are relative, so parsing must run
with the working directory at the subdirectory root (the test copies the tree
into a tempdir and runs from there). `oracle.cpp` carries the gate's two arms
as ordinary code so a real preprocessor can pin the expected branch:
`<cxx> -E -P -I . -I extlib/include oracle.cpp` emits `int picked = 2;` in
both trees — the compiler ignores `//#READMACROS=` entirely, so the two
shapes differ only for compiletools.

The files the two trees would otherwise share verbatim — `main.cpp`,
`oracle.cpp` and `extlib/include/extlib/version.hpp` — each carry a comment
naming their tree. Keep the bytes distinct: compiletools' global hash
registry requires every file in the working tree to have unique content, and
two identical copies abort any run that builds the registry (`ct-findtargets`
among them).

## Tests

`test_readmacros_declaration_site.py`.
