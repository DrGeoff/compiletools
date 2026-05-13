// ct-exemarker
//
// Demonstrates the opt-in --project-version / --project-name macros.
// The build script supplies values via:
//
//   ct-cake --project-name=demo_app --project-version=1.2.3 ...
//
// or the *-cmd variants:
//
//   ct-cake --project-name-cmd='basename $(pwd)' \
//           --project-version-cmd='git describe --always --dirty' ...
//
// ct-cake then injects -DCT_PROJECT_NAME="demo_app" and
// -DCT_PROJECT_VERSION="1.2.3" into CPPFLAGS, CFLAGS, and CXXFLAGS.
// Both injections are no-ops if the user does NOT opt in — see the
// "Macro Scope Filter" section of README.ct-cake.rst for why this
// is the right default.

#include <cstdio>

#ifndef CT_PROJECT_NAME
#  define CT_PROJECT_NAME "(unset; pass --project-name)"
#endif

#ifndef CT_PROJECT_VERSION
#  define CT_PROJECT_VERSION "(unset; pass --project-version)"
#endif

int main()
{
    std::printf("name=%s\n", CT_PROJECT_NAME);
    std::printf("version=%s\n", CT_PROJECT_VERSION);
    return 0;
}
