// ct-exemarker — see README.md for context.
//
// Stable declarations only. This file MUST NOT change across builds:
// its content_hash flows into every consumer's dep_hash and into PCH
// command hashes that include it. Keep the per-build mutable values
// (name, version, build timestamps, ...) in the generated .cpp, not
// here.
#pragma once

namespace appinfo {

extern const char* const name;
extern const char* const version;

}  // namespace appinfo
