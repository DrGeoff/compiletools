// The gate's two arms as ordinary code, for a real-preprocessor oracle:
//   <cxx> -E -P -I . -I extlib/include oracle.cpp
// must emit "int picked = 2;" (version 2.5.7 satisfies AT_LEAST(2, 5, 7)).
#include "extver.hpp"
#if !EXTLIB_AT_LEAST(2, 5, 7)
int picked = 1;
#else
int picked = 2;
#endif
