#pragma once

/* The version numbers and the function-like gate macro built on them. A
   consumer that includes this header before gating is always compilable; a
   pass that reads the gating header on its own is not able to expand
   BARE_AT_LEAST at all. */
#define BARE_VERSION_MAJOR 4
#define BARE_VERSION_MINOR 2

#define BARE_AT_LEAST(major, minor) \
    (BARE_VERSION_MAJOR > (major) || (BARE_VERSION_MAJOR == (major) && BARE_VERSION_MINOR >= (minor)))
