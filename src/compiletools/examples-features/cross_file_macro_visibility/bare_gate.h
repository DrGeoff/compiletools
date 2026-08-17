#pragma once

/* A BARE function-like gate: no #ifdef wrapper, the defining header included
   on the line above it. Every real compiler resolves this, because the
   #include always runs first.

   No wrapper is what makes this tree different from funclike_gate.h, and it
   is the shape that catches a provisional pass reporting on the user's
   stderr. A pass that reads this file on its own -- with an empty macro
   state, before any convergence -- cannot expand BARE_AT_LEAST and records
   an unevaluable condition. The settled build resolves it, so any such
   report is contradicted by the build's own answer and must never reach the
   user. funclike_gate.h cannot catch that: its #ifdef wrapper is false
   under an empty state, so the inner #if is never even reached.

   No //#LDFLAGS= here on purpose. This is the one tree the tests build
   and link for real, so a made-up -l would fail the link; the
   static_assert in bare_main.cpp is what turns a wrong verdict into a
   failure. */
#include "bare_def.h"

#if BARE_AT_LEAST(4, 0)
//#CXXFLAGS=-DBARE_MODERN=1
#else
//#CXXFLAGS=-DBARE_LEGACY=1
#endif
