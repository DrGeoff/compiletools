// ct-exemarker — see README.md for context.
#include "appinfo.hpp"

#include <cstdio>

int main()
{
    std::printf("name=%s\n", appinfo::name);
    std::printf("version=%s\n", appinfo::version);
    return 0;
}
