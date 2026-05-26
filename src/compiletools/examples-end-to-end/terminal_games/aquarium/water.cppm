// water.cppm -- the aquarium's shared environment primitives (module aquarium.water).
//
// The leaf of the aquarium's module graph: the deterministic RNG, the motion
// scale, and the tank geometry -- all pure functions of plain dimensions, never
// of Tank. That keeps this a leaf the entity modules can import without a cycle
// back through aquarium.tank. fish/bubbles/seaweed import it; tank re-exports it.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module aquarium.water;

export namespace aqua {

// Fixed-point motion scale: an entity advances one cell each time its
// accumulator crosses this. Pure integers keep the simulation exactly
// reproducible (tests use exact-equality assertions).
inline constexpr int SPEED_SCALE = 100;

// Row 0 is the water surface; the last row is the sandy floor. Fish and bubbles
// live strictly between them.
inline constexpr int SURFACE_ROW = 0;

// Advance the LCG and return its top bits -- the same generator as snake.world,
// so the determinism story is identical across the whole terminal_games example.
constexpr std::uint64_t next_rand(std::uint64_t& seed) {
    seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed >> 33;
}

constexpr int rand_range(std::uint64_t& seed, int n) {
    const int span = n < 1 ? 1 : n;
    return static_cast<int>(next_rand(seed) % static_cast<std::uint64_t>(span));
}

// Tank geometry as pure functions of the tank height: the floor row and the
// inclusive open-water band between surface and floor.
constexpr int floor_row(int height) { return height - 1; }
constexpr int water_top() { return SURFACE_ROW + 1; }
constexpr int water_bottom(int height) { return floor_row(height) - 1; }

}  // namespace aqua
