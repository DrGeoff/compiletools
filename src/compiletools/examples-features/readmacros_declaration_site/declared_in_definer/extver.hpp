#pragma once
//#READMACROS=extlib/version.hpp
#include <extlib/version.hpp>
#define EXTLIB_AT_LEAST(major, minor, patch) \
    ((EXTLIB_VERSION_MAJOR > (major)) || \
     (EXTLIB_VERSION_MAJOR == (major) && EXTLIB_VERSION_MINOR > (minor)) || \
     (EXTLIB_VERSION_MAJOR == (major) && EXTLIB_VERSION_MINOR == (minor) && \
      EXTLIB_VERSION_PATCH >= (patch)))
