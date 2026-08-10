#include "pump.h"
#include "pumpcompat.h"

/* PUMPLIB_AT_LEAST(1, 2, 0) is true at the pinned version, so the modern
   branch is the one the compiler takes and the one the dependency list
   must report. */
#if PUMPLIB_AT_LEAST(1, 2, 0)
#include "modern_pump.h"
#else
#include "legacy_pump.h"
#endif

const char* describe_pump() { return pump_flavour(); }
