#ifndef PUMPLIB_PUMPCOMPAT_H
#define PUMPLIB_PUMPCOMPAT_H

#include "pumpver.h"

/* The guard macro is function-like and its operands live in an included
   header, so a single linear pass over a consumer's directives cannot
   evaluate it. Only the converged macro state can. */
#define PUMPLIB_AT_LEAST(super, major, minor)                                  \
    (PUMPLIB_VERSION_SUPER > (super) ||                                        \
     (PUMPLIB_VERSION_SUPER == (super) && PUMPLIB_VERSION_MAJOR > (major)) ||  \
     (PUMPLIB_VERSION_SUPER == (super) && PUMPLIB_VERSION_MAJOR == (major) &&  \
      PUMPLIB_VERSION_MINOR >= (minor)))

#endif
