// Precompiled-header source for consumer.cpp.
//
// Pulls in enough of the standard library that from-source parsing is
// observably more work than reading the cached .gch — large enough for
// the wall-time gap between "PCH consumed" and "PCH bypassed" to be
// real, but small enough that the test runs in seconds.
//
// Critical structural detail: this header lives in the SAME DIRECTORY
// as the consumer source file. That is the realistic case (private
// PCH next to its sole consumer) and the configuration under which
// ct-cake's `-I <cas-pchdir>/<hash>` wiring is bypassed by GCC's
// include-search resolution order — the source-file directory is
// searched before any `-I` dir, so GCC resolves pch.h to the source
// copy and then looks for a sibling .gch that doesn't exist.
//
// Traditional include guard (not `#pragma once`) so GCC doesn't warn
// "#pragma once in main file" when this header is the .gch source.
#ifndef CT_PCH_BYPASS_BUG_PCH_H
#define CT_PCH_BYPASS_BUG_PCH_H
#include <algorithm>
#include <map>
#include <memory>
#include <string>
#include <vector>
#endif
