// tank.cppm -- the aquarium aggregate (module aquarium.tank).
//
// Composes the three entity modules (fish, bubbles, seaweed) over the shared
// aquarium.water leaf into one tank: it owns the vectors, the
// dimensions, the tick and the seed, and drives the simulation forward. It
// re-exports water + fish + bubbles + seaweed, so a consumer's single
// `import aquarium.tank;` brings in the entire aquarium -- and ct-cake discovers
// every one of the five .cppm files just by walking those import edges.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>
#include <vector>

export module aquarium.tank;

export import aquarium.water;
export import aquarium.fish;
export import aquarium.bubbles;
export import aquarium.seaweed;

export namespace aqua {

struct Tank {
    int width;
    int height;
    std::uint64_t tick;
    std::uint64_t seed;
    std::vector<Fish> fish;
    std::vector<Bubble> bubbles;
    std::vector<Weed> weed;
};

// Build the starting tank. Population scales with area so a small test tank and a
// full-screen terminal both look right.
inline Tank initial(int width, int height, std::uint64_t seed) {
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

// Advance one tick. Pure: returns a new Tank. Delegates per-entity motion to the
// fish and bubble modules; counts are conserved (entities wrap/respawn).
inline Tank step(Tank t) {
    ++t.tick;
    for (Fish& f : t.fish)
        advance_fish(f, t.width, t.height, t.seed);
    for (Bubble& b : t.bubbles)
        advance_bubble(b, t.width, t.height, t.seed);
    return t;
}

}  // namespace aqua
