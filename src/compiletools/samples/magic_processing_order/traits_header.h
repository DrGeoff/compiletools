#pragma once

// This header contains the macro definitions that might cause DirectMagicFlags processing order issues
// It simulates the complex macro interactions that cause the bug

// Simulate header macro definitions that cause the processing order issue
#ifdef USE_SIMULATION_MODE
    // This definition is used by the magic comment below
    #define FEATURE_CLASS MyCustomFeature
    
    // Magic comment that should be processed based on these macros
    //#LDFLAGS=-lsim_engine -lcustom_feature_impl
#endif

// Additional magic comments that depend on macro state
#if defined(USE_CUSTOM_FEATURES) && defined(USE_SIMULATION_MODE)
//#CPPFLAGS=-DFEATURE_CLASS=MyCustomFeature
#endif