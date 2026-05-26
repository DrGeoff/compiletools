// bubbles.cppm -- the aquarium's bubbles: interface unit (module aquarium.bubbles).
//
// Declares the Bubble type, its comparison, and the bubble behaviour signatures;
// the definitions live in bubbles_impl.cpp. No `import aquarium.water;` here --
// only the implementation needs the RNG and geometry.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.bubbles;

export namespace aqua {

struct Bubble {
    int x;
    int y;
    int speed;
    int accum;
};
constexpr bool operator==(const Bubble& a, const Bubble& b) {
    return a.x == b.x && a.y == b.y && a.speed == b.speed && a.accum == b.accum;
}

// A fresh bubble somewhere in the open water. Deterministic in seed.
Bubble spawn_bubble(int width, int height, std::uint64_t& seed);

// Advance one tick: rise by the fixed-point speed, respawning near the floor
// once it reaches the surface.
void advance_bubble(Bubble& b, int width, int height, std::uint64_t& seed);

}  // namespace aqua
