// bubbles_impl.cpp -- the aquarium's bubbles: implementation unit (module aquarium.bubbles).
//
// Defines what bubbles.cppm declares; imports aquarium.water for the RNG,
// geometry and motion scale. ct-cake links this unit automatically for anything
// that imports aquarium.bubbles.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cstdint>

module aquarium.bubbles;

import aquarium.water;

namespace aqua {
namespace {

// Re-emit a bubble near the floor at a fresh column. In an anonymous namespace
// (internal linkage): private to this implementation unit.
void respawn_bubble(Bubble& b, int width, int height, std::uint64_t& seed) {
    b.x = rand_range(seed, width);
    b.y = water_bottom(height);
    b.speed = 20 + rand_range(seed, 40);  // 0.20 .. 0.59 cell/tick
    b.accum = 0;
}

}  // namespace

Bubble spawn_bubble(int width, int height, std::uint64_t& seed) {
    const int span = water_bottom(height) - water_top() + 1;
    Bubble b{};
    b.x = rand_range(seed, width);
    b.y = water_top() + rand_range(seed, span);
    b.speed = 20 + rand_range(seed, 40);
    b.accum = 0;
    return b;
}

void advance_bubble(Bubble& b, int width, int height, std::uint64_t& seed) {
    b.accum += b.speed;
    while (b.accum >= SPEED_SCALE) {
        b.accum -= SPEED_SCALE;
        --b.y;
    }
    if (b.y <= SURFACE_ROW)
        respawn_bubble(b, width, height, seed);
}

}  // namespace aqua
