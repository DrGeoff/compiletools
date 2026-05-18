// ct-exemarker — see README.md for context.

#include <cstdio>

#include "build/version.h"

#ifndef DEMO_PREBUILD_VERSION
#  define DEMO_PREBUILD_VERSION "(unset; prebuild script did not run)"
#endif

int main()
{
    std::printf("version=%s\n", DEMO_PREBUILD_VERSION);
    return 0;
}
