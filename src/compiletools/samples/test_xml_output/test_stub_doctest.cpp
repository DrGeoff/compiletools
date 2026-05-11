// doctest-flavoured test fixture for compiletools' --test-xml-dir e2e.
// Like test_stub_gtest.cpp, this does NOT link against real doctest.

#include "doctest/doctest.h"

#include <cstdio>
#include <cstring>

int main(int argc, char** argv)
{
    // doctest uses TWO argv tokens: --reporters=junit and --out=PATH.
    // Find --out=PATH and write a minimal JUnit-shaped file.
    static const char out_prefix[] = "--out=";
    const size_t out_len = sizeof(out_prefix) - 1;

    for (int i = 1; i < argc; ++i)
    {
        if (std::strncmp(argv[i], out_prefix, out_len) == 0)
        {
            const char* path = argv[i] + out_len;
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
        }
    }
    return 0;
}
