// seaweed_impl.cpp -- the aquarium's seaweed: implementation unit (module aquarium.seaweed).
//
// Defines spawn_weed (declared in seaweed.cppm); imports aquarium.water for the
// RNG. The sway function (seaweed_offset) is constexpr and stays in the
// interface, so it is NOT defined here.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cstdint>

module aquarium.seaweed;

import aquarium.water;

namespace aqua {

Weed spawn_weed(int width, int tank_height, std::uint64_t& seed) {
    // Height spans MIN..MAX rows, but never tall enough to reach the surface in
    // a short tank: cap the random span by the rows actually available.
    constexpr int full_span = MAX_WEED_HEIGHT - MIN_WEED_HEIGHT + 1;  // distinct heights
    const int available = (tank_height - 4) > 1 ? (tank_height - 4) : 1;
    const int span = available < full_span ? available : full_span;
    Weed w{};
    w.x = rand_range(seed, width);
    w.height = MIN_WEED_HEIGHT + rand_range(seed, span);
    return w;
}

}  // namespace aqua
