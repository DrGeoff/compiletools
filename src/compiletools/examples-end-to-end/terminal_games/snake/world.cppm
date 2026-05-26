// world.cppm -- the pure Snake simulation: interface unit (module snake.world).
//
// I/O-free and deterministic. Re-exports snake.rng so a single
// `import snake.world;` brings in next_rand. Definitions live in world_impl.cpp
// (a `module snake.world;` implementation unit -- ct-cake discovers and links it
// from the same import edge).
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>
#include <deque>

export module snake.world;

export import snake.rng;

export namespace snake {

inline constexpr int START_LENGTH = 3;

enum class Direction { Up, Down, Left, Right };
enum class Verdict { Playing, Dead };

struct Cell {
    int x;
    int y;
};
constexpr bool operator==(Cell a, Cell b) { return a.x == b.x && a.y == b.y; }

struct World {
    int width;
    int height;
    std::deque<Cell> body;  // front() is the head
    Direction dir;
    Cell food;
    bool alive;
    std::uint64_t seed;     // LCG state for deterministic food
};

// The state every game and test starts from: a horizontal snake near centre.
World initial(int width, int height, std::uint64_t seed);

// Map a key to a new direction, rejecting a 180-degree reversal (which would
// instantly self-collide). Unknown keys keep the current direction.
Direction turn(Direction current, char key);

// Return the cell the head would move to in the given direction (test-visible).
Cell step_head(Cell head, Direction dir);

// Advance one tick in the given direction. Hitting a wall or the body kills the
// snake; eating food grows it by one and respawns food; otherwise it moves.
World step(World w, Direction dir);

inline Verdict classify(const World& w) { return w.alive ? Verdict::Playing : Verdict::Dead; }

inline int score(const World& w) { return static_cast<int>(w.body.size()) - START_LENGTH; }

}  // namespace snake
