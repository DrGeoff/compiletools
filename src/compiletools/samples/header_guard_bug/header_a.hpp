// Player entity for game engine
//
// This header demonstrates the guard bug:
// - Has a non-standard guard pattern (SOME_OTHER_MACRO between #ifndef and #define)
// - Includes header_b.hpp for weapon equipment system
// - After Pass 1, HEADER_A_HPP_GUARD is defined in macro state
// - Pass 2 skips this header's contents, missing header_b.hpp and its dependencies

#ifndef HEADER_A_HPP_GUARD
#define SOME_OTHER_MACRO 1  // Breaks FileAnalyzer guard detection.  A comment also broke the pattern.
#define HEADER_A_HPP_GUARD

// Feature macro to trigger Pass 2 analysis (macro convergence)
#define PLAYER_HAS_INVENTORY 1

// Include weapon system - this will be SKIPPED in Pass 2 due to guard bug
#include "header_b.hpp"

namespace game {

class player {
public:
    void equip_weapon();
    void attack();
    void take_damage(int amount);

private:
    int health_;
    int damage_;
};

} // namespace game

#endif // HEADER_A_HPP_GUARD
