/* Convergence iteration loop, reverse include order.

   DO NOT "TIDY" THIS INCLUDE ORDER. The headers are listed top-of-chain
   first on purpose, so each one is processed before the header supplying the
   macro its condition reads. A single accumulating pass resolves none of the
   chain; only repeated passes settle it. Sorting these into dependency order
   would leave the file compiling and the flags correct while silently
   removing the only coverage of the iteration loop. The paired control test
   fails if that happens.

   The static_assert turns a wrong gate verdict into a compile error.
   Compiling this file without the computed flags is expected to fail. */
#include "chain_gate.h"
#include "chain_mid.h"
#include "chain_base.h"

#if defined(CHAIN_HIGH)
constexpr int selected_chain = 1;
#elif defined(CHAIN_LOW)
constexpr int selected_chain = 2;
#else
constexpr int selected_chain = 0;
#endif

static_assert(selected_chain == 1,
              "convergence iteration regressed: the reverse-order macro chain resolved to the wrong branch");

int main()
{
    return selected_chain - 1;
}
