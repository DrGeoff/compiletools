// Game engine initialization test
//
// This test demonstrates a header guard bug where Pass 2 macro convergence
// causes transitive dependencies to be missed in the game's equipment system.
//
// Bug Impact: Weapon system dependencies (zlib compression, math library) are
// not discovered because Pass 2 skips header_a.hpp after its guard is defined.

#include "header_a.hpp"

int main() {
    // Initialize game with player and equipment
    game::player hero;
    hero.equip_weapon();
    hero.attack();
    return 0;
}
