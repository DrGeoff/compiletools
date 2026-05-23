// physics.cppm — the single source of truth for the Moon Lander simulation.
//
// Pure, I/O-free, terminal-agnostic. Both the interactive game (game.cpp) and
// the headless test (test_physics.cpp) `import` this module, so the constants,
// the state type and the rules live in exactly one place.
//
// CAS: as a module interface unit this is precompiled to a BMI cached in
// cas-pcmdir, and its object is cached in cas-objdir.
module;

#include <cmath>  // std::fabs

export module lander.physics;

export namespace lander {

// --- Physics constants (single source of truth) ---------------------------
inline constexpr double GRAVITY            = 1.62;  // m/s^2 (lunar surface)
inline constexpr double THRUST_ACCEL       = 4.00;  // m/s^2 upward, engine on
inline constexpr double FUEL_BURN_RATE     = 8.00;  // fuel units per second
inline constexpr double SAFE_LANDING_SPEED = 2.50;  // max |velocity| to land safely
inline constexpr double TICK_SECONDS       = 0.10;  // simulation timestep

// --- Initial conditions ----------------------------------------------------
inline constexpr double START_ALTITUDE = 100.0;  // m
inline constexpr double START_VELOCITY = 0.0;    // m/s (up positive)
inline constexpr double START_FUEL     = 100.0;  // units

enum class Verdict { Flying, Landed, Crashed };

struct LanderState {
    double altitude;  // metres above the pad, never negative
    double velocity;  // m/s, positive is up
    double fuel;      // remaining fuel units, never negative
};

// The state every game and test starts from.
LanderState initial() { return {START_ALTITUDE, START_VELOCITY, START_FUEL}; }

// Advance the simulation by `dt` seconds. The engine fires only when `thrust`
// is requested AND fuel remains. Semi-implicit Euler keeps the integration
// stable; altitude is clamped at the pad while the impact velocity is
// preserved for `classify` to judge.
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

// The outcome of the current state: still falling, or a safe/unsafe touchdown.
Verdict classify(LanderState s) {
    if (s.altitude > 0.0) return Verdict::Flying;
    return std::fabs(s.velocity) <= SAFE_LANDING_SPEED ? Verdict::Landed
                                                       : Verdict::Crashed;
}

}  // namespace lander
