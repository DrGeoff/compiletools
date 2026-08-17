/* A bare function-like gate, in the include order a real project uses.

   bare_gate.h includes its defining header itself, so this file needs no
   particular include order and the compiler is never in doubt. That is the
   point: the only reader that cannot evaluate the gate is a compiletools
   pass that opens bare_gate.h on its own before any macro state has
   settled. Such a pass is provisional -- the settled build emits
   BARE_MODERN, which the static_assert below pins -- so it must not report
   the condition to the user.

   The static_assert turns a wrong gate verdict into a compile error.
   Compiling this file without the computed flags is expected to fail. */
#include "bare_gate.h"

#if defined(BARE_MODERN)
constexpr int selected_bare = 1;
#elif defined(BARE_LEGACY)
constexpr int selected_bare = 2;
#else
constexpr int selected_bare = 0;
#endif

static_assert(selected_bare == 1,
              "bare function-like gate regressed: BARE_AT_LEAST resolved to the wrong branch");

int main()
{
    return selected_bare - 1;
}
