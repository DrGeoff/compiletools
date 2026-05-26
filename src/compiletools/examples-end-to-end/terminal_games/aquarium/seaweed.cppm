// seaweed.cppm -- the aquarium's seaweed (module aquarium.seaweed).
//
// Owns the Weed state, its placement, and the pure sway function. Imports
// aquarium.water for the RNG and geometry; never learns what a Tank is.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.seaweed;

import aquarium.water;

export namespace aqua {

struct Weed {
    int x;
    int height;  // plant height in rows (distinct from the tank height)
};
constexpr bool operator==(const Weed& a, const Weed& b) {
    return a.x == b.x && a.height == b.height;
}

// A weed anchored to the floor at a random column, 2..5 rows tall. Deterministic.
inline Weed spawn_weed(int width, int tank_height, std::uint64_t& seed) {
    const int max_extra = (tank_height - 4) > 1 ? (tank_height - 4) : 1;
    Weed w{};
    w.x = rand_range(seed, width);
    w.height = 2 + rand_range(seed, max_extra > 4 ? 4 : max_extra);
    return w;
}

// Horizontal sway of a weed segment: a pure, periodic function of the global
// tick and the segment index -- no <cmath>, so it stays constexpr and exactly
// testable. Returns -1, 0 or +1; period is 24 ticks.
constexpr int seaweed_offset(std::uint64_t tick, int row) {
    constexpr int wave[6] = {0, 1, 1, 0, -1, -1};
    const std::uint64_t phase = (tick / 4 + static_cast<std::uint64_t>(row)) % 6;
    return wave[phase];
}

}  // namespace aqua
