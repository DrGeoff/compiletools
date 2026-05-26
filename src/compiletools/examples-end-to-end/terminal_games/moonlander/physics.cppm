// physics.cppm — Moon Lander simulation: interface unit (module lander.physics).
//
// Pure, I/O-free, terminal-agnostic single source of truth for constants,
// types, and function signatures. The definitions live in physics_impl.cpp (a
// `module lander.physics;` implementation unit) — ct-cake discovers and links
// it from the same import edge. <cmath> (std::fabs) is only needed by the
// implementation, so it stays out of the interface.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

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
LanderState initial();

// Advance the simulation by `dt` seconds. The engine fires only when `thrust`
// is requested AND fuel remains. Semi-implicit Euler keeps the integration
// stable; altitude is clamped at the pad while the impact velocity is
// preserved for `classify` to judge.
LanderState step(LanderState s, bool thrust, double dt);

// The outcome of the current state: still falling, or a safe/unsafe touchdown.
Verdict classify(LanderState s);

}  // namespace lander
