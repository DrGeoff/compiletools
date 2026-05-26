// bubbles.cppm -- the aquarium's bubbles (module aquarium.bubbles).
//
// Owns the Bubble state and its per-bubble behaviour: spawning, rising and
// respawning at the surface. Pure and deterministic in the seed it is handed.
// Imports aquarium.water; never learns what a Tank is.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.bubbles;

import aquarium.water;

export namespace aqua {

struct Bubble {
    int x;
    int y;
    int speed;
    int accum;
};
constexpr bool operator==(const Bubble& a, const Bubble& b) {
    return a.x == b.x && a.y == b.y && a.speed == b.speed && a.accum == b.accum;
}

// Re-emit a bubble near the floor at a fresh column. Deterministic in seed.
inline void respawn_bubble(Bubble& b, int width, int height, std::uint64_t& seed) {
    b.x = rand_range(seed, width);
    b.y = water_bottom(height);
    b.speed = 20 + rand_range(seed, 40);  // 0.20 .. 0.59 cell/tick
    b.accum = 0;
}

// A fresh bubble somewhere in the open water. Deterministic.
inline Bubble spawn_bubble(int width, int height, std::uint64_t& seed) {
    const int span = water_bottom(height) - water_top() + 1;
    Bubble b{};
    b.x = rand_range(seed, width);
    b.y = water_top() + rand_range(seed, span);
    b.speed = 20 + rand_range(seed, 40);
    b.accum = 0;
    return b;
}

// Advance one tick: rise by the fixed-point speed, respawning near the floor
// once it reaches the surface.
inline void advance_bubble(Bubble& b, int width, int height, std::uint64_t& seed) {
    b.accum += b.speed;
    while (b.accum >= SPEED_SCALE) {
        b.accum -= SPEED_SCALE;
        --b.y;
    }
    if (b.y <= SURFACE_ROW)
        respawn_bubble(b, width, height, seed);
}

}  // namespace aqua
