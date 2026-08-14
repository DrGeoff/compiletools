/* Function-like macro across files, reverse include order.

   DO NOT "TIDY" THIS INCLUDE ORDER. The headers are listed top-of-chain
   first on purpose: funclike_gate.h calls a function-like macro whose
   #define in funclike_def.h is itself guarded by the object-like macro
   funclike_level.h supplies, so a single accumulating pass never records
   the function-like macro at all. This covers what neither simple_main.cpp
   nor chain_main.cpp does — both of those are object-like only — a macro
   whose body and parameter list must be carried across convergence rounds
   before expansion. Without the loop the gate emits no branch flags, only
   its unconditional sentinel (see funclike_gate.h for why), and the paired
   control test pins exactly that.

   The static_assert turns a wrong gate verdict into a compile error.
   Compiling this file without the computed flags is expected to fail. */
#include "funclike_gate.h"
#include "funclike_def.h"
#include "funclike_level.h"

#if defined(FUNCLIKE_MODERN)
constexpr int selected_funclike = 1;
#elif defined(FUNCLIKE_LEGACY)
constexpr int selected_funclike = 2;
#else
constexpr int selected_funclike = 0;
#endif

static_assert(selected_funclike == 1,
              "function-like cross-file visibility regressed: FUNCLIKE_AT_LEAST was not expandable in funclike_gate.h");

int main()
{
    return selected_funclike - 1;
}
