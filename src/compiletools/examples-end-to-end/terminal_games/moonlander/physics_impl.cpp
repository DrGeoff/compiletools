// physics_impl.cpp -- Moon Lander simulation: implementation unit (module lander.physics).
//
// Defines what physics.cppm declares. <cmath> (std::fabs) lives here, not in the
// interface -- only the implementation needs it.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cmath>  // std::fabs

module lander.physics;

namespace lander {

LanderState initial() { return {START_ALTITUDE, START_VELOCITY, START_FUEL}; }

LanderState step(LanderState s, bool thrust, double dt) {
    const bool engine_on = thrust && s.fuel > 0.0;
    const double accel = -GRAVITY + (engine_on ? THRUST_ACCEL : 0.0);

    const double velocity = s.velocity + accel * dt;
    double altitude = s.altitude + velocity * dt;
    double fuel = engine_on ? s.fuel - FUEL_BURN_RATE * dt : s.fuel;

    if (altitude < 0.0) altitude = 0.0;
    if (fuel < 0.0) fuel = 0.0;

    return {altitude, velocity, fuel};
}

Verdict classify(LanderState s) {
    if (s.altitude > 0.0) return Verdict::Flying;
    return std::fabs(s.velocity) <= SAFE_LANDING_SPEED ? Verdict::Landed
                                                       : Verdict::Crashed;
}

}  // namespace lander
