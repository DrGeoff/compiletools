// Test for duplicate flag deduplication
// This file intentionally creates duplicate flags to test deduplication

//#CPPFLAGS=-I/usr/include/test -DDUPLICATE_MACRO
//#CPPFLAGS=-I/usr/include/test -DDIFFERENT_MACRO
//#CXXFLAGS=-std=c++17 -I/usr/include/test
//#CXXFLAGS=-Wall -I/usr/include/test

// PKG-CONFIG flags that might overlap with manual flags
//#PKG-CONFIG=zlib
//#CPPFLAGS=-isystem /usr/include/zlib

// LDFLAGS that should be deduplicated
//#LDFLAGS=-L /usr/lib -l math
//#LDFLAGS=-L/usr/lib -lmath
//#LDFLAGS=-L /usr/local/lib -l pthread

// Deprecated LINKFLAGS (should also be deduplicated)
//#LINKFLAGS=-L/usr/lib -lm
//#LINKFLAGS=-L /usr/lib -l m

#include <iostream>

int main()
{
    std::cout << "Testing duplicate flag deduplication" << std::endl;

#ifdef DUPLICATE_MACRO
    std::cout << "DUPLICATE_MACRO is defined" << std::endl;
#endif

#ifdef DIFFERENT_MACRO
    std::cout << "DIFFERENT_MACRO is defined" << std::endl;
#endif

    return 0;
}