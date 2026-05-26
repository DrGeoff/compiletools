// test_tank.cpp -- integration test for the composed aquarium (module
// aquarium.tank). Classified as a test via testmarkers=unit_test.hpp. A single
// `import aquarium.tank;` brings in the whole re-exported graph -- and makes
// ct-cake discover all five .cppm files behind it. Per-entity behaviour is
// covered by test_fish/test_bubbles/test_seaweed; this asserts the composition.
#include <cstdint>
#include <vector>

#include "unit_test.hpp"

import aquarium.tank;

int main() {
    using namespace aqua;

    // initial() populates every layer.
    {
        Tank t = initial(60, 24, 123);
        UT_REQUIRE(!t.fish.empty());
        UT_REQUIRE(!t.bubbles.empty());
        UT_REQUIRE(!t.weed.empty());
    }

    // Determinism: same seed -> identical Tank after many steps (uses the
    // re-exported Fish/Bubble/Weed operator== through vector comparison).
    {
        Tank a = initial(40, 20, 123);
        Tank b = initial(40, 20, 123);
        for (int i = 0; i < 200; ++i) { a = step(a); b = step(b); }
        UT_REQUIRE(a.tick == b.tick);
        UT_REQUIRE(a.fish == b.fish);
        UT_REQUIRE(a.bubbles == b.bubbles);
        UT_REQUIRE(a.weed == b.weed);
    }

    // Conservation: fish and bubble counts are constant across steps.
    {
        Tank t = initial(40, 20, 7);
        const auto nf = t.fish.size();
        const auto nb = t.bubbles.size();
        UT_REQUIRE(nf > 0);
        UT_REQUIRE(nb > 0);
        for (int i = 0; i < 500; ++i) t = step(t);
        UT_REQUIRE(t.fish.size() == nf);
        UT_REQUIRE(t.bubbles.size() == nb);
    }

    // Everything stays in bounds after many steps.
    {
        Tank t = initial(60, 24, 99);
        for (int i = 0; i < 300; ++i) {
            t = step(t);
            for (const Fish& f : t.fish) {
                UT_REQUIRE(f.x >= 0 && f.x < t.width);
                UT_REQUIRE(f.y > SURFACE_ROW && f.y < floor_row(t.height));
            }
            for (const Bubble& b : t.bubbles) {
                UT_REQUIRE(b.x >= 0 && b.x < t.width);
                UT_REQUIRE(b.y > SURFACE_ROW && b.y < floor_row(t.height));
            }
        }
    }

    return 0;
}
