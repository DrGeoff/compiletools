// rng.cppm -- snake's deterministic RNG: interface-only leaf (module snake.rng).
//
// The LCG lives here as a constexpr leaf (no implementation unit -- all
// constexpr, like aquarium.water). snake.world re-exports it; the test imports
// it directly to exercise determinism in isolation.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>

export module snake.rng;

export namespace snake {

// Advance the LCG and return its top 31 bits. Pure given the seed.
constexpr std::uint64_t next_rand(std::uint64_t& seed) {
    seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed >> 33;
}

}  // namespace snake
