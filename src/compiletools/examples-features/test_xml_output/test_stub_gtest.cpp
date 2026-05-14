// gtest-flavoured test fixture for compiletools' --test-xml-dir e2e.
// Intentionally does NOT link against real GoogleTest -- the stub
// header at gtest/gtest.h supplies nothing, and main() handles the
// gtest XML flag by hand so the e2e test can verify ct-cake passed
// the right argv without needing GoogleTest installed on every CI
// runner.
//
// Detection trips on the include path token "gtest/gtest.h" appearing
// in the transitive header set (see compiletools.test_framework).

#include "gtest/gtest.h"

#include <cstdio>
#include <cstring>

int main(int argc, char** argv)
{
    static const char prefix[] = "--gtest_output=xml:";
    const size_t prefix_len = sizeof(prefix) - 1;

    for (int i = 1; i < argc; ++i)
    {
        if (std::strncmp(argv[i], prefix, prefix_len) == 0)
        {
            const char* path = argv[i] + prefix_len;
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
