// tank.cppm -- the pure ASCII-aquarium simulation (named module aquarium.tank).
//
// I/O-free and deterministic: every fish, bubble and weed is spawned from an
// explicit LCG seed carried in the Tank, so step() is a pure function of state.
// Both the artwork (aquarium.cpp) and the test (test_tank.cpp) import this single
// source of truth. The module knows how *many* fish species exist but nothing
// about their glyphs or colours -- that presentation lives in aquarium.cpp, the
// same way snake.world has no '@' or '*' in it.
//
// Motion is fixed-point: speed is in hundredths of a cell per tick and an entity
// advances one cell each time its accumulator crosses SPEED_SCALE. Pure integers
// keep step() exactly reproducible (the test uses exact-equality assertions).
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>
#include <vector>

export module aquarium.tank;

export namespace aqua {

// Distinct fish kinds. The module only stores the index; aquarium.cpp maps it to
// a glyph + colour. Bump this and the renderer's table together.
inline constexpr int SPECIES_COUNT = 5;

// Fixed-point motion scale: accum crossing this advances the entity one cell.
inline constexpr int SPEED_SCALE = 100;

// Row 0 is the water surface; the last row is the sandy floor. Fish and bubbles
// live strictly between them.
inline constexpr int SURFACE_ROW = 0;

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

struct Bubble {
    int x;
    int y;
    int speed;
    int accum;
};
constexpr bool operator==(const Bubble& a, const Bubble& b) {
    return a.x == b.x && a.y == b.y && a.speed == b.speed && a.accum == b.accum;
}

struct Weed {
    int x;
    int height;
};
constexpr bool operator==(const Weed& a, const Weed& b) {
    return a.x == b.x && a.height == b.height;
}

struct Tank {
    int width;
    int height;
    std::uint64_t tick;
    std::uint64_t seed;
    std::vector<Fish> fish;
    std::vector<Bubble> bubbles;
    std::vector<Weed> weed;
};

// Advance the LCG and return its top bits -- the same generator as snake.world,
// so the determinism story is identical across the example.
constexpr std::uint64_t next_rand(std::uint64_t& seed) {
    seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed >> 33;
}

constexpr int rand_range(std::uint64_t& seed, int n) {
    const int span = n < 1 ? 1 : n;
    return static_cast<int>(next_rand(seed) % static_cast<std::uint64_t>(span));
}

// Floor row and the inclusive open-water band between surface and floor.
inline int floor_row(const Tank& t) { return t.height - 1; }
inline int water_top(const Tank&) { return SURFACE_ROW + 1; }
inline int water_bottom(const Tank& t) { return floor_row(t) - 1; }

// Re-enter a fish from the edge opposite its travel, with a fresh look, depth and
// speed. Deterministic in t.seed.
inline void respawn_fish(Tank& t, Fish& f) {
    const int span = water_bottom(t) - water_top(t) + 1;
    f.x = f.dir > 0 ? 0 : t.width - 1;
    f.y = water_top(t) + rand_range(t.seed, span);
    f.species = rand_range(t.seed, SPECIES_COUNT);
    f.speed = 30 + rand_range(t.seed, 70);  // 0.30 .. 0.99 cell/tick
    f.accum = 0;
}

// Re-emit a bubble near the floor at a fresh column. Deterministic in t.seed.
inline void respawn_bubble(Tank& t, Bubble& b) {
    b.x = rand_range(t.seed, t.width);
    b.y = water_bottom(t);
    b.speed = 20 + rand_range(t.seed, 40);  // 0.20 .. 0.59 cell/tick
    b.accum = 0;
}

// Scatter `count` fish across the open water, each swimming left or right.
inline void populate_fish(Tank& t, int count) {
    const int span = water_bottom(t) - water_top(t) + 1;
    for (int i = 0; i < count; ++i) {
        Fish f{};
        f.dir = (rand_range(t.seed, 2) == 0) ? -1 : +1;
        f.x = rand_range(t.seed, t.width);
        f.y = water_top(t) + rand_range(t.seed, span);
        f.species = rand_range(t.seed, SPECIES_COUNT);
        f.speed = 30 + rand_range(t.seed, 70);
        f.accum = 0;
        t.fish.push_back(f);
    }
}

inline void populate_bubbles(Tank& t, int count) {
    const int span = water_bottom(t) - water_top(t) + 1;
    for (int i = 0; i < count; ++i) {
        Bubble b{};
        b.x = rand_range(t.seed, t.width);
        b.y = water_top(t) + rand_range(t.seed, span);
        b.speed = 20 + rand_range(t.seed, 40);
        b.accum = 0;
        t.bubbles.push_back(b);
    }
}

inline void populate_weed(Tank& t, int count) {
    const int max_extra = (t.height - 4) > 1 ? (t.height - 4) : 1;
    for (int i = 0; i < count; ++i) {
        Weed w{};
        w.x = rand_range(t.seed, t.width);
        w.height = 2 + rand_range(t.seed, max_extra > 4 ? 4 : max_extra);
        t.weed.push_back(w);
    }
}

// Build the starting tank. Population scales with area so a small test tank and a
// full-screen terminal both look right.
inline Tank initial(int width, int height, std::uint64_t seed) {
    Tank t{};
    t.width = width < 1 ? 1 : width;
    t.height = height < 3 ? 3 : height;  // surface + >=1 water row + floor
    t.tick = 0;
    t.seed = seed;
    const int area = t.width * t.height;
    populate_fish(t, area / 60 + 3);
    populate_bubbles(t, area / 120 + 2);
    populate_weed(t, t.width / 12 + 2);
    return t;
}

// Advance one tick. Pure: returns a new Tank. Fish drift horizontally and wrap;
// bubbles rise and respawn at the surface. Counts are conserved.
inline Tank step(Tank t) {
    ++t.tick;
    for (Fish& f : t.fish) {
        f.accum += f.speed;
        while (f.accum >= SPEED_SCALE) {
            f.accum -= SPEED_SCALE;
            f.x += f.dir;
        }
        if (f.x < 0 || f.x >= t.width)
            respawn_fish(t, f);
    }
    for (Bubble& b : t.bubbles) {
        b.accum += b.speed;
        while (b.accum >= SPEED_SCALE) {
            b.accum -= SPEED_SCALE;
            --b.y;
        }
        if (b.y <= SURFACE_ROW)
            respawn_bubble(t, b);
    }
    return t;
}

// Horizontal sway of a weed segment: a pure, periodic function of the global tick
// and the segment index -- no <cmath>, so it stays constexpr and exactly
// testable. Returns -1, 0 or +1; period is 24 ticks.
constexpr int seaweed_offset(std::uint64_t tick, int row) {
    constexpr int wave[6] = {0, 1, 1, 0, -1, -1};
    const std::uint64_t phase = (tick / 4 + static_cast<std::uint64_t>(row)) % 6;
    return wave[phase];
}

}  // namespace aqua
