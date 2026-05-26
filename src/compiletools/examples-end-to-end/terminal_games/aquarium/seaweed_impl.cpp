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
    const int max_extra = (tank_height - 4) > 1 ? (tank_height - 4) : 1;
    Weed w{};
    w.x = rand_range(seed, width);
    w.height = 2 + rand_range(seed, max_extra > 4 ? 4 : max_extra);
    return w;
}

}  // namespace aqua
