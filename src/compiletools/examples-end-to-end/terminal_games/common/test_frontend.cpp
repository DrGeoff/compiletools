// test_frontend.cpp -- headless unit test for the pure splash_screen builder.
// Classified as a test via testmarkers=unit_test.hpp (root ct.conf). Links the
// shared frontend.o (and transitively terminal.o), so the same shared objects
// the five programs use also back this test. wait_for_start is I/O-bound (needs
// a TTY) and is exercised by the interactive programs, not here.
#include "frontend.h"
#include "unit_test.hpp"

#include <string>
#include <string_view>

namespace {
bool contains(std::string_view hay, std::string_view needle) {
    return hay.find(needle) != std::string_view::npos;
}
}  // namespace

int main() {
    const std::string s =
        frontend::splash_screen("==== TITLE ====", "body line", "KEYS  Q quit");

    UT_REQUIRE(contains(s, "\x1b[2J\x1b[H"));        // clears the screen
    UT_REQUIRE(contains(s, "==== TITLE ===="));      // title banner verbatim
    UT_REQUIRE(contains(s, "body line"));            // body verbatim
    UT_REQUIRE(contains(s, "KEYS  Q quit"));         // key help verbatim
    UT_REQUIRE(contains(s, "press any key to begin"));  // default verb footer

    // Custom verb flows through to the footer.
    const std::string dive =
        frontend::splash_screen("T", "B", "K", "dive in");
    UT_REQUIRE(contains(dive, "press any key to dive in"));

    // The ANSI vocabulary constants are the expected escapes.
    UT_REQUIRE(frontend::CURSOR_HOME == "\x1b[H");
    UT_REQUIRE(frontend::CLEAR_EOL == "\x1b[K");
    UT_REQUIRE(frontend::RESET == "\x1b[0m");

    return 0;
}
