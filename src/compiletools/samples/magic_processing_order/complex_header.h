#pragma once

// This header mimics the complex header dependencies that cause the magic processing order bug
// It includes other headers that contribute to the magic flag processing

#include "traits_header.h"

// Complex conditional magic comments that expose the processing order bug
#ifdef NDEBUG
    #ifndef USE_SIMULATION_MODE
    //#LDFLAGS=-lproduction_util
    #endif
    //#LDFLAGS=-loptimized_core
#else
    #ifndef USE_SIMULATION_MODE
    //#LDFLAGS=-ldebug_util
    #endif
    //#LDFLAGS=-lstandard_core
#endif

// Additional complexity that might trigger the processing order bug
#ifdef USE_CUSTOM_FEATURES
//#CPPFLAGS=-DUSE_CUSTOM_FEATURES -DCUSTOM_FEATURES=MyCustomFeature
#endif