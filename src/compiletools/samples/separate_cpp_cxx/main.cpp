// ct-exemarker
//
// Demonstrates --separate-flags-CPP-CXX.
//
// By default, ct-cake unifies //#CPPFLAGS and //#CXXFLAGS into a single
// deduplicated set: a flag set on either side reaches both the
// preprocessor and the C++ compiler. With --separate-flags-CPP-CXX,
// each annotation is routed only to its own slot.
//
// The two annotations below define DIFFERENT macros via the two
// channels. The expected token expansion in main() is:
//
//   default                       --separate-flags-CPP-CXX
//   -----------                   ------------------------
//   FROM_CPP=1 FROM_CXX=1         FROM_CPP=1 FROM_CXX=1
//
// (both reach the compile in both modes — but only because the
// preprocessor sees CXXFLAGS too in unified mode and only because both
// ultimately land on the C++ compile command in separated mode either.)
//
// The user-visible difference shows up when a flag is *not* a -D and
// hence has different meaning to each tool, e.g. -E (preprocessor-only)
// vs -O2 (codegen). The annotations below pick precisely such a case:
// -DUNIFIED_FLAGS_SEEN is added by main() at the C++ side only, while
// -DPREPROCESSOR_ONLY is meaningful only at the CPP side. Run
// `ct-magicflags --separate-flags-CPP-CXX main.cpp` to see them split,
// and the same command without the flag to see them merged.
//
//#CPPFLAGS=-DFROM_CPP=1 -DPREPROCESSOR_ONLY
//#CXXFLAGS=-DFROM_CXX=1 -DUNIFIED_FLAGS_SEEN

#include <cstdio>

int main()
{
#if defined(FROM_CPP) && defined(FROM_CXX)
    std::printf("FROM_CPP=%d FROM_CXX=%d\n", FROM_CPP, FROM_CXX);
#else
    std::printf("at least one channel did not reach the compiler\n");
#endif
    return 0;
}
