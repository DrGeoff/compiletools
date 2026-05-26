// fish.cppm -- the aquarium's fish: interface unit (module aquarium.fish).
//
// Declares the Fish type, its comparison, the species count, and the signatures
// of the fish behaviour. The definitions live in fish_impl.cpp (a
// `module aquarium.fish;` implementation unit) -- ct-cake discovers and links it
// from the same import edge. The interface needs no `import aquarium.water;`:
// only the implementation touches the RNG and geometry.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.fish;

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

// A fresh fish at a random position, swimming left or right. Deterministic in seed.
Fish spawn_fish(int width, int height, std::uint64_t& seed);

// Advance one tick: drift by the fixed-point speed, wrapping (respawning at the
// opposite edge with a fresh look/depth/speed) when the fish leaves either edge.
void advance_fish(Fish& f, int width, int height, std::uint64_t& seed);

}  // namespace aqua
