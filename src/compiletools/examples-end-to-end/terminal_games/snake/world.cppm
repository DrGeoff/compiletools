// world.cppm -- the pure Snake simulation (named module snake.world).
//
// I/O-free and deterministic: food placement is driven by an explicit LCG seed
// carried in the World, so step() is a pure function of state. Both the game
// (snake.cpp) and the test (test_snake.cpp) import this single source of truth.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <cstdint>
#include <deque>

export module snake.world;

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

// Advance the LCG and return a value in [0, n). Pure given the seed.
constexpr std::uint64_t next_rand(std::uint64_t& seed) {
    seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed >> 33;
}

// Place food on a cell not currently occupied by the body. Deterministic.
inline Cell spawn_food(const std::deque<Cell>& body, int w, int h, std::uint64_t& seed) {
    Cell c{};
    do {
        c.x = static_cast<int>(next_rand(seed) % static_cast<std::uint64_t>(w));
        c.y = static_cast<int>(next_rand(seed) % static_cast<std::uint64_t>(h));
    } while ([&] {
        for (const Cell& b : body)
            if (b == c) return true;
        return false;
    }());
    return c;
}

// The state every game and test starts from: a horizontal snake near centre.
inline World initial(int width, int height, std::uint64_t seed) {
    World w{width, height, {}, Direction::Right, {}, true, seed};
    const int cy = height / 2;
    const int cx = width / 2;
    for (int i = 0; i < START_LENGTH; ++i)
        w.body.push_back({cx - i, cy});  // head first, tail trailing left
    w.food = spawn_food(w.body, width, height, w.seed);
    return w;
}

// Map a key to a new direction, rejecting a 180-degree reversal (which would
// instantly self-collide). Unknown keys keep the current direction.
inline Direction turn(Direction current, char key) {
    Direction want = current;
    switch (key) {
        case 'w': case 'k': want = Direction::Up;    break;
        case 's': case 'j': want = Direction::Down;  break;
        case 'a': case 'h': want = Direction::Left;  break;
        case 'd': case 'l': want = Direction::Right; break;
        default: return current;
    }
    const bool reversal =
        (current == Direction::Up    && want == Direction::Down) ||
        (current == Direction::Down  && want == Direction::Up) ||
        (current == Direction::Left  && want == Direction::Right) ||
        (current == Direction::Right && want == Direction::Left);
    return reversal ? current : want;
}

inline Cell step_head(Cell head, Direction dir) {
    switch (dir) {
        case Direction::Up:    return {head.x, head.y - 1};
        case Direction::Down:  return {head.x, head.y + 1};
        case Direction::Left:  return {head.x - 1, head.y};
        case Direction::Right: return {head.x + 1, head.y};
    }
    return head;
}

// Advance one tick in the given direction. Hitting a wall or the body kills the
// snake; eating food grows it by one and respawns food; otherwise it moves.
inline World step(World w, Direction dir) {
    if (!w.alive) return w;
    w.dir = dir;
    const Cell head = step_head(w.body.front(), dir);

    if (head.x < 0 || head.x >= w.width || head.y < 0 || head.y >= w.height) {
        w.alive = false;
        return w;
    }
    for (const Cell& b : w.body)
        if (b == head) {
            w.alive = false;
            return w;
        }

    w.body.push_front(head);
    if (head == w.food)
        w.food = spawn_food(w.body, w.width, w.height, w.seed);
    else
        w.body.pop_back();  // grow only when eating
    return w;
}

inline Verdict classify(const World& w) { return w.alive ? Verdict::Playing : Verdict::Dead; }

inline int score(const World& w) { return static_cast<int>(w.body.size()) - START_LENGTH; }

}  // namespace snake
