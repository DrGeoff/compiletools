// Weapon system for game engine
//
// This header contains magic flags for external dependencies:
// - zlib: Compress weapon asset data
// - libm: Math library for damage calculations
//
// Bug: These magic flags are NOT discovered when Pass 2 skips header_a.hpp

#ifndef HEADER_B_HPP_GUARD
// Break guard detection pattern
#define HEADER_B_HPP_GUARD

// Magic flags that should be discovered by ct-magicflags
//#PKG-CONFIG=zlib
//#LDFLAGS=-lm

namespace game {

class weapon {
public:
    void deal_damage();
    int calculate_critical_strike();
    void compress_weapon_data();  // Requires zlib

private:
    int base_damage_;
    float critical_multiplier_;  // Requires libm for calculations
};

} // namespace game

#endif // HEADER_B_HPP_GUARD
