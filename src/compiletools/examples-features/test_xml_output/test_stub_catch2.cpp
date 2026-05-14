// Catch2-flavoured test fixture for compiletools' --test-xml-dir e2e.
// Like test_stub_gtest.cpp / test_stub_doctest.cpp this does NOT link
// against real Catch2 -- the stub header at catch2/catch_all.hpp is
// empty and main() handles the four-token Catch2 XML argv by hand.
//
// Detection trips on the include-path token "catch2/catch_all.hpp"
// appearing in the transitive header set (see compiletools.test_framework
// -- the Catch2 entry also matches "catch2/catch.hpp" and bare
// "catch.hpp" but this stub uses the canonical v3 spelling).

#include "catch2/catch_all.hpp"

#include <cstdio>
#include <cstring>

int main(int argc, char** argv)
{
    // Catch2 wires the XML output via four argv tokens:
    //   --reporter junit --out PATH
    // We scan for "--out" and take the *next* token as the path.
    for (int i = 1; i + 1 < argc; ++i)
    {
        if (std::strcmp(argv[i], "--out") == 0)
        {
            const char* path = argv[i + 1];
            FILE* f = std::fopen(path, "w");
            if (f != nullptr)
            {
                std::fputs("<testsuites>\n", f);
                std::fputs("  <testsuite name=\"stub\" tests=\"1\" failures=\"0\">\n", f);
                std::fputs("    <testcase name=\"stub_pass\" classname=\"stub\"/>\n", f);
                std::fputs("  </testsuite>\n", f);
                std::fputs("</testsuites>\n", f);
                std::fclose(f);
            }
            break;
        }
    }
    return 0;
}
