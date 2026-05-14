// Test TU: classified as a test (not an exe) because it transitively
// includes unit_test.hpp — the default `testmarkers = unit_test.hpp`
// directive in ct.conf.d/ct.conf is what triggers the classification.
//
// `ct-cake --auto` will compile this into bin/<variant>/test_widget,
// run it, and treat a non-zero exit as a build failure.
#include "unit_test.hpp"
#include "widget.hpp"

int main()
{
    UT_REQUIRE(widget_value() == 42);
    return 0;
}
