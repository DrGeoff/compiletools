/* Cross-file macro visibility, forward include order.

   platform_level.h is processed before simple_gate.h, so one accumulating
   pass is enough. This file isolates the visibility property from the
   convergence iteration loop, which chain_main.cpp covers separately.

   The static_assert turns a wrong gate verdict into a compile error, so the
   flags compiletools computed can be checked by handing them to a real
   compiler. Compiling this file without those flags is expected to fail. */
#include "platform_level.h"
#include "simple_gate.h"

#if defined(SIMPLE_MODERN)
constexpr int selected_platform = 1;
#elif defined(SIMPLE_LEGACY)
constexpr int selected_platform = 2;
#else
constexpr int selected_platform = 0;
#endif

static_assert(selected_platform == 1,
              "cross-file macro visibility regressed: PLATFORM_LEVEL was not visible to simple_gate.h");

int main()
{
    return selected_platform - 1;
}
