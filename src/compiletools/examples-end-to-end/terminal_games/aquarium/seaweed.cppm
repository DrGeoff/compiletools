// seaweed.cppm -- the aquarium's seaweed: interface unit (module aquarium.seaweed).
//
// Declares the Weed type, its comparison, the (constexpr, pure) sway function
// that stays in the interface, and the signature of spawn_weed -- whose
// definition lives in seaweed_impl.cpp. No `import aquarium.water;` here.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.seaweed;

export namespace aqua {

struct Weed {
    int x;
    int height;  // plant height in rows (distinct from the tank height)
};
constexpr bool operator==(const Weed& a, const Weed& b) {
    return a.x == b.x && a.height == b.height;
}

// A weed anchored to the floor at a random column, 2..5 rows tall. Deterministic in seed.
Weed spawn_weed(int width, int tank_height, std::uint64_t& seed);

// Horizontal sway of a weed segment: a pure, periodic function of the global
// tick and the segment index -- no <cmath>, so it stays constexpr and exactly
// testable. Returns -1, 0 or +1; period is 24 ticks.
constexpr int seaweed_offset(std::uint64_t tick, int row) {
    constexpr int wave[6] = {0, 1, 1, 0, -1, -1};
    const std::uint64_t phase = (tick / 4 + static_cast<std::uint64_t>(row)) % 6;
    return wave[phase];
}

}  // namespace aqua
