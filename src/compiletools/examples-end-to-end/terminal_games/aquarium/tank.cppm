// tank.cppm -- the aquarium aggregate: interface unit (module aquarium.tank).
//
// Re-exports water + the three entity modules, declares the Tank aggregate and
// the simulation entry points. The definitions live in tank_impl.cpp. A
// consumer's single `import aquarium.tank;` brings in the whole aquarium -- and
// ct-cake discovers all five interface units AND all four implementation units
// just by walking the import edges, with no file list anywhere.
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
Tank initial(int width, int height, std::uint64_t seed);

// Advance one tick. Pure: returns a new Tank. Delegates per-entity motion to the
// fish and bubble modules; counts are conserved (entities wrap/respawn).
Tank step(Tank t);

}  // namespace aqua
