// heavy.h — the precompiled-header payload for widget.cpp.
//
// A few standard headers so there is a real .gch for ct-cake to build and
// cache. Nothing domain-specific: this example exists only to exercise the PCH
// precompile rule, which is what the relative-cas-dir bug breaks (see
// README.md and test_relative_cas_dir_bug.py).
#pragma once

#include <string>
#include <unordered_map>
#include <vector>
