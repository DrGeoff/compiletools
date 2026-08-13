#pragma once
//#CXXFLAGS=-isystem extlib/include
//#CPPFLAGS=-isystem extlib/include
#include "extver.hpp"
#if !EXTLIB_AT_LEAST(2, 5, 7)
//#CXXFLAGS=-DPICKED_OLD
#else
//#CXXFLAGS=-DPICKED_NEW
#endif
