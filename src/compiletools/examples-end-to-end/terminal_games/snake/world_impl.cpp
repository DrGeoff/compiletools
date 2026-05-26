// world_impl.cpp -- the pure Snake simulation: implementation unit (module snake.world).
//
// Defines what world.cppm declares. As a `module snake.world;` unit it
// implicitly imports the world interface (so all exported types are in scope)
// and inherits the `export import snake.rng;` re-export, making next_rand
// visible here. ct-cake pulls this file into the link automatically for
// anything that imports snake.world.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

#include <cstdint>
#include <deque>

module snake.world;

namespace snake {
namespace {

// Place food on a cell not currently occupied by the body. Deterministic.
// In an anonymous namespace (internal linkage): private to this implementation
// unit, not part of the public interface.
Cell spawn_food(const std::deque<Cell>& body, int w, int h, std::uint64_t& seed) {
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

}  // namespace

World initial(int width, int height, std::uint64_t seed) {
    World w{width, height, {}, Direction::Right, {}, true, seed};
    const int cy = height / 2;
    const int cx = width / 2;
    for (int i = 0; i < START_LENGTH; ++i)
        w.body.push_back({cx - i, cy});  // head first, tail trailing left
    w.food = spawn_food(w.body, width, height, w.seed);
    return w;
}

Direction turn(Direction current, char key) {
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

Cell step_head(Cell head, Direction dir) {
    switch (dir) {
        case Direction::Up:    return {head.x, head.y - 1};
        case Direction::Down:  return {head.x, head.y + 1};
        case Direction::Left:  return {head.x - 1, head.y};
        case Direction::Right: return {head.x + 1, head.y};
    }
    return head;
}

World step(World w, Direction dir) {
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

}  // namespace snake
