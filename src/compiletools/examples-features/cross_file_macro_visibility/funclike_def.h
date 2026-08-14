#pragma once

/* Middle link: the function-like macro funclike_gate.h calls, guarded by the
   object-like macro funclike_level.h supplies. A first pass reads
   FUNCLIKE_LEVEL as undefined (0), so the #define is not recorded; only a
   later convergence round records the macro — body AND parameter list. */
#if FUNCLIKE_LEVEL >= 1
#define FUNCLIKE_AT_LEAST(min) (FUNCLIKE_LEVEL >= (min))
#endif
