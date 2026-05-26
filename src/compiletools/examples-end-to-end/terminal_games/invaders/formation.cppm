// formation.cppm -- the invader formation: interface unit (module invaders.formation).
//
// Declares the Formation sub-struct, its comparison, the grid constants, the
// inline screen-coordinate accessors, and the signatures of the formation
// operations. The definitions live in formation_impl.cpp (a
// `module invaders.formation;` implementation unit). A consumer's
// `import invaders.formation;` brings in the whole formation sub-system.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

#include <vector>

export module invaders.formation;

export namespace invaders {

inline constexpr int INV_ROWS  = 3;
inline constexpr int INV_COLS  = 6;
inline constexpr int MARCH_EVERY = 4;   // ticks between formation steps

struct Formation {
    int offset_x;            // formation top-left column
    int offset_y;            // formation top-left row
    int march_dir;           // +1 right, -1 left
    std::vector<bool> alive; // INV_ROWS*INV_COLS row-major
};
constexpr bool operator==(const Formation& a, const Formation& b) {
    return a.offset_x == b.offset_x && a.offset_y == b.offset_y &&
           a.march_dir == b.march_dir && a.alive == b.alive;
}

inline bool inv_alive(const Formation& f, int r, int c) { return f.alive[r * INV_COLS + c]; }
inline int  inv_screen_x(const Formation& f, int c)     { return f.offset_x + c * 2; }
inline int  inv_screen_y(const Formation& f, int r)     { return f.offset_y + r; }

// Build the starting formation: offset (1,1), march_dir +1, all invaders alive.
Formation make_formation();

int remaining(const Formation& f);
int formation_bottom(const Formation& f);   // max screen y of a living invader; -1 if none

void march(Formation& f, int width);

// Kill the living invader at screen (x,y); returns true on hit, false on miss.
bool try_hit(Formation& f, int x, int y);

}  // namespace invaders
