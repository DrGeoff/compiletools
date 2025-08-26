#pragma once
//#PKG-CONFIG=zlib

// Test conditional magic comments that expose DirectMagicFlags processing order bug
// This mimics the pattern that causes the issue

#ifdef NDEBUG
    #ifndef USE_SIMULATION_MODE
    //#LDFLAGS=-lproduction_lib
    #endif
    //#LDFLAGS=-loptimized_lib
#else
    #ifndef USE_SIMULATION_MODE  
    //#LDFLAGS=-ldebug_lib
    #endif
    //#LDFLAGS=-lstandard_lib
#endif

// Additional magic comments that should be processed
#ifdef USE_CUSTOM_FEATURES
//#CPPFLAGS=-DCUSTOM_FEATURES=MyCustomFeature
//#LDFLAGS=-lcustom_feature_lib
#endif

