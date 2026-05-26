// fish_impl.cpp -- the aquarium's fish: implementation unit (module aquarium.fish).
//
// Defines what fish.cppm declares. As a `module aquarium.fish;` unit it
// implicitly imports the fish interface (so Fish/SPECIES_COUNT are in scope) and
// imports aquarium.water for the RNG, motion scale and geometry. ct-cake pulls
// this file into the link automatically for anything that imports aquarium.fish.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cstdint>

module aquarium.fish;

import aquarium.water;

namespace aqua {
namespace {

// Re-enter a fish from the edge opposite its travel, with a fresh look, depth
// and speed. In an anonymous namespace (internal linkage): private to this
// implementation unit, not part of the public interface.
void respawn_fish(Fish& f, int width, int height, std::uint64_t& seed) {
    const int span = water_bottom(height) - water_top() + 1;
    f.x = f.dir > 0 ? 0 : width - 1;
    f.y = water_top() + rand_range(seed, span);
    f.species = rand_range(seed, SPECIES_COUNT);
    f.speed = 30 + rand_range(seed, 70);  // 0.30 .. 0.99 cell/tick
    f.accum = 0;
}

}  // namespace

Fish spawn_fish(int width, int height, std::uint64_t& seed) {
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

void advance_fish(Fish& f, int width, int height, std::uint64_t& seed) {
    f.accum += f.speed;
    while (f.accum >= SPEED_SCALE) {
        f.accum -= SPEED_SCALE;
        f.x += f.dir;
    }
    if (f.x < 0 || f.x >= width)
        respawn_fish(f, width, height, seed);
}

}  // namespace aqua
