//#PCH=heavy.h
#include "heavy.h"

// widget.cpp — a minimal precompiled-header consumer.
//
// The `//#PCH=heavy.h` annotation makes ct-cake precompile heavy.h into a .gch
// in cas-pchdir and consume it here. That PCH precompile rule is exactly what
// the relative-cas-dir bug breaks when ct-cake is invoked from a subdirectory
// of the gitroot with a *relative* --cas-pchdir. See README.md.
int main() {
    std::unordered_map<std::string, int> counts;
    for (const std::string& w : std::vector<std::string>{"widget", "widget"})
        ++counts[w];
    return counts["widget"] == 2 ? 0 : 1;
}
