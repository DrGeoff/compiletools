#pragma once

/* FUNCLIKE_AT_LEAST is a function-like macro defined in funclike_def.h, not
   here, and this header is processed first. Evaluating the inner #if needs
   the macro's body and parameter list carried across convergence rounds.

   The #ifdef wrapper is load-bearing for the real compiler: a call to an
   undefined function-like macro inside #if is a hard preprocessor error
   (unlike an undefined object-like macro, which reads as 0), so without the
   wrapper this header could never be compiled with the gate unresolved. For
   compiletools the wrapper means a run without convergence emits NO
   funclike branch flags at all, which the static_assert in funclike_main.cpp
   turns into a compile error.

   The unconditional sentinel below is for the ablation control test: it
   proves this header was reached and scanned even when the wrapper is
   false, so "no branch flags" means the gate was unresolved rather than the
   file never being processed. */
//#CXXFLAGS=-DFUNCLIKE_GATE_SEEN=1
#ifdef FUNCLIKE_AT_LEAST
#if FUNCLIKE_AT_LEAST(2)
//#CXXFLAGS=-DFUNCLIKE_MODERN=1
//#LDFLAGS=-lfunclike_modern
#else
//#CXXFLAGS=-DFUNCLIKE_LEGACY=1
//#LDFLAGS=-lfunclike_legacy
#endif
#endif
