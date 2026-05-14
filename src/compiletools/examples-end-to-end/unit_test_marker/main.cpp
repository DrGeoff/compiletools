// Plain executable: ct-cake's auto-discovery sees `main(` (the default
// exemarker) and *no* unit_test.hpp transitive include, so this is
// classified as an exe and built into bin/<variant>/main.
#include "widget.hpp"
#include <cstdio>

int main()
{
    std::printf("widget = %d\n", widget_value());
    return 0;
}
