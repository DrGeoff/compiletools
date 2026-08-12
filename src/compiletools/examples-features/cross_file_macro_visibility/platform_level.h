#pragma once

/* The macro the gate in simple_gate.h reads. It lives in a different file on
   purpose: SimplePreprocessor does not follow #include, so the only thing that
   makes this value visible to that #if is the DirectMagicFlags convergence
   accumulating macro state across files. */
#define PLATFORM_LEVEL 3
