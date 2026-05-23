// test_physics.cpp — headless unit test for the pure simulation.
//
// Classified as a *test* (not an exe) because it includes unit_test.hpp,
// matching ct.conf's `testmarkers = unit_test.hpp`. ct-cake builds it, runs
// it, and treats a non-zero exit as a build failure. Being pure logic it runs
// identically on every backend in CI, and its pass/fail result is cached
// (content-keyed) so unchanged re-runs are skipped.
#include "unit_test.hpp"

import lander.physics;

namespace {

constexpr bool near(double a, double b, double eps = 1e-9) {
    const double d = a - b;
    return (d < 0 ? -d : d) <= eps;
}

}  // namespace

int main() {
    using namespace lander;

    // Free-fall: gravity alone pulls velocity and altitude down; fuel intact.
    {
        const LanderState s = step({100.0, 0.0, 50.0}, /*thrust=*/false, 1.0);
        UT_REQUIRE(near(s.velocity, -GRAVITY));
        UT_REQUIRE(near(s.altitude, 100.0 - GRAVITY));
        UT_REQUIRE(near(s.fuel, 50.0));
    }

    // Engine on with fuel: net upward acceleration, fuel burns.
    {
        const LanderState s = step({100.0, 0.0, 50.0}, /*thrust=*/true, 1.0);
        UT_REQUIRE(near(s.velocity, THRUST_ACCEL - GRAVITY));
        UT_REQUIRE(near(s.fuel, 50.0 - FUEL_BURN_RATE));
    }

    // Empty tank: a thrust request is ignored, so it behaves like free-fall.
    {
        const LanderState s = step({100.0, 0.0, 0.0}, /*thrust=*/true, 1.0);
        UT_REQUIRE(near(s.velocity, -GRAVITY));
        UT_REQUIRE(near(s.fuel, 0.0));
    }

    // Fuel that runs out mid-tick still fires this tick, then clamps to zero.
    {
        const LanderState s = step({100.0, 0.0, 1.0}, /*thrust=*/true, 1.0);
        UT_REQUIRE(near(s.velocity, THRUST_ACCEL - GRAVITY));
        UT_REQUIRE(near(s.fuel, 0.0));
    }

    // Altitude never goes negative; the impact velocity is preserved.
    {
        const LanderState s = step({1.0, -10.0, 50.0}, /*thrust=*/false, 1.0);
        UT_REQUIRE(near(s.altitude, 0.0));
        UT_REQUIRE(s.velocity < 0.0);
    }

    // classify: airborne vs safe vs unsafe touchdown, at the boundary.
    UT_REQUIRE(classify({50.0, -3.0, 10.0}) == Verdict::Flying);
    UT_REQUIRE(classify({0.0, -SAFE_LANDING_SPEED, 0.0}) == Verdict::Landed);
    UT_REQUIRE(classify({0.0, -(SAFE_LANDING_SPEED + 0.01), 0.0}) == Verdict::Crashed);

    return 0;
}
