// ct-exemarker
//
// Forward-declares the symbol exported by libgreeter.so so that
// ct-cake's header spider does NOT pull greeter.cpp into this
// executable's compile set. Without the forward declaration we'd
// statically link greeting() directly and would not be exercising
// the shared-library path at all.

#include <cstdio>

const char* greeting();

//#LDFLAGS=-Lmylib/bin -lgreeter
//#CXXFLAGS=-std=c++17

int main()
{
    std::printf("%s\n", greeting());
    return 0;
}
