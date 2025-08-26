#include <system/version.h>

// Use READMACROS to explicitly specify system header for macro extraction
//#READMACROS=fake_system_include/system/version.h

// Conditional compilation based on macros from system header
#if (SYSTEM_VERSION_MAJOR < 2) || (SYSTEM_VERSION_MAJOR == 2 && SYSTEM_VERSION_MINOR < 10)
//#CPPFLAGS=-DUSE_LEGACY_API -DLEGACY_HANDLER=system::LegacyProcessor
//#CXXFLAGS=-DUSE_LEGACY_API -DLEGACY_HANDLER=system::LegacyProcessor  
#else
//#CPPFLAGS=-DSYSTEM_ENABLE_V2 -DV2_PROCESSOR_CLASS=system::ModernProcessor
//#CXXFLAGS=-DSYSTEM_ENABLE_V2 -DV2_PROCESSOR_CLASS=system::ModernProcessor
#endif

// Common flags that should appear in both cases
//#CPPFLAGS=-DSYSTEM_CORE_ENABLED -DSYSTEM_CONFIG_NAMESPACE=SYSTEM_CORE

int main() {
    return 0;
}