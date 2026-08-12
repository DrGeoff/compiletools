#pragma once

/* PLATFORM_LEVEL is defined in platform_level.h, not here. Evaluating this
   condition therefore requires cross-file macro visibility. The false branch
   selects a different library, so a regression is a mislink, not a warning. */
#if PLATFORM_LEVEL >= 2
//#CXXFLAGS=-DSIMPLE_MODERN=1
//#LDFLAGS=-lmodern_platform
#else
//#CXXFLAGS=-DSIMPLE_LEGACY=1
//#LDFLAGS=-llegacy_platform
#endif
