// bullet_impl.cpp -- the player bullet: implementation unit (module invaders.bullet).
//
// Defines what bullet.cppm declares. As a `module invaders.bullet;` unit it
// implicitly imports the bullet interface (so Bullet/NO_BULLET are in scope) and
// imports invaders.formation for try_hit. ct-cake pulls this file into the link
// automatically for anything that imports invaders.bullet.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

module invaders.bullet;

import invaders.formation;

namespace invaders {

void advance_bullet(Bullet& b, Formation& f) {
    if (b.y == NO_BULLET) return;
    --b.y;
    if (b.y < 0) { b.x = b.y = NO_BULLET; return; }
    if (try_hit(f, b.x, b.y)) { b.x = b.y = NO_BULLET; }
}

}  // namespace invaders
