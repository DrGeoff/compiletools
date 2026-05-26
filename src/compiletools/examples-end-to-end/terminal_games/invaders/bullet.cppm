// bullet.cppm -- the player bullet: interface unit (module invaders.bullet).
//
// Declares the Bullet sub-struct, its comparison, the NO_BULLET sentinel, and
// the advance_bullet signature. The definition lives in bullet_impl.cpp (a
// `module invaders.bullet;` implementation unit).
// imports invaders.formation (plain import, NOT re-exported) because advance_bullet
// takes a Formation&; consumers that need Formation must import invaders.formation
// themselves.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

export module invaders.bullet;

import invaders.formation;

export namespace invaders {

inline constexpr int NO_BULLET = -1;

struct Bullet {
    int x;  // column; NO_BULLET when no bullet in flight
    int y;  // row;    NO_BULLET when no bullet in flight
};
constexpr bool operator==(const Bullet& a, const Bullet& b) {
    return a.x == b.x && a.y == b.y;
}

// Advance one tick: rise the bullet, clear it on out-of-bounds or invader hit.
void advance_bullet(Bullet& b, Formation& f);

}  // namespace invaders
