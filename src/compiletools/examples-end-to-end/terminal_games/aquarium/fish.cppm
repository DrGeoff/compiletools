// fish.cppm -- the aquarium's fish (module aquarium.fish).
//
// Owns the Fish state and its per-fish behaviour: spawning, fixed-point motion,
// and edge-wrap respawn. Pure and deterministic in the seed it is handed.
// Imports aquarium.water for the RNG, the motion scale and the geometry; it
// never learns what a Tank is -- aquarium.tank owns the fish vector.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.fish;

import aquarium.water;

export namespace aqua {

// Distinct fish kinds. The module only stores the index; aquarium.cpp maps it to
// a glyph + colour. Bump this and the renderer's table together.
inline constexpr int SPECIES_COUNT = 5;

struct Fish {
    int x;        // column of the fish's anchor cell
    int y;        // row, strictly between surface and floor
    int dir;      // +1 swims right, -1 swims left
    int species;  // [0, SPECIES_COUNT)
    int speed;    // hundredths of a cell per tick
    int accum;    // fixed-point remainder
};
constexpr bool operator==(const Fish& a, const Fish& b) {
    return a.x == b.x && a.y == b.y && a.dir == b.dir &&
           a.species == b.species && a.speed == b.speed && a.accum == b.accum;
}

// Re-enter a fish from the edge opposite its travel, with a fresh look, depth and
// speed. Deterministic in seed.
inline void respawn_fish(Fish& f, int width, int height, std::uint64_t& seed) {
    const int span = water_bottom(height) - water_top() + 1;
    f.x = f.dir > 0 ? 0 : width - 1;
    f.y = water_top() + rand_range(seed, span);
    f.species = rand_range(seed, SPECIES_COUNT);
    f.speed = 30 + rand_range(seed, 70);  // 0.30 .. 0.99 cell/tick
    f.accum = 0;
}

// A fresh fish at a random position, swimming left or right. Deterministic.
inline Fish spawn_fish(int width, int height, std::uint64_t& seed) {
    const int span = water_bottom(height) - water_top() + 1;
    Fish f{};
    f.dir = (rand_range(seed, 2) == 0) ? -1 : +1;
    f.x = rand_range(seed, width);
    f.y = water_top() + rand_range(seed, span);
    f.species = rand_range(seed, SPECIES_COUNT);
    f.speed = 30 + rand_range(seed, 70);
    f.accum = 0;
    return f;
}

// Advance one tick: drift by the fixed-point speed, wrapping (respawning) when
// the fish leaves either edge.
inline void advance_fish(Fish& f, int width, int height, std::uint64_t& seed) {
    f.accum += f.speed;
    while (f.accum >= SPEED_SCALE) {
        f.accum -= SPEED_SCALE;
        f.x += f.dir;
    }
    if (f.x < 0 || f.x >= width)
        respawn_fish(f, width, height, seed);
}

}  // namespace aqua
