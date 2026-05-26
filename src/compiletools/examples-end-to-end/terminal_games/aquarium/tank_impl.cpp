// tank_impl.cpp -- the aquarium aggregate: implementation unit (module aquarium.tank).
//
// Defines initial() and step(). As a `module aquarium.tank;` unit it implicitly
// imports the tank interface, whose `export import`s make Fish/Bubble/Weed and
// the spawn_*/advance_* functions visible here. ct-cake links this unit
// automatically for anything that imports aquarium.tank.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cstdint>
#include <vector>

module aquarium.tank;

namespace aqua {

Tank initial(int width, int height, std::uint64_t seed) {
    Tank t{};
    t.width = width < 1 ? 1 : width;
    t.height = height < 3 ? 3 : height;  // surface + >=1 water row + floor
    t.tick = 0;
    t.seed = seed;
    const int area = t.width * t.height;
    for (int i = 0, n = area / 60 + 3; i < n; ++i)
        t.fish.push_back(spawn_fish(t.width, t.height, t.seed));
    for (int i = 0, n = area / 120 + 2; i < n; ++i)
        t.bubbles.push_back(spawn_bubble(t.width, t.height, t.seed));
    for (int i = 0, n = t.width / 12 + 2; i < n; ++i)
        t.weed.push_back(spawn_weed(t.width, t.height, t.seed));
    return t;
}

Tank step(Tank t) {
    ++t.tick;
    for (Fish& f : t.fish)
        advance_fish(f, t.width, t.height, t.seed);
    for (Bubble& b : t.bubbles)
        advance_bubble(b, t.width, t.height, t.seed);
    return t;
}

}  // namespace aqua
