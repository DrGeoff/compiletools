// field_impl.cpp -- the pure Space Invaders simulation: implementation unit (module invaders.field).
//
// Defines what field.cppm declares. As a `module invaders.field;` unit it
// implicitly imports the field interface, which re-exports invaders.formation and
// invaders.bullet -- so Formation, Bullet, make_formation, advance_bullet, march,
// remaining, formation_bottom, NO_BULLET, etc. are all in scope. ct-cake pulls
// this file into the link automatically for anything that imports invaders.field.
//
// CAS: module implementation unit -> object in cas-objdir (no BMI).
module;

module invaders.field;

namespace invaders {

Field initial(int width, int height) {
    Field f{width, height, width / 2, 0, make_formation(), Bullet{NO_BULLET, NO_BULLET}};
    return f;
}

Field step(Field f, Action action) {
    if (action == Action::Left  && f.player_x > 0)             --f.player_x;
    if (action == Action::Right && f.player_x < f.width - 1)   ++f.player_x;
    if (action == Action::Fire  && f.bullet.y == NO_BULLET) {
        f.bullet.x = f.player_x;
        f.bullet.y = f.height - 2;
    }
    advance_bullet(f.bullet, f.formation);
    if (++f.ticks % MARCH_EVERY == 0) march(f.formation, f.width);
    return f;
}

Verdict classify(const Field& f) {
    if (remaining(f.formation) == 0)                  return Verdict::Won;
    if (formation_bottom(f.formation) >= f.height - 1) return Verdict::Lost;
    return Verdict::Playing;
}

}  // namespace invaders
